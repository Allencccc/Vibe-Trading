# Vibe-Trading Agent 投研团队、Swarm 机制与内部 Harness 管控剖析

> **核心结论**：`Vibe-Trading` **并不是**简单套壳外部 LLM，而是在 `agent/src/agent/`、`agent/src/swarm/` 与 `agent/evals/harness/` 中构建了一套**高度工程化的本地确定性内部 Harness**。外部 LLM 只负责推理规划、工具参数生成、L3/L5 语义摘要与研报撰写；而 DAG 调度、角色权限沙箱、5 层上下文预算与压缩、标的身份门禁、工具并发与超时治理、脱敏、断点续跑与离线断言评测，全部由本地确定性代码兜底。

---

## 1. 投研团队配置体系（30 个预设团队）

### 1.1 两级加载与干跑校验（`agent/src/swarm/presets.py`）
- 内置 **30 个 YAML 预设**（位于 `agent/src/swarm/presets/*.yaml`），运行时采用两级查找：优先读取用户目录 `~/.vibe-trading/swarm/presets/`，再回退到包内目录，支持无侵入覆写。
- 内置路径穿越校验（`_validate_preset_name`）与错误信息脱敏（`_redact_home`）。
- `inspect_preset(name)` 提供启动前静态干跑检查：校验 agent/task ID 重复、`input_from` 引用合法性、DAG 无环、拓扑分层以及模板变量声明完整性。

### 1.2 30 个团队的 3 类 DAG 拓扑结构
预设团队包含 3–6 个角色，分为三类经典编排形态：

| 拓扑结构 | 数量 | 典型预设示例 | 协作流向 |
|---|---:|---|---|
| **并行扇出 → 汇总聚合** (`[N, 1]`) | 20 | `technical_analysis_panel` (`[5,1]`)、`value_investing_committee` (`[4,1]` 巴菲特/芒格/段永平/李录 + 主席)、`fundamental_research_team` (`[3,1]`)、`risk_committee` (`[3,1]`)、`ml_quant_lab` (`[2,1]`) | 多视角并行调研，最后由单一主席/聚合器综合 |
| **多空辩论/初筛 → 风控审核 → 最终拍板** (`[2, 1, 1]` / `[2, 1, 1, 1]`) | 5 | `investment_committee`（多头+空头 → 风控官 → PM）、`factor_research_committee`、`pairs_research_lab`、`statistical_arbitrage_desk`、`quant_strategy_desk` | 强对抗研究 + 独立风控否决层 + 最终决策 |
| **纯串行流水线** (`[1, 1, 1]` / `[1, 1, 1, 1]`) | 4 | `equity_research_team`（宏观 → 行业 → 选股 → 聚合）、`derivatives_strategy_desk`、`event_driven_task_force`、`fund_selection_panel` | 自上而下逐级收敛 |

### 1.3 角色权限隔离（`agent/src/swarm/models.py`）
- `SwarmAgentSpec` 为每个角色配置严格的 **`tools` 白名单** 和 **`skills` 白名单**（在 prompt 渲染与 `build_swarm_registry()` 执行期双重强制）。例如 `equity_research_team.yaml` 中的 `aggregator` 仅开放 `[bash, read_file, write_file, load_skill]`，**禁止直接拉取新行情**，防止汇总阶段绕过上游证据自行编造数据。
- `WorkerStatus` 严格区分 `completed`、`failed`、`timeout`、`token_limit`、`incomplete`（无异常但输出为空、未解析工具标记或撞迭代上限，绝不伪装成 `completed`）与 `cancelled`。

---

## 2. Swarm DAG 编排运行时机制

核心模块位于 `agent/src/swarm/`：`runtime.py`、`task_store.py`、`worker.py`、`store.py`、`evidence_handoff.py`。

### 2.1 DAG 校验与拓扑分层（`task_store.py`）
- `validate_dag(tasks)`：使用三色 DFS 检测依赖环并报出精确环路径（`A -> B -> A`）。
- `topological_layers(tasks)`：使用 Kahn 算法按入度波次切分为 `list[list[str]]`。
- 每个任务独立原子落盘为 `tasks/task-{id}.json`（写临时文件 + `os.replace`）；上游完成后调用 `resolve_dependencies()` 解除下游 `blocked_by` 并将状态从 `blocked` 转为 `pending`。

