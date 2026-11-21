# Spec: Vibe-Trading 深度架构研究与本地 L2/Parquet 数据源接入设计

- **日期**: 2026-11-21
- **分支**: `local-dev`（遵循 [AGENTS.md](../../../AGENTS.md) 本地 Fork 规范）
- **状态**: 待审阅（Draft for Review）
- **路径分类**: Architectural（两阶段交付：Phase 1 深度研究与规范文档；Phase 2 本地 L2/Parquet 数据源工程接入）

---

## 1. 背景与目标

本项目旨在系统性掌握 `Vibe-Trading` 的核心量化研究与 Agent 架构能力，并将用户现有的本地高频 L2 Parquet 行情库（股票与期货，参考 `/Users/allen/Documents/Code/vibe_project/ai-vibe/` 定位逻辑）无缝接入 Vibe-Trading 的回测与因子评测体系，避免重复造轮子。

四项核心目标：
1. **因子库全景与自定义规范**：生成全部 **478 个内置因子** 的结构化 Markdown 文档（逻辑、公式、输入/输出契约），并沉淀自定义因子开发指南（直接复用内置算子、注册校验、成本模型与风控模块）。
2. **回测建仓逻辑深度剖析**：梳理 `runner.py` → `BaseEngine` → 各市场引擎（重点覆盖中国期货 `ChinaFuturesEngine` 与 A 股 `ChinaAEngine`）的完整建仓、调仓、撮合、资金与保证金核算机制。
3. **本地 L2/Parquet 数据源接入**：基于通用路径模板 + 兼容 `ai-vibe` 股票/期货目录定位规范，支持从本地 Parquet/L2 数据按需聚合为标准 OHLCV+VWAP Bar，同时保留原始 L2 微观结构字段扩展口，双链路打通策略回测与 Alpha Zoo 因子评测。
4. **Agent 投研团队、Swarm 与内部 Harness 剖析**：厘清 30 个预设投研团队、Swarm DAG 调度机制，以及本地自研 Harness 与外部 LLM 的职责边界。

---

## 2. 总体交付结构与分阶段策略

采用 **两阶段解耦实施**：
- **Phase 1（深度研究与知识库文档，零代码侵入）**：先产出 3 组高质量文档，摸透因子契约、期货/A 股建仓规则与 Agent Harness 架构，确保 Phase 2 设计与原引擎 100% 对齐。
- **Phase 2（本地 L2/Parquet 数据源工程实施，TDD）**：在充分理解契约的基础上，扩展本地数据加载器、聚合引擎与 Universe 路由，并补齐单元测试与真实样本校验。

### 交付物清单

| 阶段 | 文件路径 | 用途 |
|---|---|---|
| Phase 1 | `scripts/export_factor_catalog.py` | 静态 AST 扫描脚本，自动从 478 个因子源码生成校验后的目录文档 |
| Phase 1 | `docs/research/factor-zoo-catalog.md` | 478 个内置因子完整清单（按 5 大库 + 主题索引，含公式/逻辑/输入输出契约） |
| Phase 1 | `docs/research/custom-factor-guide.md` | 自定义因子开发规范 + 公共函数复用手册（算子/注册/IC/严格验证/成本/风控） |
| Phase 1 | `docs/research/backtest-position-building.md` | 回测建仓逻辑深度剖析（订单原子性、调仓容差、期货保证金/乘数、A 股 T+1） |
| Phase 1 | `docs/research/agent-swarm-harness.md` | Agent 投研团队、Swarm DAG 与 5 层内部 Harness 管控架构剖析 |
| Phase 2 | `agent/backtest/loaders/local_l2_parquet_loader.py` | 通用本地 L2/Parquet 数据集加载器（支持路径模板 + 股票/期货定位 + 聚合） |
| Phase 2 | `agent/backtest/loaders/local_loader.py`（增量扩展） | 在 `data-bridge/config.yaml` 中支持 `parquet_dataset` 配置类型与路由 |
| Phase 2 | `agent/src/tools/alpha_bench_tool.py`（增量扩展） | 支持本地 Parquet 数据集作为自定义 Universe 跑批量因子评测 |
| Phase 2 | `agent/tests/test_local_l2_parquet_loader.py` | 路径解析、股票/期货定位、累计量差分、Bar 聚合、L2 扩展列、边界条件单元测试 |

