# Vibe-Trading 自定义因子开发与公共能力复用指南

> 本文档面向基于 `Vibe-Trading` 开发自有因子（含日频量价、基本面及 L2 高频微观结构因子）的量化研究者，重点说明如何严格遵循内置契约并**直接复用仓库已有的算子、检验、交易成本与风控公共模块**，避免重复造轮子。

---

## 1. 两种标准接入路径

| 路径 | 适用场景 | 核心入口 | 优点 |
|---|---|---|---|
| **路径 A：注册为 Zoo 因子（推荐）** | 希望纳入统一管理，直接复用全套并行 Bench、严格随机对照、Web UI 与策略调用 | `agent/src/factors/zoo/<custom_zoo>/<factor>.py` 或独立目录 `Registry(zoo_root=Path(...))` | 零胶水代码接入 `Registry` 三道质量门控与 `run_bench` / `run_bench_strict` |
| **路径 B：外部脚本生成 + 工具评估** | 已有外部特征工程管线，只想快速评估宽表因子质量 | `agent/src/tools/factor_analysis_tool.py` (`run_factor_analysis`) | 只需输出 `factor.csv` + `return.csv`，立即得到 IC/IR 与分层净值 |

---

## 2. 自定义因子标准模板（路径 A）

在 `agent/src/factors/zoo/my_factors/l2_obi_mom.py`（或外部插件目录）中按如下契约编写：

```python
# ============================================================
# 中文名称: L2 盘口失衡动量因子
# 简要说明: 过去 10 个 bar 的一档委托失衡均值与收益动量的横截面合成。
# 典型用途: 捕获盘口买压持续增强且价格尚未充分反映的短期超额收益。
# ============================================================
from __future__ import annotations

import pandas as pd

from src.factors.base import delta, rank, safe_div, ts_mean, zscore

__alpha_meta__ = {
    # 必须匹配 ^[a-z][a-z0-9]+_[a-z0-9_]+$，且文件名与目录名匹配 ^[a-z][a-z0-9_]{0,31}$
    "id": "myfactors_l2_obi_mom",
    "nickname": "L2 OBI Momentum",
    # 可选值: momentum, reversal, volume, volatility, quality, value,
    #        liquidity, microstructure, sentiment, growth, leverage
    "theme": ["microstructure", "momentum"],
    "formula_latex": r"\mathrm{rank}(\mathrm{ts\_mean}(\mathrm{obi}_1, 10)) + \mathrm{rank}(\Delta_5 \mathrm{close} / \mathrm{close}_{t-5})",
    # 仅允许: open/high/low/close/volume/vwap/amount，以及 fund:* / val:* 命名空间
    "columns_required": ["close"],
    # 非标准量价/基本面的自定义扩展列（如 L2 盘口特征）必须声明在 extras_required
    "extras_required": ["l2:obi_1"],
    "requires_sector": False,
    "universe": ["futures", "equity_cn"],
    "frequency": ["1m", "5m", "1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 10,
    "notes": "Requires L2 OBI feature column in panel['l2:obi_1'].",
}


def compute(panel: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Compute raw factor scores on wide panel (index=date, columns=symbols)."""
    close = panel["close"]
    obi = panel["l2:obi_1"]

    obi_ma = ts_mean(obi, 10)
    ret5 = safe_div(delta(close, 5), close.shift(5))
    return zscore(rank(obi_ma) + rank(ret5))
```