### 2.2 层内并发、层间串行与失败透明降级（`runtime.py`）
```
SwarmRuntime.start_run()
  ├── 1. 清理僵尸运行 (reap_stale_running_runs) & 校验 DAG
  ├── 2. 预取盘前行情快照 (grounding_data) 注入全局上下文
  └── 3. 逐层串行遍历 topological_layers：
         ├── 层内使用 ThreadPoolExecutor(max_workers=4) 并发执行所有就绪 Worker
         ├── 仅对 status=="failed" 执行 Equal-Jitter 指数退避重试（timeout/token_limit/cancelled 不重试）
         ├── 上游部分失败/取消时：下游仍可运行，但强行在 upstream[key] 注入明确缺失声明，杜绝静默编造
         └── 每层结束后汇总 Token/Cache 统计并同步落盘 run.json
```

### 2.3 跨角色不可变证据交接（`evidence_handoff.py`）
下游角色并不直接访问上游角色的工作目录，而是由 `prepare_handoff()` 在启动下游 Worker 前自动执行：
1. 从上游 `messages.json` 提取真实工具观测结果（剔除 `read_file`/`bash` 等管道工具），按内容 **SHA-256** 从 `tool-results/{digest}/result.json` 解析大体积结果（绝不信任原始路径字符串）；
2. 统一脱敏与去重后，写入下游隔离目录 `{destination}/upstream/{sha256}.json` 与索引 `upstream/index.json`；
3. 在下游 System Prompt 中注入紧凑的 `## Inherited evidence` 指引，支持下游直接通过 `quantlib_call` 的 `__data_ref__` 引用原始数据做二次计算。

### 2.4 级联取消、心跳与僵尸回收（`runtime.py`, `worker.py`, `store.py`）
- **跨 Runtime 取消索引**：模块级 `_ACTIVE_RUNS` 统一索引所有在途 Run，解决 HTTP 取消接口（单例 Runtime）与 Agent 工具调用（临时 Runtime）实例隔离的问题，支持按 `run_id` 或 `session_id` 一键级联中断。
- **`_CancellationSignal` / `_LayerStop`**：透传到 `llm.stream_chat(should_cancel=...)`、工具派发前后与重试退避等待中；非正常退出时自动取消子后台任务并 `llm.close()` 防止 socket 泄漏。
- **硬超时与僵尸回收**：每层计算 `layer_deadline`，超时后 `layer_stop.expire()` 阻止挂起线程继续写事件；`HeartbeatTimer` 每 3 秒写心跳到 `events.jsonl`，`SwarmStore.reconcile_run()` 负责崩溃恢复与超时静默任务回收（`run_reaped`）。

### 2.5 运行本质与 Worker 四级启动调用链

#### 两层 Agent 心智模型（单 Agent vs Swarm）
- **单 Agent 对话模式**（`agent/src/agent/loop.py`）：**不依赖 YAML**，直接运行动态 ReAct 循环（`LLM 思考 → 本地工具执行 → 结果回传 → 继续推理`）。
- **Swarm 投研团队模式**（`runtime.py` + `worker.py`）：**YAML 只定义宏观 DAG 协作骨架（角色、工具白名单、依赖与 `input_from`），每个节点内部仍是完整的动态 ReAct 工具循环**，并非“调一次 LLM 拿文本就结束”。
- **关键分工（非 LLM 心算）**：Swarm 并不是“把数据喂给 LLM 让 LLM 计算分析结果”，而是 **LLM 指挥本地确定性 Python 工具取数与计算（行情、因子、回测、`quantlib_call` 等）→ 工具返回精确数值与原始 JSON 证据（`upstream/index.json` / `__data_ref__`）→ LLM 负责定性解读、补查决策与跨层逐级汇总**。

#### Worker 四级启动链与源码定位
1. **最外层入口**：`SwarmRuntime.start_run()`（`agent/src/swarm/runtime.py:413-498`），由 Web API 或 Agent 工具（`agent/src/tools/swarm_tool.py`）调用，启动后台守护线程运行 `_execute_run()`。
2. **层内并发派发**：`_execute_layer()`（`agent/src/swarm/runtime.py:1170-1200`）用 `ThreadPoolExecutor` 并发提交本层就绪任务，并带硬超时等待整层收齐：
   ```python
   future = executor.submit(
       self._run_worker_with_retries,
       agent_spec=agent_spec, task=task, upstream_summaries=upstream, ...
   )
   for future in as_completed(futures, timeout=layer_deadline):
       results[futures[future]] = future.result()
   ```