---

## 3. 模块详细设计

### 模块 1：478 因子库文档与自定义因子复用规范

#### 1.1 478 因子全景文档生成机制
- **覆盖范围**：`agent/src/factors/zoo/` 下 5 个子库（共 478 个 `.py` 因子文件）：
  - `alpha101`（101 个）
  - `gtja191`（191 个）
  - `qlib158`（154 个）
  - `fundamental`（20 个）
  - `academic`（12 个）
- **提取方式（脚本化 + 零遗漏）**：
  - 编写 `scripts/export_factor_catalog.py`，复用 `src/factors/registry.py` 的 `load_alpha_meta_from_py()` 静态解析 `__alpha_meta__`，同时解析模块顶部中文/英文 docstring 与 `compute(panel)` 的关键算子调用链。
  - 每个因子条目固定包含：
    1. `id`、`zoo`、`nickname`、`theme`
    2. **因子逻辑与公式**：LaTeX 公式 + 中文逻辑说明
    3. **输入规范**：`columns_required`（量价 / `fund:*` / `val:*`）、`extras_required`、`requires_sector`、`universe`、`frequency`、`min_warmup_bars`、`decay_horizon`
    4. **输出规范**：宽表 `pd.DataFrame`（`index=DatetimeIndex`, `columns=instrument_code`），严格 NaN 传播、禁止 `±inf`、前 `min_warmup_bars - 1` 行自动置 NaN、全表 NaN > 95% 拒绝。
- **验收标准**：脚本校验产出条目数严格等于 `Registry().list()` 返回的总数（478），无缺失元数据或解析错误。

#### 1.2 自定义因子开发与公共能力复用规范
文档 `docs/research/custom-factor-guide.md` 明确列出开发自定义因子时的标准路径与可直接复用的公共函数，避免重复实现：

| 环节 | 直接复用的公共模块 | 提供的核心能力 |
|---|---|---|
| 向量化算子 | `agent/src/factors/base.py` + `_backend.py` | `rank`, `zscore`, `zscore_by_group`, `scale`, `ts_rank`, `ts_corr`, `ts_cov`, `ts_slope`, `ts_resi`, `ts_argmax/argmin`, `decay_linear`, `downside_beta`, `factor_residuals`, `safe_div`（内置 `bottleneck` 与 `sliding_window_view` 加速，强制防未来函数 `delta(d>=1)`） |
| 注册与质量门控 | `agent/src/factors/registry.py` | AST 扫描发现、`columns_required` 校验、缺失依赖掩码、warmup 自动裁剪、`±inf`/高 NaN 拦截；支持独立 `Registry(zoo_root=...)` 插件目录 |
| 因子评估（IC/分层） | `agent/src/factors/factor_analysis_core.py`、`bench_runner.py` | 向量化 Spearman Rank IC、非重叠多期前瞻收益、分位数分层净值、多进程并行批量跑库 |
| 防过拟合严苛校验 | `agent/src/factors/bench_runner_strict.py`、`src/quantlib/multipletesting.py` | 同截面随机置换对照（`_shuffle_within_rows`）、Train/Test OOS 切分、Deflated Sharpe Ratio |
| 交易成本建模 | `agent/backtest/factor_costs.py` | ADV 参与率上限（默认 10%，未成交顺延）、固定/线性/平方根冲击成本、分市场融券年化费率 |
| 风控与组合优化 | `agent/backtest/constraints.py`、`risk_xray.py`、`optimizers/` | 持仓/行业/杠杆约束、组合波动贡献与回撤透视、Risk Parity / Mean-Variance / Turnover-Aware 优化器 |

---

### 模块 2：回测建仓逻辑深度剖析

文档 `docs/research/backtest-position-building.md` 按真实执行调用链拆解：
1. **入口与信号时序控制（`runner.py` + `signal_timing.py`）**：
   - `BacktestConfigSchema` 校验、`warmup_bars` / `evaluation_start_date` 边界切分、`signal_engine` AST 校验与 `intentional_signal_delay_bars` 防未来函数检测。
