"""Generic local L2 / Parquet dataset loader with DuckDB pushdown & bar aggregation.

Supports:
1. Arbitrary date-partitioned path templates (e.g. CFFEX/ZX01 futures L2 parquet).
2. Built-in ``ai_vibe_stock_l2`` locator compatible with ``ai-vibe/stocks/utils/data_loader.py``
   (``{year}/{YYYYMMDD}`` + ``HX02/{year}/{YYYYMMDD}``, prioritizing ``.parquet``).
3. DuckDB lazy scan with column projection & ContractId/Symbol predicate pushdown.
4. ``.NET Tick`` (``ExchTick - NET_TICK_OFFSET``), unix timestamp, and ISO time conversion.
5. Exact cumulative volume/turnover differencing per ``(TradingDay, Symbol)`` with
   multi-interval OHLCV+VWAP bar aggregation and optional ``l2:*`` microstructure features
   (``l2:spread``, ``l2:obi_1``, ``l2:obi_5``, ``l2:bid_depth``, ``l2:ask_depth``).
6. ``fetch_raw_l2()`` for tick/snapshot/order/trade microstructure research.
7. ``build_panel()`` for wide ``DatetimeIndex x symbol`` Alpha Zoo evaluation.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import duckdb
import numpy as np
import pandas as pd
import yaml

from backtest.loaders.base import validate_date_range, validate_ohlc
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

# .NET ticks = 100-nanosecond intervals since 0001-01-01 00:00:00
NET_TICK_OFFSET = 621355968000000000

_DEFAULT_CONFIG_PATH = Path.home() / ".vibe-trading" / "data-bridge" / "config.yaml"
_ENV_CONFIG_KEY = "VIBE_TRADING_LOCAL_DATA_CONFIG"

_INTERVAL_TO_FREQ: dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1H": "1h",
    "1h": "1h",
    "4H": "4h",
    "4h": "4h",
    "1D": "1D",
    "1d": "1D",
}

SCHEMA_PROFILES: dict[str, dict[str, str]] = {
    "l2_futures_pro": {
        "trading_day": "TradingDay",
        "symbol": "Symbol",
        "tick": "ExchTick",
        "open": "Open",
        "high": "Highest",
        "low": "Lowest",
        "last": "Last",
        "volume": "Volume",
        "turnover": "Turnover",
        "bid": "Bid",
        "ask": "Ask",
        "bid_vol": "BidVol",
        "ask_vol": "AskVol",
    },
    "l2_stock_snapshot": {
        "trading_day": "TradingDay",
        "symbol": "ContractId",
        "tick": "ExchTick",
        "open": "Open",
        "high": "Highest",
        "low": "Lowest",
        "last": "Last",
        "volume": "Volume",
        "turnover": "Turnover",
        "bid": "Bid",
        "ask": "Ask",
        "bid_vol": "BidVol",
        "ask_vol": "AskVol",
    },
}

_BID_VOL_5_CANDIDATES = (
    ("BidVol", "BidVol2", "BidVol3", "BidVol4", "BidVol5"),
    ("BidVol1", "BidVol2", "BidVol3", "BidVol4", "BidVol5"),
    ("bid_vol1", "bid_vol2", "bid_vol3", "bid_vol4", "bid_vol5"),
)
_ASK_VOL_5_CANDIDATES = (
    ("AskVol", "AskVol2", "AskVol3", "AskVol4", "AskVol5"),
    ("AskVol1", "AskVol2", "AskVol3", "AskVol4", "AskVol5"),
    ("ask_vol1", "ask_vol2", "ask_vol3", "ask_vol4", "ask_vol5"),
)

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(name: str) -> str:
    """Safely double-quote a SQL column identifier."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def resolve_local_data_config_path(default_path: Path | None = None) -> Path:
    """Resolve local data config path, honoring VIBE_TRADING_LOCAL_DATA_CONFIG."""
    env_val = os.environ.get(_ENV_CONFIG_KEY, "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return default_path if default_path is not None else _DEFAULT_CONFIG_PATH


def load_local_data_config(config_path: Path | None = None) -> dict[str, Any]:
    """Load YAML config from explicit path, env override, or default location."""
    path = config_path if config_path is not None else resolve_local_data_config_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_parquet_datasets(config_path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Return mapping dataset_name -> dataset_cfg from config ``datasets`` and ``sources``."""
    cfg = load_local_data_config(config_path)
    datasets: dict[str, dict[str, Any]] = {}

    for entry in cfg.get("datasets") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if name:
            datasets[name] = entry

    for entry in cfg.get("sources") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "").strip().lower() == "parquet_dataset":
            name = str(entry.get("name") or entry.get("symbol") or "").strip()
            if name and name not in datasets:
                datasets[name] = entry

    return datasets


def get_contract_overrides(
    symbol: str, config_path: Path | None = None
) -> dict[str, Any] | None:
    """Look up futures contract_overrides for ``symbol`` across configured datasets."""
    datasets = load_parquet_datasets(config_path)
    sym_upper = symbol.upper()
    for ds in datasets.values():
        overrides = ds.get("contract_overrides")
        if not isinstance(overrides, dict):
            continue
        if symbol in overrides and isinstance(overrides[symbol], dict):
            return overrides[symbol]
        for key, val in overrides.items():
            if str(key).upper() == sym_upper and isinstance(val, dict):
                return val
    return None


def resolve_contract_id(symbol: str, rule: str | None) -> int | str:
    """Map an instrument symbol to the parquet filter value according to ``rule``."""
    if not rule:
        return symbol
    rule_norm = str(rule).strip().lower()
    if rule_norm == "cn_stock_prefix":
        bare = symbol.split(".", 1)[0].strip()
        if bare.isdigit():
            prefix = "11" if bare.startswith("6") else "21"
            return int(f"{prefix}{bare}")
    return symbol


def _resolve_schema(dataset_cfg: dict[str, Any]) -> dict[str, str]:
    profile_name = str(dataset_cfg.get("schema_profile") or "").strip()
    if not profile_name:
        if dataset_cfg.get("locator") == "ai_vibe_stock_l2":
            profile_name = "l2_stock_snapshot"
        else:
            profile_name = "l2_futures_pro"
    schema = dict(SCHEMA_PROFILES.get(profile_name, SCHEMA_PROFILES["l2_futures_pro"]))
    overrides = dataset_cfg.get("columns")
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if isinstance(v, str) and v.strip():
                schema[str(k)] = v.strip()
    return schema


def _iter_dates(start_date: str, end_date: str) -> list[dt.date]:
    start = dt.date.fromisoformat(start_date[:10])
    end = dt.date.fromisoformat(end_date[:10])
    n_days = (end - start).days
    return [start + dt.timedelta(days=offset) for offset in range(n_days + 1)]


class FileLocator:
    """Date-partitioned Parquet file locator supporting templates and ai-vibe stock layout."""

    def __init__(self, dataset_cfg: dict[str, Any]) -> None:
        self.cfg = dataset_cfg
        self.root = str(Path(str(dataset_cfg.get("root") or "")).expanduser())
        self.locator_mode = str(dataset_cfg.get("locator") or "template").strip().lower()
        self.path_templates: list[str] = [
            str(t) for t in (dataset_cfg.get("path_templates") or []) if isinstance(t, str)
        ]
        self.defaults: dict[str, Any] = dict(dataset_cfg.get("defaults") or {})
        self.symbol_overrides: dict[str, dict[str, Any]] = dict(
            dataset_cfg.get("symbol_overrides") or {}
        )

    def _candidates_ai_vibe_stock(
        self, date: dt.date, data_type: str = "snapshot"
    ) -> list[Path]:
        year = date.strftime("%Y")
        date_str = date.strftime("%Y%m%d")
        if data_type == "trade":
            file_names = [
                f"TradeData_{date_str}.parquet",
                f"tbtTrade_{date_str}.parquet",
            ]
        elif data_type == "order":
            file_names = [
                f"OrderData_{date_str}.parquet",
                f"tbtOrder_{date_str}.parquet",
            ]
        else:
            file_names = [
                f"PubMarketDataProLog_{date_str}.parquet",
                f"MarketDataPro_{date_str}.parquet",
            ]
        base = Path(self.root)
        prefixes = [base / year, base / "HX02" / year]
        return [prefix / date_str / fname for prefix in prefixes for fname in file_names]

    def _candidates_template(self, symbol: str, date: dt.date) -> list[Path]:
        params: dict[str, Any] = {
            **self.defaults,
            **(self.symbol_overrides.get(symbol) or {}),
            "root": self.root,
            "symbol": symbol,
            "year": date.strftime("%Y"),
            "date": date,
            "yyyymmdd": date.strftime("%Y%m%d"),
        }
        candidates: list[Path] = []
        for tmpl in self.path_templates:
            try:
                rendered = tmpl.format(**params)
            except KeyError as exc:
                logger.debug("FileLocator template %r missing key %s", tmpl, exc)
                continue
            candidates.append(Path(rendered).expanduser())
        return candidates

    def locate_day(
        self, symbol: str, date: dt.date, data_type: str = "snapshot"
    ) -> Tuple[Optional[Path], list[str]]:
        if self.locator_mode == "ai_vibe_stock_l2":
            candidates = self._candidates_ai_vibe_stock(date, data_type=data_type)
        else:
            candidates = self._candidates_template(symbol, date)

        attempted = [str(p) for p in candidates]
        for path in candidates:
            if path.is_file():
                return path, attempted
        return None, attempted

    def locate_range(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        data_type: str = "snapshot",
    ) -> list[Tuple[dt.date, Path]]:
        found: list[Tuple[dt.date, Path]] = []
        sample_attempted: list[str] = []

        for day in _iter_dates(start_date, end_date):
            path, attempted = self.locate_day(symbol, day, data_type=data_type)
            if path is not None:
                found.append((day, path))
            else:
                logger.debug("local_parquet: no file for %s on %s", symbol, day)
                if len(sample_attempted) < 6:
                    sample_attempted.extend(attempted[:2])

        if not found:
            ds_name = self.cfg.get("name", "<unnamed>")
            tried_str = ", ".join(sample_attempted[:6]) if sample_attempted else "<no templates>"
            raise FileNotFoundError(
                f"No parquet files found for dataset {ds_name!r}, symbol {symbol!r} "
                f"in [{start_date}, {end_date}]. Tried paths such as: {tried_str}"
            )
        return found


def _convert_tick_series(series: pd.Series, mode: str) -> pd.Series:
    """Convert raw tick/timestamp column to UTC-naive pandas datetime64[ns]."""
    mode_norm = str(mode or "dotnet_tick").strip().lower()
    if mode_norm == "dotnet_tick":
        ticks = pd.to_numeric(series, errors="coerce")
        us = ((ticks - NET_TICK_OFFSET) // 10).astype("float64")
        return pd.to_datetime(us, unit="us", errors="coerce")
    if mode_norm == "unix_s":
        return pd.to_datetime(pd.to_numeric(series, errors="coerce"), unit="s", errors="coerce")
    if mode_norm == "unix_ms":
        return pd.to_datetime(pd.to_numeric(series, errors="coerce"), unit="ms", errors="coerce")
    if mode_norm == "unix_us":
        return pd.to_datetime(pd.to_numeric(series, errors="coerce"), unit="us", errors="coerce")
    parsed = pd.to_datetime(series, errors="coerce", utc=True)
    return parsed.dt.tz_convert(None)


def _read_parquet_day_batch_duckdb(
    file_path: Path,
    *,
    id_col: str | None,
    filter_vals: Sequence[int | str] | None,
    wanted_cols: Sequence[str],
    last_col: str | None = None,
) -> pd.DataFrame:
    """Read a single day's Parquet file for multiple symbols in one DuckDB scan."""
    path_str = str(file_path)
    with duckdb.connect(":memory:") as conn:
        desc_rows = conn.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)", [path_str]
        ).fetchall()
        available_cols = [row[0] for row in desc_rows]
        available_set = set(available_cols)

        if wanted_cols:
            select_cols = [c for c in wanted_cols if c in available_set]
        else:
            select_cols = list(available_cols)

        if not select_cols:
            return pd.DataFrame()

        select_sql = ", ".join(_quote_ident(c) for c in select_cols)
        where_clauses: list[str] = []
        params: list[Any] = [path_str]

        if id_col and id_col in available_set and filter_vals:
            unique_vals = list(dict.fromkeys(filter_vals))
            placeholders = ", ".join("?" for _ in unique_vals)
            where_clauses.append(f"{_quote_ident(id_col)} IN ({placeholders})")
            params.extend(unique_vals)

        if last_col and last_col in available_set:
            where_clauses.append(
                f"{_quote_ident(last_col)} IS NOT NULL AND {_quote_ident(last_col)} > 0"
            )

        sql = f"SELECT {select_sql} FROM read_parquet(?)"
        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        return conn.execute(sql, params).df()