3. **重试与断点续跑包装**：`_run_worker_with_retries()`（`agent/src/swarm/runtime.py:1330-1347`）检查 `checkpoint.json` 决定是否传 `resume=True`，随后调用 `run_worker(...)`。
4. **Worker 实例化与 ReAct 主循环**：
   - `run_worker()`（`agent/src/swarm/worker.py:482-547`）：为每个任务新建独立 `ChatLLM(model_name=agent_spec.model_name)`，调用 `_run_worker_impl()`，并在 `finally` 中强制 `llm.close()` 与取消遗留后台任务。
   - `_run_worker_impl()`（`agent/src/swarm/worker.py:761-805`）：进入 `for iteration in range(max_iterations):` 主循环（每轮保存 checkpoint → 超时/取消检查 → `worker_context.compact()` 卸载上下文 → `llm.stream_chat()` → 执行白名单工具 → 结果追加到 `messages` 继续下一轮；最后一轮关闭 `tool_defs` 强制输出文本结论）。

---

## 3. 内部自研 Harness 管控机制详解

### 3.1 5 层上下文压缩管线与真实 Token 预算（`loop.py`, `context_budget.py`, `context_compact.py`）
- **真实 Token 预算（`context_budget.py`）**：
  - 窗口解析优先级：环境变量 `VIBE_TRADING_CONTEXT_WINDOW` > 运行时捕获 Provider 溢出报错动态学习的上限 > `context_windows.json` > 默认值。
  - 可用上限取 `min(window * 0.8, max_tokens)`，扣除 System Prompt 与工具 Schema 静态开销后，按对话预算的 `80% / 90% / 100%` 划定 `micro_at`、`collapse_at`、`compact_at` 三道水位线。
  - `ContextMeter` 以 Provider 上一次返回的真实 `input_tokens` 为锚点，仅对新增/压缩消息增量估算，避免纯字符估算漂移；若触发 Provider 上下文溢出报错，`_recover_context_overflow()` 会自动学习真实上限、强制压缩并立即无延迟重试一次。
- **5 层递进压缩策略**：
  1. **L1 `microcompact`**（`> 80%`）：清理最早的 `role="tool"` 输出（保留最近 3 条与待重放调用），替换为自描述占位符 `[CLEARED FROM CONTEXT: this tool call SUCCEEDED...]`，并重新放开对应只读工具的去重键以便必要时重查。
  2. **L2 `context_collapse`**（`> 90%`，零 LLM 成本）：对 6 轮以前超过 2400 字符的长文本按 `前 900 + 后 500` 折叠，并将已清理结果对应的超大 `tool_calls.arguments` 替换为 `"{}"`。
  3. **L3 `auto_compact`**（`> 100%`，防抖阈值 `+8000`）：先落盘完整 `transcript_{ts}.jsonl`；保护尾部 `20,000` Token 不被截断；将头部按 `80,000` 字符分块调用 LLM 做结构化摘要（带硬超时保护）；**确定性回挂 `_verification_ledger`（保留已验证的金融数学结果）** 并修复孤立 tool pair（`_fix_tool_pairs`）。
  4. **L4 `compact` 工具**：允许模型主动请求压缩，但若当前未超过 `micro_at` 水位，Harness 会**直接拒绝**以防过早丢弃证据。
  5. **L5 增量迭代更新**：第 N 次压缩把上一次摘要与新增轮次一起喂给 `_ITERATIVE_UPDATE_PROMPT`，防止早期关键事实衰减。
- **Swarm Worker 轻量化策略（`swarm/context.py`）**：Worker 复用相同的 `CompactionBudget` 与 `ContextMeter`，但用**磁盘卸载**（`context-results/{sha256}/parts/`）替代 LLM 摘要，减少多 Agent 并发成本。
- **工具 Schema 懒加载（`tool_loading.py`）**：当工具总数 `>= 16` 且启用 `lazy` 模式时，初始仅暴露 7 个核心工具 + `load_tools`，其余工具仅展示 80 字符提示，按需追加 Schema 以保护 Prompt Cache 前缀命中率。

### 3.2 工具生命周期治理（`tool_lifecycle.py`, `result_artifacts.py`, `redaction.py`, `trace.py`）
- **并发与顺序控制**：`_batch_execute()` 自动将连续只读工具合并为线程池并发批处理（最多 8 并发），写工具严格串行执行；`search_symbol` 作为分段边界保证身份先解析后使用。
- **去重、缓存与防死循环**：按 `(tool_name, canonical_json(args))` 生成去重键；对反复失败的相同调用直接熔断拦截（`_tool_progress.is_blocked`）；被 L1/L3 压缩掉的只读结果允许从 `_readonly_replay_cache` 恢复最多 6 次（任何写操作立即清空缓存）。
- **差异化超时策略**：只读工具运行在守护线程中，超时即硬中断并返回 `tool_timeout` JSON；**写工具绝不强杀**，仅通过看门狗发 `timeout_warning` 并等待自然结束，防止半写入副作用。另配有 Loop 级 `_stall_watchdog` 监控真实进展（忽略纯心跳）。
- **截断、不可变产物与单点脱敏**：工具输出统一先经 `redact_tool_result()` 脱敏（剔除内部路径、API Key/Token/密码、账户号/SSN 等 PII），再按 `10,000` 字符截断；大结果以只创建一次（`open('x')`）的不可变方式落盘至 `tool-results/{sha256}/result.json` + 分页切片。
- **崩溃安全审计（`trace.py`）**：`TraceWriter` 每条记录落盘 `trace.jsonl` 后立即 `flush() + os.fsync()`；超过 50K 字符的字段先经 `写临时文件 → fsync → os.replace → 目录 fsync` 落盘为 sidecar，再写引用行，确保断电/崩溃无悬空指针。