2. **目标权重生成与约束（`BaseEngine.run_backtest` + `optimizers/` + `constraints.py`）**：
   - 信号如何转为目标权重、`rebalance_mask` 调仓日历掩码、`rebalance_tolerance` 偏离容差带（仅在持仓权重漂移超过阈值或目标变化时触发调仓）。
3. **逐 Bar 建仓/减仓/反手原子化执行（`agent/backtest/engines/base.py`）**：
   - `_OpenOrder`（价格、数量、杠杆、保证金、手续费、总消耗 `margin + commission`）与 `_ReductionOrder`（释放保证金 + 已实现盈亏 - 平仓手续费）的原子提交逻辑；
   - 资金不足时的缩放/拒单规则、滑点与成本倍率（`execution_cost_multiplier`、`extra_slippage_bps`）生效点；
   - 期末平仓与净值/指标核算（`metrics.py`、`run_card.py`）。
4. **市场专属建仓规则对比**：
   - **中国期货（`china_futures.py` + `futures_base.py`）**：T+0、品种合约乘数（`IF=300`, `IC=200`, `IM=200` 等）、交易所最低保证金率、涨跌停拦截（含滑点后成交价校验）、最小 1 手整数取整、按金额或按手数手续费。
   - **A 股（`china_a.py`）**：T+1 不可当日卖出、涨跌停板拦截、100 股整数手、印花税与过户费。

---

### 模块 3：本地 Parquet / L2 通用数据源接入设计

#### 3.1 设计原则
1. **零硬编码路径，兼容 `ai-vibe` 现有目录资产**：既支持用户通过配置声明任意路径模板，也内置兼容 `/Users/allen/Documents/Code/vibe_project/ai-vibe/` 中已验证的股票与期货目录/文件名定位逻辑。
2. **高吞吐按需裁剪**：按 `start_date ~ end_date` 先做日期级文件定位与剪枝，再通过 **DuckDB**（仓库已原生依赖）或可选 **Polars Lazy** 执行列裁剪与谓词下推，不将无关日期或无关标的读入内存。
3. **双层数据契约（Bar 兼容 + L2 扩展）**：
   - **标准 Bar 输出**：严格满足 Vibe-Trading `BaseDataLoader` 契约（`DatetimeIndex` + `open`, `high`, `low`, `close`, `volume`, `amount`, `vwap`），直接驱动 478 个内置因子和所有回测引擎。
   - **L2 微观结构扩展口**：支持在配置中开启 L2 聚合特征（如盘口价差 `l2:spread`、一档/五档委托失衡 `l2:obi_1` / `l2:obi_5`、买一/卖一深度等），写入 `panel["l2:*"]` 供自定义高频因子通过 `extras_required` 消费；同时提供独立方法 `fetch_raw_l2(symbol, date, data_type)` 给精细高频研究直接读取原始逐笔/快照。

#### 3.2 配置格式设计（扩展 `~/.vibe-trading/data-bridge/config.yaml`）

支持在现有 `sources` 列表下新增 `type: parquet_dataset`（并支持通过环境变量 `VIBE_TRADING_LOCAL_DATA_CONFIG` 指定项目级配置文件覆盖）：