### 使用独立插件目录（不污染原仓库 `zoo/`）
`Registry` 原生支持自定义 `zoo_root`（见 [registry.py](../../agent/src/factors/registry.py#L208-L220)），不会覆盖内置模块的 `sys.modules` 缓存：

```python
from pathlib import Path
from src.factors.registry import Registry

custom_reg = Registry(zoo_root=Path("/path/to/my_zoo_root"))
factor_df = custom_reg.compute("myfactors_l2_obi_mom", panel)
```

---

## 3. 公共能力复用手册（避免重复造轮子）

### 3.1 向量化算子层 — `agent/src/factors/base.py`
所有算子均作用于宽表 `pd.DataFrame`（`index=DatetimeIndex`, `columns=symbols`），自动利用 `bottleneck`（C 编译）与 NumPy `sliding_window_view` 加速（见 [_backend.py](../../agent/src/factors/_backend.py)）：

| 类别 | 可直接复用的函数 | 关键行为与注意事项 |
|---|---|---|
| 横截面标准化 | `rank(df)`, `zscore(df)`, `scale(df, a=1.0)` | 逐行（`axis=1`）计算；零方差或全 NaN 行严格返回 `NaN` |
| 行业内中性化 | `zscore_by_group(df, groups)` | 行业内样本 `< 2` 或零方差返回 `NaN`，**绝不静默降级为全市场 zscore** |
| 滚动统计 | `ts_mean`, `ts_std`, `ts_max`, `ts_min`, `ts_corr`, `ts_cov` | `min_periods=n`，常量窗口相关性强制返回 `NaN` |
| 滚动排序/极值 | `ts_rank`, `ts_argmax`, `ts_argmin` | `ts_rank` 基于 `sliding_window_view` 向量化（比 pandas 快 ~45x）；`argmax/argmin` 优先走 `bottleneck` |
| 趋势/回归/衰减 | `ts_slope`, `ts_resi`, `decay_linear`, `geometric_wma` | 线性回归斜率与残差、线性衰减加权移动平均 |
| 差分与安全运算 | `delta(df, d)`, `safe_div(a, b, eps=1e-12)`, `signed_power(df, p)` | `delta` 强制 `d >= 1` 杜绝前视；`safe_div` 避免 `±inf` |
| 风险与多因子残差 | `downside_beta(close, benchmark, samples=252)`, `factor_residuals(returns, factors, window=60)` | 下行贝塔、滚动多因子正交化残差 |
| 市场 VWAP | `vwap(panel, market)` | 按 `Market` 枚举处理不同市场成交额/量口径差异 |

### 3.2 注册表与质量门控 — `agent/src/factors/registry.py`
调用 `reg.compute(alpha_id, panel)` 自动为你完成以下校验，无需在每个因子里手写防御代码：
1. **依赖前置校验**：缺少 `columns_required` / `extras_required` / `sector` 时抛出 `SkipAlpha`。
2. **依赖缺失掩码**：任一依赖列为 `NaN` 的单元，输出自动置 `NaN`。
3. **预热期自动裁剪**：前 `min_warmup_bars - 1` 行自动置 `NaN`。
4. **输出合法性拦截**：检查 shape 一致、禁止 `±inf`、全表 NaN 比例 `> 95%` 抛出 `RegistryError`。

### 3.3 因子检验、批量 Bench 与严格防过拟合
- **单因子 IC / IR & 分层回测**（[factor_analysis_core.py](../../agent/src/factors/factor_analysis_core.py)）：
  ```python
  from src.factors.factor_analysis_core import compute_ic_series, compute_group_equity

  # 每日有效标的 < 5 自动丢弃；向量化 Spearman Rank IC
  ic_series = compute_ic_series(factor_df, forward_return_df)
  # 按日等权 5 分组净值曲线 (Group_1 .. Group_5)
  group_nav = compute_group_equity(factor_df, forward_return_df, n_groups=5)
  ```
- **多期前瞻收益（非重叠采样防 t 值虚高）**（[alpha_bench_tool.py](../../agent/src/tools/alpha_bench_tool.py#L959-L994)）：
  - `horizon=1` 使用 `close.pct_change(fill_method=None).shift(-1)`；
  - `horizon > 1` 使用末端对齐的非重叠采样，避免重叠窗口引入的 $\sqrt{h}$ 倍 t 统计量虚高。
- **严格随机对照 + 样本外 OOS 验证**（[bench_runner_strict.py](../../agent/src/factors/bench_runner_strict.py)）：
  - 调用 `run_bench_strict(..., oos_split=...)` 或底层 `compute_random_ic_series(factor_df, return_df, n_seeds=5)`：在每个交易日横截面随机置换有限因子值，计算空假设 IC 基线，只有显著跑赢随机对照且样本外依然有效的因子才会归为 `confirmed_alive`。
  - 多重检验校正可直接调用 `src/quantlib/multipletesting.py` 中的 `deflated_sharpe_ratio` 与 `expected_maximum_sharpe`。

### 3.4 交易成本与流动性容量模型 — `agent/backtest/factor_costs.py`
在权重空间评估多空因子组合时，直接复用 [factor_costs.py](../../agent/backtest/factor_costs.py)，无需自己估算滑点、冲击与融券费：

```python
from backtest.factor_costs import apply_adv_capacity, rebalance_cost, borrow_cost

# 1. 容量上限：单期成交额不超过 ADV 的 10%，未成交缺口进入 unfilled_weights 顺延下期
cap = apply_adv_capacity(
    target_weights=w_target,
    current_weights=w_curr,
    adv_value=adv_cny,          # 必须与 capital 同币种（成交额，非股数）
    capital=10_000_000,
    max_participation=0.10,
)

# 2. 换手冲击成本 + 融券成本（支持 fixed / linear / sqrt 三种冲击模型）
cost = rebalance_cost(
    target_weights=w_target,
    current_weights=w_curr,
    adv_value=adv_cny,
    capital=10_000_000,
    periods_per_year=252,
    impact_model="sqrt",        # 调用 src.quantlib.impact.sqrt_impact
    market="CN",                # 默认参考融券年化费率: CN=8.5%, US=0.3%, HK=1.0%
)
# cost.total_cost 为正数拖累（直接从毛收益扣除），同时返回 turnover 与 unfilled_turnover
```

### 3.5 风控透视、持仓约束与组合优化器
- **组合约束**（[constraints.py](../../agent/backtest/constraints.py)）：
  - `MaxWeight`（单标的最大权重）、`MinWeight`（最小权重）、`GroupExposure`（行业/分组敞口上下限）；
  - 通过 `load_constraints(config)` 加载，并用 `apply_constraints_frame(weights_df, constraints, ...)` 批量投影到可行域。
- **组合优化器**（`agent/backtest/optimizers/`，均继承 `BaseOptimizer`）：
  - `EqualVolatilityOptimizer`（等波动加权）
  - `RiskParityOptimizer`（风险平价）
  - `MaxDiversificationOptimizer`（最大分散化）
  - `MeanVarianceOptimizer`（均值-方差）
  - `TurnoverAwareOptimizer`（带换手惩罚与单资产上限的优化器，适合高换手短期因子）
- **组合风险透视**（[risk_xray.py](../../agent/backtest/risk_xray.py)）：
  - 调用 `compute_risk_xray(weights, returns, ...)` 一次输出：集中度（HHI/Top-N）、多空敞口、年化波动与单标的风险贡献（Signed Risk Contribution）、历史回撤区间分解、尾部风险（VaR/CVaR）、分散化比率与相关性矩阵，并可直接用 `render_risk_xray_markdown(report)` 导出报告。

---

## 4. 常见反模式与避坑清单

1. **禁止 `fillna(0)`**：停牌、未上市或预热期缺失必须保持 `NaN`，否则横截面 `rank`/`zscore` 会把缺失标的错误排到中位数。
2. **禁止在 `SignalEngine.generate()` 中重复 `.shift(1)`**：回测引擎已内置次根 Bar 开盘成交（`shift(1)`），额外 shift 会触发 `signal_timing.py` 的 `signal_timing_double_delay` 报错（除非显式配置 `intentional_signal_delay_bars`）。
3. **禁止用 `adv` 股数替代成交额**：`factor_costs.py` 所有 `adv_value` 参数必须与 `capital` 同币种。
4. **多期因子评测务必传 `horizon`**：月频/周频低换手因子直接用默认 `horizon=1` 评测会被短期噪声淹没，传 `horizon=21` 会自动启用非重叠采样。
