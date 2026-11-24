"""TDD tests for local L2/Parquet dataset loader, local_loader integration,
ChinaFuturesEngine contract_overrides, and alpha_bench_tool local universe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml

NET_TICK_OFFSET = 621355968000000000


def _to_dotnet_tick(dt_str: str) -> int:
    """Convert an ISO-like naive timestamp string to .NET ticks (100ns since 0001-01-01)."""
    ts = pd.Timestamp(dt_str)
    # Timestamp.value is nanoseconds since 1970-01-01; 1 .NET tick = 100 ns
    return NET_TICK_OFFSET + int(ts.value // 100)


def _write_futures_l2_day(
    path: Path,
    trading_day: str,
    symbol: str,
    ticks: list[tuple[str, float, float, float, float, float, float, float]],
) -> None:
    """Write a synthetic futures L2 parquet file.

    Each tick tuple is:
        (time_str, last, cum_vol, cum_turnover, bid1, ask1, bid_vol1, ask_vol1)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    open_px = ticks[0][1] if ticks else 0.0
    high_px = 0.0
    low_px = float("inf")
    for time_str, last, vol, turn, bid1, ask1, bv1, av1 in ticks:
        if last > 0:
            high_px = max(high_px, last)
            low_px = min(low_px, last)
        rows.append(
            {
                "TradingDay": int(trading_day),
                "Symbol": symbol,
                "ExchTick": _to_dotnet_tick(f"{trading_day[:4]}-{trading_day[4:6]}-{trading_day[6:]} {time_str}"),
                "Open": open_px,
                "Highest": high_px if high_px > 0 else 0.0,
                "Lowest": low_px if low_px < float("inf") else 0.0,
                "Last": last,
                "Volume": vol,
                "Turnover": turn,
                "Bid": bid1,
                "Ask": ask1,
                "BidVol": bv1,
                "AskVol": av1,
                "Bid2": bid1 - 0.2 if bid1 > 0 else 0.0,
                "Ask2": ask1 + 0.2 if ask1 > 0 else 0.0,
                "BidVol2": bv1 * 2,
                "AskVol2": av1 * 0.5,
            }
        )
    pd.DataFrame(rows).to_parquet(path, index=False)


