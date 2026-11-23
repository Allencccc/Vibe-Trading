# Vibe-Trading 回测建仓与执行逻辑深度剖析

> 本文档基于 `agent/backtest/` 真实源码（`runner.py`、`signal_timing.py`、`engines/base.py`、`engines/futures_base.py`、`engines/china_futures.py`、`engines/china_a.py`、`constraints.py`、`rebalance_mask.py`、`optimizers/`），完整拆解从配置校验到逐 Bar 建仓、调仓、撮合与风控核算的全链路实现。

---

## 1. 全链路架构总览

```
config.json + code/signal_engine.py
        │
        ▼  [runner.py: main()]
  1. BacktestConfigSchema 校验 & 市场引擎路由 (_select_market_engine)
  2. signal_engine.py AST 安全沙箱校验 + signal_timing.py 双重延迟检测
        │
        ▼  [BaseEngine.run_backtest()]
  3. 全窗口运行 SignalEngine.generate() → _align()
     - 标的自身日历上自动 shift(1)（Next-Bar-Open 语义）
     - 可选 Optimizer（严格使用 ret.index < dt 的历史收益）
     - 可选 Constraints（MaxWeight / MinWeight / GroupExposure）
     - 总敞口归一化：sum(|w|) <= 1.0
  4. Warmup 切片 (evaluation_start_index)：剔除预热期 Bar
        │
        ▼  [BaseEngine._execute_bars()]
  5. 逐 Bar 执行循环：
     a. before_rebalance_bar() 钩子
     b. _calc_open_equity() 按开盘价估算权益（不偷看收盘价）
     c. hold 模式 或 rebalance 模式（含容差带 + 滑点死区 + 50 次二分资金缩放）
     d. 原子提交：先 reductions 释放资金，再 opens 建仓/加仓
     e. after_rebalance_bar() → on_bar() 市场钩子
     f. _calc_equity() 按收盘价盯市（MTM）记录净值快照
  6. 期末强制平仓 (end_of_backtest) & 指标/归因导出
```

---

## 2. 入口校验、安全沙箱与防未来函数机制

### 2.1 配置校验与引擎路由（`agent/backtest/runner.py`）
- `BacktestConfigSchema`（`runner.py:83–258`）在拉取任何行情前即完成前置拦截：
  - **市场识别拦截**：拒绝无法被市场规则识别的标的代码（如 `local:BHP.AX`）和不支持执行规则的日本股票（`jp_equity`），防止静默落回 A 股规则；韩国/台湾/越南股市拒绝分钟级回测。
  - **关键建仓控制参数**：
    - `position_adjustment`: `"hold"`（默认）或 `"rebalance"`；
    - `rebalance_mask`: 调仓日历（如 `"MS"`, `"W-MON"` 或 ISO 日期列表），仅允许在 `rebalance` 模式下使用；
    - `rebalance_tolerance`: 调仓容差带比例（默认 `0.0`）；
    - `warmup_bars` 与 `evaluation_start_date`：二选一互斥，确保评估窗口至少保留 2 根 Bar；
    - `execution_cost_multiplier`（手续费倍率）与 `extra_slippage_bps`（额外滑点基点）。
- `_select_market_engine()`（`runner.py:1799–1942`）按标的路由至 `ChinaFuturesEngine`、`GlobalFuturesEngine`、`ChinaAEngine`、`CompositeEngine`（跨市场组合）等。

### 2.2 `signal_engine.py` AST 安全沙箱（`runner.py:260–882`）
- 导入策略代码前，`_validate_signal_engine_source` 禁止顶层可执行语句和自循环导入；`_scan_runtime_reachable` 从 `SignalEngine` 所有方法递归扫描可达函数，拦截网络/子进程/动态执行模块（`socket`, `subprocess`, `requests`, `eval`, `exec`, `src.trading`, `src.live` 等）以及文件写入操作。

### 2.3 自动 1-Bar 延迟与双重延迟检测（`signal_timing.py` + `engines/base.py`）
- **引擎内置 Next-Bar-Open 语义**：在 `BaseEngine._align()`（`base.py:331–349`）中，每个标的的信号在**自身交易日历**上被强制 `clip([-1, 1])` 并向后平移 1 根 Bar（`shifted[0] = 0.0; shifted[1:] = sig[:-1]`）。因此 `t-1` 收盘产生的信号一定在 `t` 开盘执行。
- **防双重延迟（`signal_timing.py:27–146`）**：`output_delays()` 对 `SignalEngine.generate()` 做静态数据流污点分析。如果策略对最终返回的目标仓位额外调用了 `.shift(k)` 且未在 `config.json` 中显式声明 `intentional_signal_delay_bars = k`，直接抛出 `signal_timing_double_delay` 报错（指标计算内部的历史 lookback 不受影响）。