```yaml
datasets:
  # 示例 1：期货 L2（匹配 /Volumes/raid6/L2MktData/CFFEX/ZX01/2026/MarketDataProLv2_20260821.parquet）
  - name: local_cffex_l2
    asset_class: china_futures
    root: "/Volumes/raid6/L2MktData"
    path_templates:
      - "{root}/{exchange}/{machine}/{year}/{prefix}_{date:%Y%m%d}.parquet"
      - "{root}/{exchange}/{symbol}/{year}/{prefix}_{date:%Y%m%d}.parquet"
    defaults:
      exchange: "CFFEX"
      machine: "ZX01"           # 可按交易所配置映射，如 CFFEX->ZX01/ZS01, DCE->GJ02
      prefix: "MarketDataProLv2"
    schema_profile: "l2_futures_pro"   # 内置字段映射 profile，也可手动覆盖 columns
    volume_mode: "cumulative"          # 累计成交量/成交额，Bar 聚合时按 max - min 差分
    tick_time_mode: "dotnet_tick"      # 自动减去 NET_TICK_OFFSET 转为本地交易时间戳
    l2_features: ["spread", "obi_1", "obi_5"]
    contract_overrides:                # 自定义合约/连续合约（如 ZX01）的期货规则补充
      ZX01:
        multiplier: 200
        margin_rate: 0.12
        price_limit: 0.10
        commission: ["rate", 0.000023]

  # 示例 2：A 股 L2（兼容 ai-vibe/stocks/utils/data_loader.py 定位逻辑）
  - name: local_cn_stock_l2
    asset_class: china_a
    root: "/Volumes/raid6/L2MktData/SZSE"
    locator: "ai_vibe_stock_l2"        # 自动按 {year}/{YYYYMMDD} 与 HX02/{year}/{YYYYMMDD} 及新旧文件名查找，优先 .parquet
    contract_id_rule: "cn_stock_prefix" # 6开头->11{code}, 其余->21{code}
    schema_profile: "l2_stock_snapshot"
    volume_mode: "cumulative"
    tick_time_mode: "dotnet_tick"
```

#### 3.3 核心组件与数据流

```
config.yaml (parquet_dataset)
        │
        ▼
[1. FileLocator]  按 [start_date, end_date] 展开候选日期
        │         - 模板模式：按 path_templates 依次探测存在的 .parquet
        │         - 股票兼容模式：按 {year}/{YYYYMMDD} & HX02/{year}/{YYYYMMDD}
        │           依次探测 MarketDataPro / PubMarketDataProLog (snapshot/trade/order)
        ▼
[2. DuckDB / Lazy Reader]  仅扫描命中的日文件，按 ContractId / Symbol 谓词下推
        │                  - ExchTick (.NET Tick) -> 标准 timestamp
        │                  - 过滤 Last <= 0 / NaN / 非交易时段
        ▼
[3. Bar & L2 Aggregator]
        ├─ interval == "1D"：按 TradingDay 聚合 OHLC、max(Volume)/max(Turnover)（或差分）、VWAP
        └─ interval in {1m,5m,15m,30m,1H,4H}：
             - 按桶聚合 arg_min(Last)/max/min/arg_max(Last)
             - volume_mode=cumulative 时按桶内差分（处理跨交易日重置与夜盘切分）
             - 剔除全天零成交量垃圾日
             - 若配置 l2_features，同步输出 l2:spread / l2:obi_1 / l2:obi_5 等附加列
        ▼
[4. Dual Integration]
        ├─ 回测链路：注册为 loader source="local_parquet"（或通过 local: 前缀路由），接入 backtest/runner.py
        │           + 将 contract_overrides 透传给 ChinaFuturesEngine
        └─ 因子链路：接入 alpha_bench_tool.py，支持 universe="local:<dataset_name>" 构造宽表 Panel
```

#### 3.4 边界与错误处理
- **缺失交易日/节假日**：某日无对应 Parquet 文件时静默跳过并记录 debug 日志；若整个区间 0 个文件命中，明确抛出 `FileNotFoundError` 并打印尝试过的模板路径，方便排查挂载盘或命名问题。
- **零成交量与异常价格**：过滤 `Last <= 0` 或 `Open <= 0` 的脏 tick；盘中 bar 聚合后自动剔除全天总成交量为 0 的非活跃交易日。
- **累计量跨日/夜盘重置**：累计成交量差分严格按 `(TradingDay, Symbol)` 分组计算，防止跨日出现负成交量；任何负差分记录告警并钳位或标记 NaN（不静默掩盖脏数据）。
- **合约代码识别**：Vibe-Trading `runner.py` 会拒绝无法识别市场的标的代码；对于 `ZX01` 等自定义/连续合约，通过配置中的 `asset_class` 与 `contract_overrides` 显式声明其市场规则，避免被 `runner.py` 拦截。