def _write_stock_l2_day(
    path: Path,
    trading_day: str,
    contract_id: int,
    ticks: list[tuple[str, float, float, float, float, float, float, float]],
    extra_rows: list[dict[str, Any]] | None = None,
) -> None:
    """Write a synthetic A-share L2 snapshot parquet file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    open_px = ticks[0][1] if ticks else 0.0
    high_px = 0.0
    low_px = float("inf")
    for time_str, last, vol, turn, bid1, ask1, bv1, av1 in ticks:
        if last > 0:
            high_px = max(high_px, last)
            low_px = min(low_px, last)
        rows.append(
            {
                "TradingDay": int(trading_day),
                "ContractId": contract_id,
                "ExchTick": _to_dotnet_tick(f"{trading_day[:4]}-{trading_day[4:6]}-{trading_day[6:]} {time_str}"),
                "Open": open_px,
                "Highest": high_px if high_px > 0 else 0.0,
                "Lowest": low_px if low_px < float("inf") else 0.0,
                "Last": last,
                "Volume": vol,
                "Turnover": turn,
                "Bid": bid1,
                "Ask": ask1,
                "BidVol": bv1,
                "AskVol": av1,
            }
        )
    if extra_rows:
        rows.extend(extra_rows)
    pd.DataFrame(rows).to_parquet(path, index=False)


# ---------------------------------------------------------------------------
# 1. File locator & contract ID resolution
# ---------------------------------------------------------------------------


def test_resolve_cn_stock_contract_id():
    from backtest.loaders.local_l2_parquet_loader import resolve_contract_id

    assert resolve_contract_id("600519", "cn_stock_prefix") == 11600519
    assert resolve_contract_id("600519.SH", "cn_stock_prefix") == 11600519
    assert resolve_contract_id("000001", "cn_stock_prefix") == 21000001
    assert resolve_contract_id("000001.SZ", "cn_stock_prefix") == 21000001
    assert resolve_contract_id("300750.SZ", "cn_stock_prefix") == 21300750
    assert resolve_contract_id("ZX01", None) == "ZX01"


def test_file_locator_futures_template_and_missing_day(tmp_path: Path):
    from backtest.loaders.local_l2_parquet_loader import FileLocator

    day1 = tmp_path / "CFFEX" / "ZX01" / "2026" / "MarketDataProLv2_20260821.parquet"
    day3 = tmp_path / "CFFEX" / "ZX01" / "2026" / "MarketDataProLv2_20260823.parquet"
    _write_futures_l2_day(day1, "20260821", "ZX01", [("09:30:05", 4000.0, 10, 40000.0, 3999.8, 4000.2, 5, 5)])
    _write_futures_l2_day(day3, "20260823", "ZX01", [("09:30:05", 4010.0, 12, 48120.0, 4009.8, 4010.2, 6, 4)])

    cfg = {
        "name": "local_cffex_l2",
        "root": str(tmp_path),
        "path_templates": [
            "{root}/{exchange}/{machine}/{year}/{prefix}_{date:%Y%m%d}.parquet",
        ],
        "defaults": {"exchange": "CFFEX", "machine": "ZX01", "prefix": "MarketDataProLv2"},
    }
    locator = FileLocator(cfg)
    found = locator.locate_range("ZX01", "2026-08-21", "2026-08-23")
    assert len(found) == 2
    assert found[0][1] == day1
    assert found[1][1] == day3


def test_file_locator_ai_vibe_stock_hx02_fallback(tmp_path: Path):
    from backtest.loaders.local_l2_parquet_loader import FileLocator

    # Day 1 in regular year dir with MarketDataPro_
    day1 = tmp_path / "2026" / "20260821" / "MarketDataPro_20260821.parquet"
    # Day 2 in HX02 fallback dir with PubMarketDataProLog_
    day2 = tmp_path / "HX02" / "2026" / "20260822" / "PubMarketDataProLog_20260822.parquet"
    _write_stock_l2_day(day1, "20260821", 21000001, [("09:30:03", 10.0, 100, 1000.0, 9.99, 10.01, 50, 50)])
    _write_stock_l2_day(day2, "20260822", 21000001, [("09:30:03", 10.2, 200, 2040.0, 10.19, 10.21, 60, 40)])

    cfg = {
        "name": "local_cn_stock_l2",
        "root": str(tmp_path),
        "locator": "ai_vibe_stock_l2",
        "contract_id_rule": "cn_stock_prefix",
    }
    locator = FileLocator(cfg)
    found = locator.locate_range("000001.SZ", "2026-08-21", "2026-08-22")
    assert [p for _, p in found] == [day1, day2]


def test_file_locator_raises_file_not_found_when_zero_files(tmp_path: Path):
    from backtest.loaders.local_l2_parquet_loader import FileLocator

    cfg = {
        "name": "empty_ds",
        "root": str(tmp_path),
        "path_templates": ["{root}/{exchange}/{year}/Mkt_{date:%Y%m%d}.parquet"],
        "defaults": {"exchange": "CFFEX"},
    }
    locator = FileLocator(cfg)
    with pytest.raises(FileNotFoundError, match="Mkt_"):
        locator.locate_range("ZX01", "2026-08-21", "2026-08-22")


# ---------------------------------------------------------------------------
# 2. Bar aggregation, cumulative volume diff, VWAP, dirty ticks & L2 features
# ---------------------------------------------------------------------------


def test_intraday_1m_aggregation_cumulative_diff_and_l2_features(tmp_path: Path):
    from backtest.loaders.local_l2_parquet_loader import LocalL2ParquetLoader

    day1 = tmp_path / "CFFEX" / "ZX01" / "2026" / "MarketDataProLv2_20260821.parquet"
    day2 = tmp_path / "CFFEX" / "ZX01" / "2026" / "MarketDataProLv2_20260822.parquet"
    day3_zero = tmp_path / "CFFEX" / "ZX01" / "2026" / "MarketDataProLv2_20260823.parquet"

    # Day 1: 2 minute buckets + 1 dirty tick (Last <= 0)
    _write_futures_l2_day(
        day1,
        "20260821",
        "ZX01",
        [
            ("09:30:05", 0.0, 0, 0.0, 0.0, 0.0, 0, 0),  # dirty tick -> filtered
            ("09:30:10", 4000.0, 10, 40000.0, 3999.5, 4000.5, 30, 10),
            ("09:30:40", 4004.0, 25, 100060.0, 4003.5, 4004.5, 20, 20),
            ("09:31:15", 4002.0, 40, 160090.0, 4001.5, 4002.5, 10, 30),
        ],
    )
    # Day 2: cumulative volume resets to 5 (must NOT become negative vs Day 1's 40)
    _write_futures_l2_day(
        day2,
        "20260822",
        "ZX01",
        [
            ("09:30:10", 4010.0, 5, 20050.0, 4009.5, 4010.5, 15, 5),
            ("09:30:50", 4012.0, 15, 60170.0, 4011.5, 4012.5, 10, 10),
        ],
    )
    # Day 3: zero-volume junk day -> should be dropped for intraday bars
    _write_futures_l2_day(
        day3_zero,
        "20260823",
        "ZX01",
        [
            ("09:30:10", 4015.0, 0, 0.0, 4014.5, 4015.5, 5, 5),
        ],
    )

    cfg = {
        "name": "local_cffex_l2",
        "asset_class": "china_futures",
        "root": str(tmp_path),
        "path_templates": ["{root}/{exchange}/{machine}/{year}/{prefix}_{date:%Y%m%d}.parquet"],
        "defaults": {"exchange": "CFFEX", "machine": "ZX01", "prefix": "MarketDataProLv2"},
        "schema_profile": "l2_futures_pro",
        "volume_mode": "cumulative",
        "tick_time_mode": "dotnet_tick",
        "l2_features": ["spread", "obi_1", "obi_5"],
    }

    loader = LocalL2ParquetLoader()
    df = loader.fetch_symbol_from_dataset(cfg, "ZX01", "2026-08-21", "2026-08-23", interval="1m")

    assert isinstance(df.index, pd.DatetimeIndex)
    # Day 3 (zero volume) dropped; Day 1 has 2 bars, Day 2 has 1 bar -> 3 bars total
    assert len(df) == 3
    assert (df["volume"] >= 0).all()

    # Check Day 1 bar 0 (09:30)
    bar0 = df.iloc[0]
    assert bar0["open"] == pytest.approx(4000.0)
    assert bar0["high"] == pytest.approx(4004.0)
    assert bar0["low"] == pytest.approx(4000.0)
    assert bar0["close"] == pytest.approx(4004.0)
    assert bar0["volume"] == pytest.approx(25.0)
    assert bar0["amount"] == pytest.approx(100060.0)
    assert bar0["vwap"] == pytest.approx(100060.0 / 25.0)
    assert bar0["l2:spread"] == pytest.approx(1.0)
    # tick1 obi_1 = (30-10)/40 = 0.5, tick2 obi_1 = (20-20)/40 = 0.0 -> mean 0.25
    assert bar0["l2:obi_1"] == pytest.approx(0.25)
    assert "l2:obi_5" in df.columns

    # Check Day 1 bar 1 (09:31): single tick gets diff 40 - 25 = 15
    bar1 = df.iloc[1]
    assert bar1["volume"] == pytest.approx(15.0)
    assert bar1["amount"] == pytest.approx(60030.0)

    # Check Day 2 bar 0 (09:30): reset to 15, NOT negative
    bar2 = df.iloc[2]
    assert bar2["volume"] == pytest.approx(15.0)
    assert bar2["open"] == pytest.approx(4010.0)
    assert bar2["close"] == pytest.approx(4012.0)


def test_daily_1d_aggregation_and_raw_l2_fetch(tmp_path: Path):
    from backtest.loaders.local_l2_parquet_loader import LocalL2ParquetLoader

    day1 = tmp_path / "2026" / "20260821" / "MarketDataPro_20260821.parquet"
    # Include another ContractId to verify predicate pushdown filters it out
    other_row = {
        "TradingDay": 20260821,
        "ContractId": 21000002,
        "ExchTick": _to_dotnet_tick("2026-08-21 09:30:05"),
        "Open": 99.0,
        "Highest": 99.0,
        "Lowest": 99.0,
        "Last": 99.0,
        "Volume": 9999.0,
        "Turnover": 999900.0,
        "Bid": 98.9,
        "Ask": 99.1,
        "BidVol": 100.0,
        "AskVol": 100.0,
    }
    _write_stock_l2_day(
        day1,
        "20260821",
        21000001,
        [
            ("09:30:03", 10.0, 100, 1000.0, 9.99, 10.01, 50, 50),
            ("14:59:57", 10.5, 500, 5150.0, 10.49, 10.51, 80, 20),
        ],
        extra_rows=[other_row],
    )

    cfg = {
        "name": "local_cn_stock_l2",
        "asset_class": "china_a",
        "root": str(tmp_path),
        "locator": "ai_vibe_stock_l2",
        "contract_id_rule": "cn_stock_prefix",
        "schema_profile": "l2_stock_snapshot",
        "volume_mode": "cumulative",
        "tick_time_mode": "dotnet_tick",
        "l2_features": ["spread", "obi_1"],
    }

    loader = LocalL2ParquetLoader()
    df = loader.fetch_symbol_from_dataset(cfg, "000001.SZ", "2026-08-21", "2026-08-21", interval="1D")
    assert len(df) == 1
    assert df.index[0] == pd.Timestamp("2026-08-21")
    assert df.iloc[0]["open"] == pytest.approx(10.0)
    assert df.iloc[0]["high"] == pytest.approx(10.5)
    assert df.iloc[0]["low"] == pytest.approx(10.0)
    assert df.iloc[0]["close"] == pytest.approx(10.5)
    assert df.iloc[0]["volume"] == pytest.approx(500.0)
    assert df.iloc[0]["vwap"] == pytest.approx(5150.0 / 500.0)

    raw = loader.fetch_raw_l2(cfg, "000001.SZ", "2026-08-21", data_type="snapshot")
    assert len(raw) == 2
    assert set(raw["ContractId"].unique()) == {21000001}


# ---------------------------------------------------------------------------
# 3. Registry.compute() compatibility with built-in alpha & L2 custom alpha
# ---------------------------------------------------------------------------


def test_panel_compatible_with_factor_registry(tmp_path: Path):
    from backtest.loaders.local_l2_parquet_loader import LocalL2ParquetLoader
    from src.factors.registry import Registry

    symbols = ["000001.SZ", "000002.SZ", "600519.SH"]
    dates = pd.bdate_range("2026-08-03", periods=10)
    for d_idx, dt in enumerate(dates):
        ds = dt.strftime("%Y%m%d")
        f = tmp_path / dt.strftime("%Y") / ds / f"MarketDataPro_{ds}.parquet"
        rows = []
        for s_idx, sym in enumerate(symbols):
            cid = 11600519 if sym.startswith("6") else int(f"21{sym[:6]}")
            px = 10.0 + s_idx * 5.0 + d_idx * (0.2 + 0.05 * s_idx)
            rows.append(
                {
                    "TradingDay": int(ds),
                    "ContractId": cid,
                    "ExchTick": _to_dotnet_tick(f"{dt.strftime('%Y-%m-%d')} 14:55:00"),
                    "Open": px - 0.1,
                    "Highest": px + 0.2,
                    "Lowest": px - 0.2,
                    "Last": px,
                    "Volume": 1000.0 + d_idx * 50.0,
                    "Turnover": px * (1000.0 + d_idx * 50.0),
                    "Bid": px - 0.01,
                    "Ask": px + 0.01,
                    "BidVol": 100.0 + s_idx * 20.0,
                    "AskVol": 80.0,
                }
            )
        f.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(f, index=False)

    cfg = {
        "name": "local_cn_stock_l2",
        "asset_class": "china_a",
        "root": str(tmp_path),
        "locator": "ai_vibe_stock_l2",
        "contract_id_rule": "cn_stock_prefix",
        "schema_profile": "l2_stock_snapshot",
        "symbols": symbols,
        "l2_features": ["spread", "obi_1"],
    }
    loader = LocalL2ParquetLoader()
    panel = loader.build_panel(cfg, symbols=symbols, start_date="2026-08-03", end_date="2026-08-14", interval="1D")

    assert {"open", "high", "low", "close", "volume", "amount", "vwap", "l2:spread", "l2:obi_1"}.issubset(panel.keys())
    assert list(panel["close"].columns) == symbols
    assert len(panel["close"]) == 10

    # Run an actual built-in factor from Registry (qlib158_kmid has min_warmup_bars=1)
    reg = Registry()
    out = reg.compute("qlib158_kmid", panel)
    assert out.shape == (10, 3)
    assert out.notna().any().any()


# ---------------------------------------------------------------------------
# 4. Integration with local_loader.py & env var config override
# ---------------------------------------------------------------------------


def test_local_loader_routes_parquet_dataset_via_env_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from backtest.loaders.local_loader import DataLoader as LocalDataLoader

    day1 = tmp_path / "CFFEX" / "ZX01" / "2026" / "MarketDataProLv2_20260821.parquet"
    _write_futures_l2_day(
        day1,
        "20260821",
        "ZX01",
        [
            ("09:30:10", 4000.0, 10, 40000.0, 3999.8, 4000.2, 5, 5),
            ("14:59:50", 4020.0, 30, 120400.0, 4019.8, 4020.2, 8, 4),
        ],
    )
    cfg_path = tmp_path / "local_data.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "datasets": [
                    {
                        "name": "local_cffex_l2",
                        "asset_class": "china_futures",
                        "root": str(tmp_path),
                        "path_templates": [
                            "{root}/{exchange}/{machine}/{year}/{prefix}_{date:%Y%m%d}.parquet"
                        ],
                        "defaults": {"exchange": "CFFEX", "machine": "ZX01", "prefix": "MarketDataProLv2"},
                        "symbols": ["ZX01"],
                        "contract_overrides": {
                            "ZX01": {
                                "multiplier": 200,
                                "margin_rate": 0.12,
                                "price_limit": 0.10,
                                "commission": ["rate", 0.000023],
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_TRADING_LOCAL_DATA_CONFIG", str(cfg_path))

    loader = LocalDataLoader()
    assert loader.is_available() is True
    res = loader.fetch(["local:ZX01"], "2026-08-21", "2026-08-21", interval="1D")
    assert "ZX01" in res
    assert res["ZX01"].iloc[0]["close"] == pytest.approx(4020.0)


# ---------------------------------------------------------------------------
# 5. End-to-end smoke test: ChinaFuturesEngine with contract_overrides
# ---------------------------------------------------------------------------


def test_china_futures_engine_e2e_smoke_with_contract_overrides(tmp_path: Path):
    from backtest.engines.china_futures import ChinaFuturesEngine
    from backtest.loaders.local_l2_parquet_loader import LocalL2ParquetLoader

    dates = pd.bdate_range("2026-08-03", periods=6)
    prices = [4000.0, 4020.0, 4050.0, 4080.0, 4060.0, 4100.0]
    for dt, px in zip(dates, prices):
        ds = dt.strftime("%Y%m%d")
        f = tmp_path / "CFFEX" / "ZX01" / "2026" / f"MarketDataProLv2_{ds}.parquet"
        _write_futures_l2_day(
            f,
            ds,
            "ZX01",
            [
                ("09:30:10", px - 5.0, 10, (px - 5.0) * 10, px - 5.2, px - 4.8, 10, 10),
                ("14:59:50", px, 50, px * 50, px - 0.2, px + 0.2, 12, 8),
            ],
        )

    dataset_cfg = {
        "name": "local_cffex_l2",
        "asset_class": "china_futures",
        "root": str(tmp_path),
        "path_templates": ["{root}/{exchange}/{machine}/{year}/{prefix}_{date:%Y%m%d}.parquet"],
        "defaults": {"exchange": "CFFEX", "machine": "ZX01", "prefix": "MarketDataProLv2"},
        "contract_overrides": {
            "ZX01": {
                "multiplier": 200,
                "margin_rate": 0.12,
                "price_limit": 0.10,
                "commission": ["rate", 0.000023],
            }
        },
    }
    cfg_path = tmp_path / "futures_cfg.yaml"
    cfg_path.write_text(yaml.safe_dump({"datasets": [dataset_cfg]}), encoding="utf-8")
    loader = LocalL2ParquetLoader(config_path=cfg_path)
    df = loader.fetch_symbol_from_dataset(dataset_cfg, "ZX01", "2026-08-03", "2026-08-10", interval="1D")
    assert len(df) == 6

    bt_config = {
        "codes": ["ZX01"],
        "start_date": "2026-08-03",
        "end_date": "2026-08-10",
        "interval": "1D",
        "initial_capital": 1_000_000.0,
        "contract_overrides": dataset_cfg["contract_overrides"],
    }
    engine = ChinaFuturesEngine(bt_config)
    assert engine.get_contract_multiplier("ZX01") == pytest.approx(200.0)
    assert engine.get_margin_rate("ZX01") == pytest.approx(0.12)
    # Commission for 1 lot at 4000 with multiplier 200 and rate 0.000023 = 18.4
    assert engine.calc_commission_for_symbol("ZX01", 1.0, 4000.0, True) == pytest.approx(18.4)

    class _SimpleLongSignal:
        def generate(self, data: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
            idx = data["ZX01"].index
            return {"ZX01": pd.Series([1.0, 1.0, 1.0, 1.0, 0.0, 0.0], index=idx)}

    result = engine.run_backtest(bt_config, loader, _SimpleLongSignal(), tmp_path / "run")
    assert isinstance(result, dict)
    assert result["trade_count"] >= 1
    assert result["final_value"] > 1_000_000.0
    assert (tmp_path / "run" / "artifacts").is_dir()


# ---------------------------------------------------------------------------
# 6. alpha_bench_tool local:<dataset_name> universe integration
# ---------------------------------------------------------------------------


def test_alpha_bench_local_universe_panel_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src.tools.alpha_bench_tool import _load_universe_panel

    symbols = ["000001.SZ", "000002.SZ", "000004.SZ", "600000.SH", "600519.SH"]
    dates = pd.bdate_range("2026-08-03", periods=5)
    for d_idx, dt in enumerate(dates):
        ds = dt.strftime("%Y%m%d")
        f = tmp_path / dt.strftime("%Y") / ds / f"MarketDataPro_{ds}.parquet"
        rows = []
        for s_idx, sym in enumerate(symbols):
            cid = int(f"11{sym[:6]}") if sym.startswith("6") else int(f"21{sym[:6]}")
            px = 20.0 + s_idx + d_idx * 0.5
            rows.append(
                {
                    "TradingDay": int(ds),
                    "ContractId": cid,
                    "ExchTick": _to_dotnet_tick(f"{dt.strftime('%Y-%m-%d')} 14:55:00"),
                    "Open": px - 0.1,
                    "Highest": px + 0.1,
                    "Lowest": px - 0.1,
                    "Last": px,
                    "Volume": 1000.0,
                    "Turnover": px * 1000.0,
                    "Bid": px - 0.01,
                    "Ask": px + 0.01,
                    "BidVol": 50.0,
                    "AskVol": 50.0,
                }
            )
        f.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(f, index=False)

    cfg_path = tmp_path / "local_data.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "datasets": [
                    {
                        "name": "my_cn_l2",
                        "asset_class": "china_a",
                        "root": str(tmp_path),
                        "locator": "ai_vibe_stock_l2",
                        "contract_id_rule": "cn_stock_prefix",
                        "symbols": symbols,
                        "l2_features": ["spread", "obi_1"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("VIBE_TRADING_LOCAL_DATA_CONFIG", str(cfg_path))

    panel = _load_universe_panel("local:my_cn_l2", "2026-08-03/2026-08-07", use_cache=False)
    assert "close" in panel
    assert panel["close"].shape == (5, 5)
    assert "l2:spread" in panel
    assert panel["meta"]["universe"] == "local:my_cn_l2"


def test_build_panel_batches_shared_daily_files_in_single_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """5 symbols across 3 shared daily Parquet files must trigger only 3 file reads, not 15."""
    import backtest.loaders.local_l2_parquet_loader as mod

    symbols = ["000001.SZ", "000002.SZ", "000004.SZ", "600000.SH", "600519.SH"]
    dates = pd.bdate_range("2026-08-03", periods=3)
    for d_idx, dt in enumerate(dates):
        ds = dt.strftime("%Y%m%d")
        f = tmp_path / dt.strftime("%Y") / ds / f"MarketDataPro_{ds}.parquet"
        rows = []
        for s_idx, sym in enumerate(symbols):
            cid = int(f"11{sym[:6]}") if sym.startswith("6") else int(f"21{sym[:6]}")
            px = 15.0 + s_idx * 2.0 + d_idx * 0.5
            rows.append(
                {
                    "TradingDay": int(ds),
                    "ContractId": cid,
                    "ExchTick": _to_dotnet_tick(f"{dt.strftime('%Y-%m-%d')} 09:35:00"),
                    "Open": px - 0.1,
                    "Highest": px + 0.2,
                    "Lowest": px - 0.2,
                    "Last": px,
                    "Volume": 100.0 + d_idx * 10.0,
                    "Turnover": px * (100.0 + d_idx * 10.0),
                    "Bid": px - 0.01,
                    "Ask": px + 0.01,
                    "BidVol": 60.0 + s_idx * 5.0,
                    "AskVol": 40.0,
                }
            )
            rows.append(
                {
                    "TradingDay": int(ds),
                    "ContractId": cid,
                    "ExchTick": _to_dotnet_tick(f"{dt.strftime('%Y-%m-%d')} 14:55:00"),
                    "Open": px - 0.1,
                    "Highest": px + 0.3,
                    "Lowest": px - 0.2,
                    "Last": px + 0.2,
                    "Volume": 250.0 + d_idx * 20.0,
                    "Turnover": (px + 0.2) * (250.0 + d_idx * 20.0),
                    "Bid": px + 0.19,
                    "Ask": px + 0.21,
                    "BidVol": 80.0,
                    "AskVol": 20.0,
                }
            )
        f.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_parquet(f, index=False)

    cfg = {
        "name": "batch_cn_l2",
        "asset_class": "china_a",
        "root": str(tmp_path),
        "locator": "ai_vibe_stock_l2",
        "contract_id_rule": "cn_stock_prefix",
        "schema_profile": "l2_stock_snapshot",
        "symbols": symbols,
        "l2_features": ["spread", "obi_1"],
    }

    scan_calls: list[Path] = []
    orig_batch_reader = mod._read_parquet_day_batch_duckdb

    def _counting_batch_reader(file_path: Path, **kwargs: Any) -> pd.DataFrame:
        scan_calls.append(file_path)
        return orig_batch_reader(file_path, **kwargs)

    monkeypatch.setattr(mod, "_read_parquet_day_batch_duckdb", _counting_batch_reader)

    loader = mod.LocalL2ParquetLoader()
    panel = loader.build_panel(
        cfg, symbols=symbols, start_date="2026-08-03", end_date="2026-08-05", interval="1D"
    )

    # Exactly 3 daily parquet files scanned (once per day), NOT 5 * 3 = 15
    assert len(scan_calls) == 3
    assert panel["close"].shape == (3, 5)
    assert list(panel["close"].columns) == symbols
    assert "l2:spread" in panel and "l2:obi_1" in panel

    # Verify numerical parity with single-symbol fetch
    single = loader.fetch_symbol_from_dataset(
        cfg, "600519.SH", "2026-08-03", "2026-08-05", interval="1D"
    )
    pd.testing.assert_series_equal(
        panel["close"]["600519.SH"], single["close"], check_names=False
    )
    pd.testing.assert_series_equal(
        panel["volume"]["600519.SH"], single["volume"], check_names=False
    )
    pd.testing.assert_series_equal(
        panel["l2:obi_1"]["600519.SH"], single["l2:obi_1"], check_names=False
    )
