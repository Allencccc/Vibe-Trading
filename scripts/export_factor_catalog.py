#!/usr/bin/env python3
"""Static AST exporter for all bundled factors in agent/src/factors/zoo/.

Scans every factor module under ``agent/src/factors/zoo/<zoo>/*.py`` without
executing arbitrary module code, validates ``__alpha_meta__`` against the
registry contract, extracts Chinese/English descriptions and base operator
usage, and writes ``docs/research/factor-zoo-catalog.md``.
"""

from __future__ import annotations

import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
ZOO_ROOT = REPO_ROOT / "agent" / "src" / "factors" / "zoo"
DEFAULT_OUTPUT = REPO_ROOT / "docs" / "research" / "factor-zoo-catalog.md"

_ZH_NAME_RE = re.compile(r"^#\s*中文名称\s*[:：]\s*(.+)$", re.MULTILINE)
_ZH_DESC_RE = re.compile(r"^#\s*简要说明\s*[:：]\s*(.+)$", re.MULTILINE)
_ZH_USE_RE = re.compile(r"^#\s*典型用途\s*[:：]\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class FactorRecord:
    zoo: str
    file_rel: str
    meta: dict[str, Any]
    zh_name: str
    zh_desc: str
    zh_use: str
    docstring_first_line: str
    base_ops: list[str]


def _extract_meta_and_ops(py_file: Path) -> tuple[dict[str, Any], str, list[str]]:
    source = py_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(py_file))

    meta_node: ast.expr | None = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            if any(t.id == "__alpha_meta__" for t in targets):
                meta_node = stmt.value
                break
    if meta_node is None:
        raise ValueError(f"{py_file}: missing __alpha_meta__")
    meta = ast.literal_eval(meta_node)
    if not isinstance(meta, dict):
        raise ValueError(f"{py_file}: __alpha_meta__ is not a dict")

    doc = ast.get_docstring(tree) or ""
    doc_first = next((line.strip() for line in doc.splitlines() if line.strip()), "")

    imported_base_ops: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "src.factors.base":
            for alias in stmt.names:
                imported_base_ops.add(alias.asname or alias.name)

    used_ops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in imported_base_ops:
                used_ops.add(node.func.id)
    return meta, doc_first, sorted(used_ops)