#### 3.5 测试策略（TDD）
在 `agent/tests/test_local_l2_parquet_loader.py` 中使用 `tmp_path` 构造合成的股票与期货 L2 Parquet 文件，覆盖：
1. 路径模板定位（含 `CFFEX/ZX01/2026/MarketDataProLv2_YYYYMMDD.parquet` 与股票 `HX02` 回退路径）；
2. `ContractId` 过滤与 `.NET Tick` 时间转换；
3. `1D` 与 `1m`/`5m` Bar 聚合（验证 OHLC、累计成交量差分、VWAP、跨日不产生负量）；
4. L2 扩展特征列（`l2:spread`, `l2:obi_1`）输出与 `Registry.compute()` 的兼容性；
5. 缺失文件、空区间、脏价格（`<= 0`）错误处理；
6. 端到端冒烟测试：本地 Parquet 接入 `ChinaFuturesEngine` 跑通一次完整回测。

---

### 模块 4：Agent 投研团队、Swarm 机制与内部 Harness 剖析

文档 `docs/research/agent-swarm-harness.md` 基于真实源码深入拆解以下四层：

1. **投研团队配置体系（`agent/src/swarm/presets/` & `presets.py`）**：
   - 剖析 30 个预设投研团队的 YAML/模型结构（角色定义、System Prompt、工具白名单、上下游依赖关系）。
2. **Swarm 编排运行时（`runtime.py`, `task_store.py`, `worker.py`, `store.py`, `evidence_handoff.py`）**：
   - DAG 校验（`validate_dag`）与拓扑分层（`topological_layers`）：同层 Worker 线程池并发执行，跨层串行等待并传递结构化证据（`evidence_handoff.py`）；
   - 进程级活跃运行索引（`_ACTIVE_RUNS`）、HTTP/工具级联取消机制（`_CancellationSignal`）、心跳保活与超时处理。
3. **内部自研 Harness 管控机制（`agent/src/agent/` + `swarm/grounding.py` + `swarm/checkpoint.py` + `evals/harness/`）**：
   - **5 层上下文压缩管线（`loop.py` + `context_compact.py` + `context_budget.py`）**：
     - L1 `microcompact`（内存压力下清理旧工具输出）
     - L2 `context_collapse`（零 LLM 成本折叠长文本）
     - L3 `auto_compact`（基于 Token 预算与尾部保护的结构化摘要）
     - L4 `compact tool`（模型显式触发）
     - L5 `iterative update`（增量迭代更新摘要）
   - **工具治理（`tool_lifecycle.py`, `tool_loading.py`, `redaction.py`, `trace.py`）**：连续只读工具自动并发批处理、写工具串行、超时与输出截断、内部路径脱敏、全链路 JSONL Trace 审计。
   - **数值与证据核验（`swarm/grounding.py` + `agent/instrument_identity.py`）**：核验报告中的标的代码与数值是否有上游工具原始输出支撑，防止 LLM 编造数据。
   - **断点续跑与离线评测（`swarm/checkpoint.py` + `agent/evals/harness/`）**：中断任务保留已完成角色产出并续跑未完成节点；离线任务回放评测 Harness。
4. **内部 Harness vs 外部 LLM 边界对照表**：
   - 清晰列出哪些环节完全由本地确定性代码管控（DAG 调度、Token 预算、工具并发与权限、路径脱敏、实盘额度护栏、Grounding 核验、Checkpoint），哪些环节交由外部 LLM（推理规划、工具参数生成、L3/L5 语义摘要、研报综合撰写）。

---

## 4. 风险与约束

1. **只读同步约束**：所有新增文档、脚本与代码严格在 `local-dev` 分支开发，禁止修改 `main` 分支或推送到 `upstream`。
2. **不破坏上游兼容性**：对 `local_loader.py` 和 `alpha_bench_tool.py` 的改动保持向后兼容（现有单文件 YAML 配置和内置 Universe 行为零回归），降低未来 `git merge main` 时的冲突面。
3. **性能与依赖**：优先复用项目已有的 `duckdb` 和 `pandas` 依赖完成 Parquet 扫描与聚合；若环境已安装 `polars` 可作为可选加速后端，但不强制新增重依赖。