def _read_parquet_day_duckdb(
    file_path: Path,
    *,
    id_col: str | None,
    filter_val: int | str | None,
    wanted_cols: Sequence[str],
    last_col: str | None = None,
) -> pd.DataFrame:
    """Read a single day's Parquet file with DuckDB column projection & predicate pushdown."""
    return _read_parquet_day_batch_duckdb(
        file_path,
        id_col=id_col,
        filter_vals=[filter_val] if filter_val is not None else None,
        wanted_cols=wanted_cols,
        last_col=last_col,
    )


def _detect_level5_columns(
    available_cols: Iterable[str], candidates: Sequence[Sequence[str]]
) -> list[str]:
    avail = set(available_cols)
    for group in candidates:
        present = [c for c in group if c in avail]
        if present:
            return present
    return []


def _compute_incremental_flows(
    df: pd.DataFrame,
    vol_col: str | None,
    turn_col: str | None,
    volume_mode: str,
    symbol: str,
) -> Tuple[pd.Series, pd.Series]:
    """Compute per-tick volume and turnover increments without cross-day negative diffs."""
    n = len(df)
    if vol_col and vol_col in df.columns:
        vol_raw = pd.to_numeric(df[vol_col], errors="coerce").fillna(0.0).astype("float64")
    else:
        vol_raw = pd.Series(0.0, index=df.index, dtype="float64")

    if turn_col and turn_col in df.columns:
        turn_raw = pd.to_numeric(df[turn_col], errors="coerce").fillna(0.0).astype("float64")
    else:
        turn_raw = pd.Series(np.nan, index=df.index, dtype="float64")

    if str(volume_mode or "cumulative").strip().lower() != "cumulative":
        return vol_raw.clip(lower=0.0), turn_raw

    # Group by TradingDay (or calendar date) so cross-day resets never yield negative diffs
    day_key = df["_day_key"]
    dv = vol_raw.groupby(day_key, sort=False).diff()
    first_mask = dv.isna()
    dv = dv.where(~first_mask, vol_raw)

    neg_vol = dv < 0
    if neg_vol.any():
        logger.warning(
            "local_parquet: %d negative cumulative volume diffs for %s; clamping to 0",
            int(neg_vol.sum()),
            symbol,
        )
        dv = dv.clip(lower=0.0)

    if turn_raw.notna().any():
        dt_flow = turn_raw.groupby(day_key, sort=False).diff()
        first_t = dt_flow.isna()
        dt_flow = dt_flow.where(~first_t, turn_raw)
        neg_t = dt_flow < 0
        if neg_t.any():
            logger.warning(
                "local_parquet: %d negative cumulative turnover diffs for %s; clamping to 0",
                int(neg_t.sum()),
                symbol,
            )
            dt_flow = dt_flow.clip(lower=0.0)
    else:
        dt_flow = pd.Series(np.nan, index=df.index, dtype="float64")

    return dv.astype("float64"), dt_flow.astype("float64")