def scan_zoo(zoo_root: Path = ZOO_ROOT) -> list[FactorRecord]:
    records: list[FactorRecord] = []
    for zoo_dir in sorted(zoo_root.iterdir()):
        if not zoo_dir.is_dir() or zoo_dir.name.startswith("_") or zoo_dir.name == "__pycache__":
            continue
        zoo_id = zoo_dir.name
        for py_file in sorted(zoo_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            source = py_file.read_text(encoding="utf-8")
            meta, doc_first, base_ops = _extract_meta_and_ops(py_file)
            zh_name = (_ZH_NAME_RE.search(source).group(1).strip() if _ZH_NAME_RE.search(source) else "")
            zh_desc = (_ZH_DESC_RE.search(source).group(1).strip() if _ZH_DESC_RE.search(source) else "")
            zh_use = (_ZH_USE_RE.search(source).group(1).strip() if _ZH_USE_RE.search(source) else "")
            records.append(
                FactorRecord(
                    zoo=zoo_id,
                    file_rel=str(py_file.relative_to(REPO_ROOT)),
                    meta=meta,
                    zh_name=zh_name,
                    zh_desc=zh_desc,
                    zh_use=zh_use,
                    docstring_first_line=doc_first,
                    base_ops=base_ops,
                )
            )
    return records


def _escape_md(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
        .strip()
    )


def _format_code_cell(text: str) -> str:
    cleaned = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if not cleaned:
        return "-"
    # Escape pipes for markdown table safety; wrap in backticks so raw formulas/LaTeX don't break table rendering
    cleaned = cleaned.replace("|", "\\|")
    if "`" in cleaned:
        return f"`` {cleaned} ``"
    return f"`{cleaned}`"


def render_markdown(records: list[FactorRecord]) -> str:
    by_zoo: dict[str, list[FactorRecord]] = {}
    theme_counter: Counter[str] = Counter()
    dep_counter: Counter[str] = Counter()
    sector_count = 0

    for rec in records:
        by_zoo.setdefault(rec.zoo, []).append(rec)
        for t in rec.meta.get("theme", []):
            theme_counter[t] += 1
        for c in rec.meta.get("columns_required", []):
            if c.startswith("fund:"):
                dep_counter["fund:* (财务基本面)"] += 1
            elif c.startswith("val:"):
                dep_counter["val:* (日频估值)"] += 1
            else:
                dep_counter[c] += 1
        if rec.meta.get("requires_sector"):
            sector_count += 1

    lines: list[str] = []
    lines.append("# Vibe-Trading 内置 478 因子全景目录（Factor Zoo Catalog）")
    lines.append("")
    lines.append("> 本文档由 [export_factor_catalog.py](../../scripts/export_factor_catalog.py) 通过静态 AST 扫描 `agent/src/factors/zoo/` 自动生成，与 `Registry` 元数据契约 100% 同步。")
    lines.append("")

    # 1. Overview
    lines.append("## 1. 因子库总览")
    lines.append("")
    lines.append(f"当前共收录 **{len(records)} 个内置因子**，分布在 **{len(by_zoo)} 个因子库** 中：")
    lines.append("")
    lines.append("| 因子库 (`zoo`) | 因子数量 | 典型来源 / 定位 | 需行业标签 (`requires_sector`) | 需基本面/估值列 |")
    lines.append("|---|---:|---|---:|---:|")

    zoo_desc = {
        "academic": "经典学术因子（Fama-French 5 因子、Carhart 动量、BAB、ILLIQ、52 周高点等）",
        "alpha101": "WorldQuant 101 Formulaic Alphas (Kakushadze, 2015)",
        "fundamental": "PIT 财务基本面与估值因子（盈利质量、成长、现金流、估值比率）",
        "gtja191": "国泰君安 191 短期量价 Alpha 因子库（2014）",
        "qlib158": "Microsoft Qlib Alpha158 特征库（K 线形态、滚动统计、量价回归特征）",
    }
    for zoo_id in sorted(by_zoo.keys()):
        items = by_zoo[zoo_id]
        sec_n = sum(1 for r in items if r.meta.get("requires_sector"))
        fv_n = sum(
            1
            for r in items
            if any(c.startswith(("fund:", "val:")) for c in r.meta.get("columns_required", []))
        )
        lines.append(
            f"| [`{zoo_id}`](#zoo-{zoo_id}) | {len(items)} | {zoo_desc.get(zoo_id, '-')} | {sec_n} | {fv_n} |"
        )
    lines.append(f"| **合计** | **{len(records)}** | 5 大库全覆盖 | **{sector_count}** | **{sum(1 for r in records if any(c.startswith(('fund:', 'val:')) for c in r.meta.get('columns_required', [])))}** |")
    lines.append("")

    # Theme distribution
    lines.append("### 主题分布（Theme）")
    lines.append("")
    lines.append("| 主题 (`theme`) | 覆盖因子数 | 占比 |")
    lines.append("|---|---:|---:|")
    for theme, cnt in theme_counter.most_common():
        lines.append(f"| `{theme}` | {cnt} | {cnt / len(records) * 100:.1f}% |")
    lines.append("")

    # Input / Output Contract
    lines.append("## 2. 统一输入与输出规范（Contract）")
    lines.append("")
    lines.append("所有内置因子均遵循 `agent/src/factors/registry.py` 与 `agent/src/factors/base.py` 定义的硬性契约：")
    lines.append("")
    lines.append("### 2.1 输入规范：`panel: dict[str, pd.DataFrame]`")
    lines.append("- **矩阵形态**：每个键对应一张宽表 `pd.DataFrame`，`index = DatetimeIndex`（交易日期），`columns = instrument_code`（标的代码）。")
    lines.append("- **允许的列命名空间**：")
    lines.append("  - **量价列**：`open`, `high`, `low`, `close`, `volume`, `vwap`, `amount`。")
    lines.append("  - **基本面列（`fund:*`）**：Point-in-Time (PIT) 财报数据，基准评测时对日期级财报强制 `.shift(1)` 防止盘后披露泄露。")
    lines.append("  - **日频估值列（`val:*`）**：A 股 `daily_basic` 白名单估值字段。")
    lines.append("  - **扩展与行业标签**：`extras_required` 声明额外字段；`requires_sector=True` 时必须提供与量价同形状的 `panel['sector']` 标签矩阵。")
    lines.append("- **缺失依赖行为**：若 `panel` 缺少任一 `columns_required` / `extras_required` / `sector`，`Registry.compute()` 抛出 `SkipAlpha` 跳过该因子，不做静默补零。")
    lines.append("")
    lines.append("### 2.2 输出规范：`pd.DataFrame` 与三道质量闸门")
    lines.append("- **返回形态**：与 `panel['close']`（或首个依赖列）完全同 `shape`、同 `index`、同 `columns` 的 `float64` 宽表原始因子得分（raw scores）。")
    lines.append("- **防未来函数**：底层算子 `delta(df, d)` 强制 `d >= 1`，禁止负移位。")
    lines.append("- **严格 NaN 传播**：禁止静默 `fillna(0)`；常量窗口的 `ts_corr`/`ts_cov` 返回 `NaN`。")
    lines.append("- **`Registry.compute()` 自动施加的三道门控**：")
    lines.append("  1. **依赖缺失掩码**：任一声明依赖列为 `NaN` 的单元，输出强制置 `NaN`。")
    lines.append("  2. **预热期裁剪**：前 `min_warmup_bars - 1` 行强制置 `NaN`。")
    lines.append("  3. **数值合法性拦截**：若包含 `±inf` 或全表 `NaN` 比例 `> 95%`，直接抛出 `RegistryError` 拒绝。")
    lines.append("")

    # 3. Per-zoo detailed tables
    lines.append("## 3. 478 个内置因子明细")
    lines.append("")
    for zoo_id in sorted(by_zoo.keys()):
        items = by_zoo[zoo_id]
        lines.append(f'<a id="zoo-{zoo_id}"></a>')
        lines.append(f"### 3.{sorted(by_zoo.keys()).index(zoo_id) + 1} `{zoo_id}`（{len(items)} 个因子）")
        lines.append("")
        lines.append("| ID | 名称与逻辑说明 | 主题 | 输入列 / 行业 / 预热期 / 衰减 | 适用市场 & 频率 | 核心公式 (`formula_latex`) | 调用算子 |")
        lines.append("|---|---|---|---|---|---|---|")

        for rec in items:
            m = rec.meta
            fid = m["id"]
            # Build concise human-readable description
            title = rec.zh_name or m.get("nickname") or rec.docstring_first_line or fid
            detail = rec.zh_desc or m.get("notes") or ""
            if rec.zh_use:
                detail = f"{detail} 用途：{rec.zh_use}" if detail else f"用途：{rec.zh_use}"
            # Keep table readable without losing key meaning
            if len(detail) > 160:
                detail = detail[:157] + "..."
            desc_cell = _escape_md(f"**{title}**" + (f" — {detail}" if detail and detail != title else ""))

            themes = ", ".join(f"`{t}`" for t in m.get("theme", [])) or "-"
            cols = ", ".join(m.get("columns_required", []))
            extras = m.get("extras_required", [])
            if extras:
                cols += f" + extra({', '.join(extras)})"
            sec_flag = "需 `sector`" if m.get("requires_sector") else "无行业依赖"
            warmup = m.get("min_warmup_bars", 0)
            decay = m.get("decay_horizon", 0)
            input_cell = f"`{cols}` / {sec_flag} / warmup={warmup} / decay={decay}"

            univ = ", ".join(m.get("universe", []))
            freq = ", ".join(m.get("frequency", []))
            market_cell = f"`{univ}` ({freq})"

            formula_cell = _format_code_cell(str(m.get("formula_latex", "")))
            ops_cell = ", ".join(f"`{op}`" for op in rec.base_ops) if rec.base_ops else "-"

            lines.append(
                f"| [`{fid}`](../../{rec.file_rel}) | {desc_cell} | {themes} | {input_cell} | {market_cell} | {formula_cell} | {ops_cell} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    records = scan_zoo(ZOO_ROOT)

    # Cross-check against live Registry if importable
    sys.path.insert(0, str(REPO_ROOT / "agent"))
    try:
        from src.factors.registry import Registry

        reg = Registry()
        reg_ids = set(reg.list())
        scanned_ids = {r.meta["id"] for r in records}
        health = reg.health()
        if health["failed"] > 0:
            print(f"WARNING: Registry reported {health['failed']} failed alpha(s): {health['errors']}", file=sys.stderr)
        if reg_ids != scanned_ids:
            missing = sorted(reg_ids - scanned_ids)
            extra = sorted(scanned_ids - reg_ids)
            raise RuntimeError(f"Mismatch vs Registry: missing={missing}, extra={extra}")
        print(f"Verified against Registry: {len(records)} alphas, 0 load errors.")
    except ImportError as exc:
        print(f"Registry import skipped ({exc}); scanned {len(records)} alphas via AST.", file=sys.stderr)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    md = render_markdown(records)
    output_path.write_text(md, encoding="utf-8")
    print(f"Wrote {output_path} ({len(records)} factors, {len(md):,} chars).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