---

## 3. 目标权重生成：优化器、约束与调仓掩码

1. **组合优化器（`agent/backtest/optimizers/base.py:36–104`）**：
   - 支持 `equal_volatility`、`risk_parity`、`mean_variance`、`max_diversification`、`turnover_aware`。
   - **严格因果性**：在日期 `dt` 计算权重时，仅取 `ret.loc[ret.index < dt, active]`（不包含当日收益 `ret[dt]`），并将空头标的收益取反转换到持仓空间后再计算协方差与权重，最后乘回信号符号。
2. **组合约束（`agent/backtest/constraints.py:48–208`）**：
   - **注意**：当前约束仅在配置了 `optimizer` 时生效（`base.py:412–416`，未配优化器时会告警忽略）。
   - 按配置顺序逐行作用于活跃标的：`MaxWeight`（超限部分按比例再分配，最多 50 轮迭代）、`MinWeight`（低配标的从超额部分补足，不可行时降级等权）、`GroupExposure`（行业/分组超限等比压缩，不向外再分配）。
3. **总敞口归一化（`base.py:366–367`）**：
   - `scale = pos.abs().sum(axis=1).clip(lower=1.0); pos = pos.div(scale, axis=0)`。保证 $\sum_j |w_{t,j}| \le 1.0$（只向下压缩杠杆，不向上放大欠配仓位）。
4. **调仓日历掩码（`agent/backtest/rebalance_mask.py:47–106`）**：
   - 按周期别名（取每周期首个交易日）或指定日期列表（`bisect_left` 映射到首个 `>= date` 的交易日）决定哪些 Bar 允许调仓；非调仓日直接跳过。

---

## 4. 逐 Bar 建仓、调仓与原子化资金账本（`engines/base.py`）

### 4.1 订单与账本核心模型（`base.py:70–100`, `models.py`）
- **`_OpenOrder`**：记录 `symbol, direction, price, size, leverage, margin, commission`，资金消耗 `cost = margin + commission`。
- **`_ReductionOrder`**：记录 `before, target_size, price, released_margin, realized_pnl, exit_commission`，资金回笼 `capital_credit = released_margin + realized_pnl - exit_commission`。

### 4.2 开盘权益估值（防偷看收盘价）
在 `_execute_bars()`（`base.py:1322–1332`）每根 Bar 开始时，通过 `_calc_open_equity()`（`base.py:1545–1586`）按**当前 Bar 开盘价**计算总权益 `equity = capital + sum(margin + unrealized_at_open)`，确保下单仓位大小绝不依赖尚未发生的当日收盘价。

### 4.3 `hold` 模式 vs `rebalance` 模式

| 维度 | `hold` 模式（默认） | `rebalance` 模式 |
|---|---|---|
| **开仓 / 平仓 / 反手** | 均支持（先全量平仓释放资金，再规划开仓） | 均支持（统一规划 `reductions` 与 `opens`） |
| **同方向调仓（加仓/减仓）** | **忽略**，并在 `dropped_target_adjustments` 中记录告警 | **支持**，执行 `_execute_position_increase` / `_execute_partial_reduction` |
| **容差带 (`rebalance_tolerance`)** | 不适用 | 用**未加滑点的开盘价**算 `reference_size`，若 `|before.size - reference_size| <= tol * |reference_size|` 则跳过 |
| **滑点死区保护** | 不适用 | 分别用买滑点价与卖滑点价计算取整后的目标股数/手数；若目标落在滑点带宽内（`increase == reduction`），不触发无意义微调 |

### 4.4 资金不足时的确定性二分缩放（`_fit_rebalance_opens`, `base.py:1957–2046`）
当本根 Bar 的预估资金 `projected_capital = available_cash + projected_reduction_buying_power - sum(open.cost) < -1e-9` 时：
- 引擎**不会**按标的遍历顺序“先到先得”（避免标的代码排序影响回测结果）；
- 而是对所有 `_OpenOrder.size` 施加同一个缩放系数 `mid ∈ [0.0, 1.0]`，运行 **50 次二分搜索**，每次重新执行 `round_size()`、重算保证金与手续费，找到刚好不透支的最大等比缩放系数；
- 缩放后取整为 `0` 的标的被剔除并记入 `plan_rejections["insufficient_capital"]`；
- 若减仓本身导致资金为负（例如亏损超过保证金），则整根 Bar 原子中止并抛出 `ValueError`。