def _aggregate_ticks_to_bars(
    df: pd.DataFrame,
    *,
    schema: dict[str, str],
    interval: str,
    volume_mode: str,
    tick_time_mode: str,
    l2_features: Sequence[str],
    symbol: str,
) -> pd.DataFrame:
    """Aggregate tick/snapshot DataFrame into standard OHLCV+VWAP(+l2:*) bars."""
    if df.empty:
        return pd.DataFrame()

    tick_col = schema["tick"]
    last_col = schema["last"]
    vol_col = schema.get("volume")
    turn_col = schema.get("turnover")
    day_col = schema.get("trading_day")

    work = df.copy()
    work["_ts"] = _convert_tick_series(work[tick_col], tick_time_mode)
    work["_last"] = pd.to_numeric(work[last_col], errors="coerce")
    work = work.dropna(subset=["_ts", "_last"])
    work = work[work["_last"] > 0]
    if work.empty:
        return pd.DataFrame()

    if day_col and day_col in work.columns:
        work["_day_key"] = pd.to_datetime(
            work[day_col].astype(str), format="%Y%m%d", errors="coerce"
        )
        fallback = work["_ts"].dt.normalize()
        work["_day_key"] = work["_day_key"].fillna(fallback)
    else:
        work["_day_key"] = work["_ts"].dt.normalize()

    work = work.sort_values("_ts", kind="mergesort")

    dv, dt_flow = _compute_incremental_flows(work, vol_col, turn_col, volume_mode, symbol)
    work["_dv"] = dv
    work["_dt"] = dt_flow

    # Pre-compute tick-level L2 features if requested
    requested_l2 = [str(f).strip().lower() for f in (l2_features or [])]
    bid_col = schema.get("bid", "Bid")
    ask_col = schema.get("ask", "Ask")
    bv_col = schema.get("bid_vol", "BidVol")
    av_col = schema.get("ask_vol", "AskVol")

    if "spread" in requested_l2 and bid_col in work.columns and ask_col in work.columns:
        bid = pd.to_numeric(work[bid_col], errors="coerce")
        ask = pd.to_numeric(work[ask_col], errors="coerce")
        valid = (bid > 0) & (ask > 0)
        work["l2:spread"] = np.where(valid, ask - bid, np.nan)

    if "obi_1" in requested_l2 and bv_col in work.columns and av_col in work.columns:
        bv = pd.to_numeric(work[bv_col], errors="coerce").fillna(0.0)
        av = pd.to_numeric(work[av_col], errors="coerce").fillna(0.0)
        denom = bv + av
        work["l2:obi_1"] = np.where(denom > 0, (bv - av) / denom, np.nan)

    if "obi_5" in requested_l2:
        bv5_cols = _detect_level5_columns(work.columns, _BID_VOL_5_CANDIDATES)
        av5_cols = _detect_level5_columns(work.columns, _ASK_VOL_5_CANDIDATES)
        if bv5_cols and av5_cols:
            bv5 = work[bv5_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(axis=1)
            av5 = work[av5_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(axis=1)
            denom5 = bv5 + av5
            work["l2:obi_5"] = np.where(denom5 > 0, (bv5 - av5) / denom5, np.nan)

    if "bid_depth" in requested_l2 and bv_col in work.columns:
        work["l2:bid_depth"] = pd.to_numeric(work[bv_col], errors="coerce")

    if "ask_depth" in requested_l2 and av_col in work.columns:
        work["l2:ask_depth"] = pd.to_numeric(work[av_col], errors="coerce")

    is_daily = interval.upper() == "1D"
    if is_daily:
        work["_bucket"] = work["_day_key"]
    else:
        freq = _INTERVAL_TO_FREQ.get(interval)
        if freq is None:
            raise ValueError(
                f"Unsupported interval {interval!r}; expected one of {sorted(_INTERVAL_TO_FREQ)}"
            )
        work["_bucket"] = work["_ts"].dt.floor(freq)

    grouped = work.groupby("_bucket", sort=True)
    agg_dict: dict[str, pd.Series] = {
        "open": grouped["_last"].first(),
        "high": grouped["_last"].max(),
        "low": grouped["_last"].min(),
        "close": grouped["_last"].last(),
        "volume": grouped["_dv"].sum(),
    }

    if work["_dt"].notna().any():
        agg_dict["amount"] = grouped["_dt"].sum()
    else:
        agg_dict["amount"] = agg_dict["close"] * agg_dict["volume"]

    for col in [c for c in work.columns if c.startswith("l2:")]:
        agg_dict[col] = grouped[col].mean()

    bars = pd.DataFrame(agg_dict)
    bars.index.name = "trade_date"

    # Drop zero-volume trading days on intraday bars
    if not is_daily and not bars.empty:
        day_vol = bars.groupby(bars.index.normalize())["volume"].transform("sum")
        bars = bars[day_vol > 0]

    if bars.empty:
        return bars

    vol_pos = bars["volume"] > 0
    bars["vwap"] = np.where(vol_pos, bars["amount"] / bars["volume"], bars["close"])

    # Enforce OHLC invariants and float64 dtypes
    ohlc_checked = validate_ohlc(bars[["open", "high", "low", "close"]])
    for col in ("open", "high", "low", "close"):
        bars[col] = ohlc_checked[col].astype("float64")
    for col in ("volume", "amount", "vwap"):
        bars[col] = bars[col].astype("float64")
    for col in [c for c in bars.columns if c.startswith("l2:")]:
        bars[col] = bars[col].astype("float64")

    ordered_cols = ["open", "high", "low", "close", "volume", "amount", "vwap"] + [
        c for c in bars.columns if c.startswith("l2:")
    ]
    return bars[ordered_cols]


@register
class LocalL2ParquetLoader:
    """Local L2/Parquet dataset loader registered as ``local_parquet``."""

    name = "local_parquet"
    markets = {"a_share", "futures", "us_equity", "hk_equity", "crypto"}
    requires_auth = False

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path

    def _datasets(self) -> dict[str, dict[str, Any]]:
        return load_parquet_datasets(self._config_path)

    def is_available(self) -> bool:
        return len(self._datasets()) > 0

    def _resolve_dataset_for_code(
        self, code: str
    ) -> Tuple[Optional[dict[str, Any]], str]:
        """Resolve ``dataset:symbol`` or bare ``symbol`` to ``(dataset_cfg, clean_symbol)``."""
        datasets = self._datasets()
        clean = code.split(":", 1)[1] if code.startswith("local:") else code

        if ":" in clean:
            ds_name, sym = clean.split(":", 1)
            if ds_name in datasets:
                return datasets[ds_name], sym

        # Match by explicit symbols list or contract_overrides keys
        for ds in datasets.values():
            syms = ds.get("symbols") or []
            overrides = ds.get("contract_overrides") or {}
            if clean in syms or clean in overrides:
                return ds, clean
            if str(ds.get("symbol") or "").strip() == clean:
                return ds, clean

        if len(datasets) == 1:
            return next(iter(datasets.values())), clean

        return None, clean

    def fetch_symbol_from_dataset(
        self,
        dataset_cfg: dict[str, Any],
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        data_type: str = "snapshot",
    ) -> pd.DataFrame:
        """Load and aggregate bars for a single symbol from a dataset config."""
        validate_date_range(start_date, end_date)
        locator = FileLocator(dataset_cfg)
        matched = locator.locate_range(symbol, start_date, end_date, data_type=data_type)

        schema = _resolve_schema(dataset_cfg)
        id_col = schema.get("symbol")
        filter_val = resolve_contract_id(symbol, dataset_cfg.get("contract_id_rule"))
        l2_features = list(dataset_cfg.get("l2_features") or [])

        wanted = [
            schema["trading_day"],
            schema["symbol"],
            schema["tick"],
            schema["open"],
            schema["high"],
            schema["low"],
            schema["last"],
            schema["volume"],
            schema["turnover"],
        ]
        if l2_features:
            wanted.extend([schema["bid"], schema["ask"], schema["bid_vol"], schema["ask_vol"]])
            if "obi_5" in [f.lower() for f in l2_features]:
                for group in _BID_VOL_5_CANDIDATES + _ASK_VOL_5_CANDIDATES:
                    wanted.extend(group)
        # Deduplicate while preserving order
        wanted_unique = list(dict.fromkeys(c for c in wanted if c))

        frames: list[pd.DataFrame] = []
        for _day, file_path in matched:
            day_df = _read_parquet_day_duckdb(
                file_path,
                id_col=id_col,
                filter_val=filter_val,
                wanted_cols=wanted_unique,
                last_col=schema["last"],
            )
            if not day_df.empty:
                frames.append(day_df)

        if not frames:
            return pd.DataFrame()

        combined = pd.concat(frames, ignore_index=True)
        return _aggregate_ticks_to_bars(
            combined,
            schema=schema,
            interval=interval,
            volume_mode=str(dataset_cfg.get("volume_mode") or "cumulative"),
            tick_time_mode=str(dataset_cfg.get("tick_time_mode") or "dotnet_tick"),
            l2_features=l2_features,
            symbol=symbol,
        )

    def fetch_raw_l2(
        self,
        dataset_cfg: dict[str, Any] | str,
        symbol: str,
        date: str,
        *,
        data_type: str = "snapshot",
        columns: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """Read raw L2 snapshot/trade/order rows for a single symbol and trading date."""
        if isinstance(dataset_cfg, str):
            ds = self._datasets().get(dataset_cfg)
            if ds is None:
                raise KeyError(f"Unknown local parquet dataset {dataset_cfg!r}")
            dataset_cfg = ds

        day = dt.date.fromisoformat(date[:10])
        locator = FileLocator(dataset_cfg)
        file_path, attempted = locator.locate_day(symbol, day, data_type=data_type)
        if file_path is None:
            raise FileNotFoundError(
                f"No {data_type} parquet file for {symbol!r} on {date}. Tried: {attempted[:4]}"
            )

        schema = _resolve_schema(dataset_cfg)
        id_col = schema.get("symbol")
        filter_val = resolve_contract_id(symbol, dataset_cfg.get("contract_id_rule"))
        df = _read_parquet_day_duckdb(
            file_path,
            id_col=id_col,
            filter_val=filter_val,
            wanted_cols=list(columns) if columns else [],
            last_col=None,
        )
        tick_col = schema.get("tick")
        if not df.empty and tick_col and tick_col in df.columns:
            df["timestamp"] = _convert_tick_series(
                df[tick_col], str(dataset_cfg.get("tick_time_mode") or "dotnet_tick")
            )
        return df

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        """BaseDataLoader-compatible fetch across configured local parquet datasets."""
        validate_date_range(start_date, end_date)
        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            ds, clean_sym = self._resolve_dataset_for_code(code)
            if ds is None:
                logger.warning("local_parquet: no dataset configured for %s", code)
                continue
            df = self.fetch_symbol_from_dataset(
                ds, clean_sym, start_date, end_date, interval=interval
            )
            if not df.empty:
                result[clean_sym] = df
        return result

    def build_panel(
        self,
        dataset: dict[str, Any] | str,
        *,
        symbols: Sequence[str] | None = None,
        start_date: str,
        end_date: str,
        interval: str = "1D",
        data_type: str = "snapshot",
    ) -> dict[str, Any]:
        """Build a wide ``{field: DataFrame(index=DatetimeIndex, columns=symbols)}`` panel.

        Symbols that resolve to the same daily Parquet file (e.g. full-market A-share
        or exchange-wide futures snapshots) are scanned together in a single DuckDB
        ``IN (...)`` pass per day instead of re-reading the file once per symbol.
        """
        validate_date_range(start_date, end_date)
        if isinstance(dataset, str):
            ds = self._datasets().get(dataset)
            if ds is None:
                raise KeyError(f"Unknown local parquet dataset {dataset!r}")
            dataset_cfg = ds
        else:
            dataset_cfg = dataset

        sym_list = list(symbols) if symbols is not None else list(dataset_cfg.get("symbols") or [])
        if not sym_list:
            raise ValueError(
                f"Dataset {dataset_cfg.get('name')!r} has no symbols configured; "
                "set `symbols` in config or pass symbols explicitly."
            )

        schema = _resolve_schema(dataset_cfg)
        id_col = schema.get("symbol")
        l2_features = list(dataset_cfg.get("l2_features") or [])
        volume_mode = str(dataset_cfg.get("volume_mode") or "cumulative")
        tick_time_mode = str(dataset_cfg.get("tick_time_mode") or "dotnet_tick")

        wanted = [
            schema["trading_day"],
            schema["symbol"],
            schema["tick"],
            schema["open"],
            schema["high"],
            schema["low"],
            schema["last"],
            schema["volume"],
            schema["turnover"],
        ]
        if l2_features:
            wanted.extend([schema["bid"], schema["ask"], schema["bid_vol"], schema["ask_vol"]])
            if "obi_5" in [f.lower() for f in l2_features]:
                for group in _BID_VOL_5_CANDIDATES + _ASK_VOL_5_CANDIDATES:
                    wanted.extend(group)
        wanted_unique = list(dict.fromkeys(c for c in wanted if c))

        locator = FileLocator(dataset_cfg)
        contract_rule = dataset_cfg.get("contract_id_rule")

        # Group requested symbols by unique daily file path so each shared file is read once
        file_to_sym_vals: dict[Path, list[tuple[str, int | str]]] = {}
        for sym in sym_list:
            matched = locator.locate_range(sym, start_date, end_date, data_type=data_type)
            fval = resolve_contract_id(sym, contract_rule)
            for _day, fpath in matched:
                file_to_sym_vals.setdefault(fpath, []).append((sym, fval))

        raw_frames_by_sym: dict[str, list[pd.DataFrame]] = {sym: [] for sym in sym_list}
        for fpath, sym_val_pairs in file_to_sym_vals.items():
            filter_vals = [fv for _, fv in sym_val_pairs]
            day_df = _read_parquet_day_batch_duckdb(
                fpath,
                id_col=id_col,
                filter_vals=filter_vals,
                wanted_cols=wanted_unique,
                last_col=schema["last"],
            )
            if day_df.empty:
                continue

            if id_col and id_col in day_df.columns:
                val_to_syms: dict[Any, list[str]] = {}
                for sym, fv in sym_val_pairs:
                    val_to_syms.setdefault(fv, []).append(sym)
                for fv, grp in day_df.groupby(id_col, sort=False):
                    target_syms = val_to_syms.get(fv)
                    if target_syms is None:
                        target_syms = val_to_syms.get(str(fv))
                    if not target_syms:
                        continue
                    for sym in target_syms:
                        raw_frames_by_sym[sym].append(grp)
            else:
                for sym, _ in sym_val_pairs:
                    raw_frames_by_sym[sym].append(day_df)

        per_symbol: dict[str, pd.DataFrame] = {}
        all_cols: list[str] = []
        for sym in sym_list:
            frames = raw_frames_by_sym[sym]
            if not frames:
                continue
            combined = pd.concat(frames, ignore_index=True)
            df = _aggregate_ticks_to_bars(
                combined,
                schema=schema,
                interval=interval,
                volume_mode=volume_mode,
                tick_time_mode=tick_time_mode,
                l2_features=l2_features,
                symbol=sym,
            )
            if not df.empty:
                per_symbol[sym] = df
                for c in df.columns:
                    if c not in all_cols:
                        all_cols.append(c)

        if not per_symbol:
            return {}

        panel: dict[str, Any] = {}
        ordered_syms = [s for s in sym_list if s in per_symbol]
        for col in all_cols:
            series_map = {
                sym: per_symbol[sym][col]
                for sym in ordered_syms
                if col in per_symbol[sym].columns
            }
            if series_map:
                wide = pd.DataFrame(series_map).sort_index()
                wide.index.name = "trade_date"
                panel[col] = wide

        ds_name = str(dataset_cfg.get("name") or "local_parquet")
        panel["meta"] = {
            "universe": f"local:{ds_name}",
            "dataset": ds_name,
            "asset_class": dataset_cfg.get("asset_class", "unknown"),
            "interval": interval,
            "n_symbols": len(ordered_syms),
            "start_date": start_date,
            "end_date": end_date,
        }
        return panel