### 3.3 标的身份门禁与行情 Grounding（`instrument_identity.py`, `swarm/grounding.py`）
- **`IdentityLedger` 确定性状态机**：在任何带标的符号参数（`code/symbol/ticker/underlying` 等）的工具执行前，强制执行 `authorize_tool_call()`：先通过 `_normalize_symbol()` 归一化别名（如 `.SS → .SH`、`sh600519 → 600519.SH`、`HK.00700 → 00700.HK`），未锁定或冲突的符号直接阻断（`identity_required` / `identity_conflict` / `identity_mismatch`），杜绝 LLM 静默篡改交易所后缀。
- **Swarm 盘前 Grounding（`swarm/grounding.py`）**：启动前从 `user_vars` 提取最多 8 个标的，预取近 30 天日线 OHLCV 并在每个 Worker 的 System Prompt 头部注入最近 5 个交易日表格与 30 日极值参考，作为价格事实锚点。

### 3.4 原子 Checkpoint、三阶段续跑与零 LLM 离线评测
- **Worker Checkpoint（`swarm/checkpoint.py`）**：每次迭代边界、工具批处理前后原子保存 `checkpoint.json`（带初始消息 SHA-256 `context_key`）。恢复时**绝不自动重放被中断的工具**，而是为未闭合的 `tool_call` 注入合成的 `interrupted_tool_result` 错误，强制模型先检查外部状态。
- **Run 级三阶段增量续跑（`runtime.py:839-993`）**：
  1. 对比任务定义（`user_vars`, `prompt_template`, `depends_on`, `SwarmAgentSpec`, 模型配置）保留未变更的已完成节点；
  2. 不动点传播：若任意传递上游需要重跑，则使下游失效重跑；
  3. 安全迁移保留节点的产物路径（拒绝符号链接和越界路径），实现精准断点续跑。
- **离线确定性评测 Harness（`agent/evals/harness/`）**：完全**零 LLM、零网络、只读**，直接校验持久化产物（`state.json`, `trace.jsonl`, `identity_evidence.json`, `run_manifest.json` 等）的工具契约、身份账本、数值误差边界与耗时约束，输出 `PASS / FAIL / NOT_EVALUABLE / INVALID_ARTIFACT` 与标准 CI 退出码。

---

## 4. 内部确定性 Harness vs 外部 LLM 边界对照表

| 管控维度 | 本地内部 Harness（确定性代码控制） | 外部 LLM（概率推理） |
|---|---|---|
| **团队编排与调度** | 预设 Schema、DAG 环检测、Kahn 拓扑分层、层内并发/层间串行、退避重试与层硬超时 | 在分配的单个任务内规划分析步骤、撰写研报与综合观点 |
| **角色权限与隔离** | `tools` / `skills` 双重白名单、工作目录越界拦截、`prepare_handoff` 不可变证据复制 | 在允许的范围内决定调用哪个工具、传什么业务参数 |
| **事实与身份防幻觉** | 盘前 OHLCV Grounding 预取、符号别名归一化、`IdentityLedger` 事前硬门禁、日期窗口标准化 | 发起 `search_symbol` 查询、向用户追问歧义标的、基于真实价格推理 |
| **工具安全与性能** | 只读并发/写串行批处理、去重与失败熔断、只读硬超时/写工具守护、10K 截断与脱敏、`fsync` Trace | 解读工具返回的数据或错误、调整后续研究策略 |
| **上下文与成本控制** | 真实 Token 窗口解析与锚定、L1/L2 确定性压缩、L4 过早压缩拒绝、20K 尾部保护、Worker 磁盘卸载、工具懒加载 | 生成 L3/L5 结构化语义摘要、按需调用 `compact` 工具 |
| **容错、取消与验收** | `_ACTIVE_RUNS` 级联取消、心跳与僵尸回收、原子 Checkpoint（禁重放写操作）、三阶段增量续跑、离线 `evals/harness` 断言 | 断点恢复后判断下一步检查动作、产出最终人类可读交付物 |