### 4.5 原子提交、加权均价与期末平仓
1. **严格先减后增**（`base.py:1946–1955`）：先提交全部 `reductions`（`target_size <= 1e-9` 走 `_close_position`，否则走 `_execute_partial_reduction` 按比例分摊入场手续费与持仓周期），再提交全部 `opens`（已有持仓走 `_execute_position_increase` 重算加权平均 `entry_price` 并保留原始 `open_price`/`entry_time`）。
2. **滑点与手续费**（`base.py:683–696`）：先叠 `extra_slippage_bps`，再调子类 `apply_slippage(shocked, direction)`；手续费乘以 `execution_cost_multiplier`。
3. **盘后钩子与盯市**：调用 `on_bar()`，再用收盘价（停牌时用无界前向填充 `close_val_df`）计算 `EquitySnapshot` 净值。
4. **期末强平**（`base.py:1484–1539`）：在最后一根 Bar 对剩余持仓按反向滑点价格强制平仓（`reason="end_of_backtest"`），并覆写最后一个净值点以计入期末平仓成本。

---

## 5. 市场专属建仓规则对比：中国期货 vs A 股

对于即将接入的本地 `CFFEX` 期货与 `SZSE/SSE` 股票 L2 数据，两个核心引擎的差异如下：

| 规则维度 | 中国期货 `ChinaFuturesEngine` (`china_futures.py` + `futures_base.py`) | 中国 A 股 `ChinaAEngine` (`china_a.py`) |
|---|---|---|
| **T+0 / T+1** | **T+0**（当日可开可平） | **T+1**（`bar_date == pos.entry_time.date()` 禁止卖出） |
| **做空权限** | 允许（`direction = +1 / -1`） | **禁止**（`direction == -1` 拦截） |
| **杠杆与保证金** | 按品种查 `_MARGIN_RATE`（1.5%–12%，如 `IF/IC/IH/IM=12%`，默认 10% → 10x） | 固定 `leverage = 1.0` |
| **合约乘数 (`cm`)** | `FuturesBaseEngine` 注入 `_calc_pnl`/`_calc_margin`/`_calc_raw_size`；`IF/IH=300, IC/IM=200, T=10000, rb=10` 等，未收录品种默认 `10` | `1.0`（按股计价） |
| **最小下单单位** | 整数手 `>= 1`（`max(int(raw_size), 0)`） | 主板/创业板 100 股整数倍；科创板 `>=200` 股且 1 股递增；北交所 `>=100` 股且 1 股递增 |
| **涨跌停基准价** | 优先 `pre_settle` → `pre_close` → 上根收盘价 | `pre_close` → 上根收盘价 |
| **涨跌停幅度（含滑点校验）** | 股指 `±10%`，国债 `±0.5%–3.5%`，商品默认 `±5%`；用**叠加滑点后的预估成交价**检查是否触板 | 主板 `±10%`，创业板/科创板 `±20%`，北交所 `±30%`（**不识别 ST 和新股上市首日**） |
| **默认滑点** | `0.0005`（5 bps） | `0.001`（10 bps） |
| **手续费与税费** | 按品种 `("rate", pct)` 乘 `size*price*cm`（股指 `0.0023%`）或 `("fixed", 元/手)`；**开平对称（未区分为平今高费率）** | 双边佣金 `max(0.025%, ¥5)` + 双边过户费 `0.001%` + 卖出印花税 `0.05%` |
| **强平 / 追加保证金** | **暂未实现**（`on_bar` 为空操作，资金为负仅阻止新开仓；强平目前仅 `CryptoEngine` 实现） | 不适用（无杠杆现货） |

### 对 Phase 2 接入本地期货 L2（如 `CFFEX/ZX01`）的直接启示
1. `ZX01` 等自定义/连续合约不在 `_MULTIPLIER` / `_MARGIN_RATE` 硬编码表中，若不扩展 `contract_overrides` 会静默退回到默认乘数 `10` 与 `10%` 保证金（虽然会在 `pricing_assumptions` 里记一条告警）。Phase 2 必须在配置层透传品种乘数、保证金率、涨跌停与费率覆写。
2. `ChinaFuturesEngine` 的 `commission_override` 当前公式为 `size * price * override`（未乘 `cm`，见 `china_futures.py:284–285`），自定义配置需注意与按合约名义价值的口径一致。
3. 若做高频日内期货回测，需留意当前引擎未建模**平今惩罚费率**与**盘中维持保证金强平**，实盘前需在成本模型或自定义 `on_bar` 钩子中补齐。
