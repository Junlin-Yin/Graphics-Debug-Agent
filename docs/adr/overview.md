# debug-agent Roadmap And Design Rationale

## 1. 核心理念

`debug-agent` 的目标不是做一个通用到无边界的 agent 平台，而是做一个能稳定执行长流程调试任务的本地 debug agent。它必须能支撑 `shader-debug-loop` 这类任务：长时间 build/test、失败产物收集、RenderDoc/子代理分析、补丁生成、回归验证、可中断和可恢复。

核心理念：

- **Runtime 自主，框架可替换**：debug-agent 自己定义 session、run、tool、checkpoint、workflow 等核心 contract；LangChain/LangGraph 只作为 agent loop adapter。
- **安全边界由 runtime 强制执行**：所有工具调用都必须经过 `ToolBroker`，包括 native tool、shell/git、subagent 内部工具，以及后续接入的 MCP tool。
- **状态必须结构化且可恢复**：可恢复状态不依赖自然语言总结；checkpoint 保存 authoritative state，event log 保存审计事实，artifact store 保存大文件。
- **Workflow 是一级执行系统**：长流程调试不能靠 prompt agent 自由循环推进；workflow 由 runtime 显式驱动。
- **垂直切片优先**：每个 phase 都交付可运行闭环，避免先横向铺满所有模块但长期不可用。
- **不过度设计**：v1 不做 MCP 集成、完整 plugin 平台、通用 YAML workflow DSL、多层嵌套 workflow、step-level retry、skill 热更新、云存储或 Postgres。

## 2. 核心设计

### 2.1 Session / Run 模型

`Session` 是 runtime 容器，保存 approval mode、active run、config snapshot、artifact root 和最新 checkpoint。

`Run` 是执行域，类型包括 `prompt`、`subagent`、`workflow`。REPL 默认是长寿命 prompt run，one-shot 默认是单次 prompt run。workflow skill 被命中时，runtime 创建 workflow run 并压栈执行。

为什么这样设计：

- REPL、one-shot、subagent、workflow 都需要统一的生命周期和恢复语义。
- `shader-debug-loop` 这类任务会跨越多个长时间 step，必须能明确知道当前卡在哪个 run、哪个 step。
- run-scope 的 `active_skills` 可以避免 skill 污染整个 session。

替代方案：

- **只有 Session，没有 Run**：实现简单，但无法清晰表达 prompt/subagent/workflow 的父子关系，也难以恢复局部任务。
- **任意深度 run tree**：表达力更强，但 v1 不需要，会增加 resume、取消、审计复杂度。
- **每条用户消息一个独立进程**：隔离强，但 REPL 上下文、skill 激活、workflow handoff 都会变复杂。

不选替代方案的原因：v1 只需要单层 handoff，`Session + Run` 已经覆盖当前目标，任意深度执行树属于过早设计。

### 2.1.1 工作目录所有权

v1 采用一个 project root / git worktree 同时最多一个 active session 的策略。Session 创建时获取该工作目录的 ownership；如果已有 active session，则拒绝启动。

为什么这样设计：

- debug agent 会执行 shell、git、文件修改和测试，多个 session 共用一个工作目录会产生难以恢复的冲突。
- `shader-debug-loop` 单次运行耗时长、状态重，稳定性比并发能力更重要。
- 并行需求可以通过 git worktree 创建独立 repo 副本解决，不需要 runtime 自己管理多工作树隔离。

替代方案：

- **同一工作目录允许多 session**：用户体验灵活，但 git 状态、artifact、patch、cleanup 会互相污染。
- **runtime 自动复制目录**：隔离更强，但成本高，磁盘占用大，也容易和大型引擎仓库不匹配。
- **细粒度文件锁**：理论精细，但实现复杂，无法覆盖 shell/git/build/test 的全局副作用。

不选替代方案的原因：单工作目录单 session 是最小可靠模型；需要并行时使用 git worktree 更清晰。

### 2.2 自研 Runtime Core + AgentLoopAdapter

Runtime Core 定义自己的 `Session`、`Run`、`RunEvent`、`ToolResult`、`StepResult`。LangChain 默认只通过 `LangChainAgentLoopAdapter` 接入，负责模型 tool-calling loop。

为什么这样设计：

- agent framework 更新很快，核心状态不能被第三方抽象锁死。
- LangChain 适合快速跑通 prompt agent，但不适合拥有 workflow checkpoint、approval、artifact、session control 等系统真值。
- 后续如果切换 OpenAI Agents SDK、LangGraph 或自研 loop，只需新增 adapter。

替代方案：

- **LangChain-first**：直接让 LangChain/LangGraph 拥有状态、graph、checkpoint。初期快，但后期安全、恢复、workflow 与 artifact 逻辑容易被框架约束。
- **完全自研 agent loop**：控制力最强，但 Phase 0/1 成本高，不符合不过度设计。
- **每个 provider 单独写 executor**：短期可用，但接口会分裂，难以统一工具和事件审计。

不选替代方案的原因：adapter 模式在控制力和实现成本之间更稳。

### 2.3 ToolBroker 作为唯一工具出口

所有工具调用都进入 `ToolBroker`，输出统一 `ToolResult`。ToolBroker 负责 allowed/disallowed tools、approval mode、path policy、timeout、cancel、audit event；MCP wrapper 在后续可选阶段接入同一边界。

为什么这样设计：

- 这是安全边界的唯一可信位置。
- `shader-debug-loop` 会执行 shell、git、diff、文件读写和子代理工具，不能依赖 prompt 约束。
- 统一 `ToolResult` 能让 agent、subagent、workflow、trace、测试复用同一套失败处理。

替代方案：

- **工具直接暴露给 agent**：实现最快，但 MCP/subagent/shell 都可能绕过审批和路径策略。
- **每类 executor 自己做权限**：局部简单，但策略会重复且不一致。
- **只靠系统 prompt 禁止危险操作**：不可接受，模型输出不能作为安全边界。

不选替代方案的原因：debug agent 会操作真实工作树和外部命令，安全策略必须由 runtime 执行。

### 2.3.1 SchemaValidator 作为边界校验器

`SchemaValidator` 只校验 runtime contract 的结构，不判断业务推理是否正确。它覆盖 agent/subagent 结构化输出、`ToolResult`、`StepResult`、workflow state/checkpoint、artifact metadata 和 manifest。

为什么这样设计：

- LLM 和外部工具输出都不应被默认信任，进入 runtime 前必须结构化校验。
- 结构错误和业务错误需要分开；schema 错误影响恢复和审计，应立即失败。
- `shader-debug-loop` 依赖 `rdc_report`、`fix_report` 等结构化产物，schema 校验失败必须可追踪。

替代方案：

- **不做统一 validator，各处手写检查**：实现快，但错误分类和行为会分裂。
- **让 validator 校验业务正确性**：边界过宽，会把领域判断和 runtime contract 混在一起。
- **只校验 agent 输出**：遗漏 tool/workflow/artifact 边界，仍会出现非法状态进入 checkpoint。

不选替代方案的原因：validator 的职责应窄而硬，只守住结构 contract。

### 2.4 Event Log + Checkpoint Snapshot

SQLite 保存 `sessions`、`runs`、`run_events`、`checkpoints`、`artifacts`、`approval_grants`。checkpoint 保存 authoritative state，event log 保存审计事实，artifact store 保存大输出。

为什么这样设计：

- checkpoint 负责恢复，event log 负责解释发生了什么。
- 长 stdout、RenderDoc capture、diff、日志等不应进入 LLM 上下文或 checkpoint。
- `trace.md` 可以从 event log 和 artifacts 派生，不需要成为真值。

替代方案：

- **Checkpoint-only**：实现简单，但失败诊断、审计、trace 生成会弱。
- **完整事件溯源**：理论最强，但需要设计复杂 replay 语义，v1 过重。
- **纯文件 JSONL**：容易起步，但并发、查询、resume、版本迁移会变脆。

不选替代方案的原因：SQLite event log + snapshot 足够支撑 v1 的恢复和审计，同时没有完整事件溯源那么重。

### 2.5 Code-first Workflow

Workflow v1 使用 Python `WorkflowDefinition`、step executor 和 handler。YAML 暂不承载复杂控制逻辑。

为什么这样设计：

- `shader-debug-loop` 的控制流包含 retry、case switch、artifact collection、子代理串联、diff 校验，这些逻辑用 Python 更清晰、更可测。
- 一开始设计通用 YAML DSL 容易把表达式、模板、类型、错误处理全部推复杂。
- code-first workflow 仍然能保持 deterministic finite workflow，不退化成 agent 自由循环。

替代方案：

- **YAML-first DSL**：配置化好，但会很快需要 expression evaluator、模板系统、schema、debugger。
- **Agent-driven workflow**：代码最少，但状态推进、恢复、安全边界都弱。
- **直接使用 LangGraph workflow**：生态成熟，但会把 checkpoint 和 graph 语义绑定到外部框架。

不选替代方案的原因：v1 的重点是跑稳一个长流程，而不是设计通用 workflow 语言。

### 2.5.1 Workflow 失败和重试

v1 不提供通用 step-level retry。step 一旦失败、超时、被拒绝或 schema 校验失败，workflow 直接进入 error-handling。业务级循环必须由 workflow handler 显式表达。

为什么这样设计：

- `shader-debug-loop` 的 build/test/debug 每步都可能耗时很长，自动重试会显著增加总运行时间。
- 该 skill 已定义失败时走 error-handling 并退出 workflow，不需要 executor 级 retry。
- 业务级 retry 与 step-level retry 语义不同；前者是调试循环，后者是隐藏失败的执行策略。

替代方案：

- **每个 step 支持 max_retries**：通用但容易拉长运行时间，也会掩盖真实失败。
- **只对 shell timeout retry**：看似保守，但仍会引入复杂的幂等性和副作用问题。
- **外层重新触发整个 workflow**：简单但太粗，不适合保留当前调试上下文。

不选替代方案的原因：v1 需要确定性和可诊断性，失败应显式进入 error-handling。

### 2.6 Prompt Skill 动态激活

Prompt skill 通过 `activate_skill(name)` 激活，skill 正文在下一次 model call 生效。workflow skill 由 orchestrator 直接路由，不通过 `activate_skill`。

为什么这样设计：

- skill 正文可能很长，不适合启动时全部注入。
- agent 运行中才知道需要哪类方法论，动态激活能节省上下文。
- workflow 是执行模式，不应让模型在运行中自由决定是否进入。

替代方案：

- **启动时注入所有 skill**：简单但浪费上下文，也容易引入无关约束。
- **每次激活都重启 agent loop**：实现直观，但会破坏同一 run 的连续性。
- **workflow 也用 activate_skill 触发**：模糊了顶层路由和模型工具调用的边界。

不选替代方案的原因：prompt skill 和 workflow skill 的生命周期不同，必须分开处理。

### 2.6.1 无热更新

v1 不支持 skill、agent、MCP config 或 model config 热更新。Session 启动时冻结 registry/config snapshot，修改配置后必须启动新 session 才生效。

为什么这样设计：

- 长流程调试需要可复现，同一 session 中途改变 skill 或 agent prompt 会让 trace 难以解释。
- 热更新需要处理版本、缓存失效、active skill 替换、subagent config 变更等问题，早期收益不高。
- 重启 session 是清晰且可接受的操作边界。

替代方案：

- **每次 tool/model call 前重新扫描**：灵活但不可复现，也增加 IO 和状态复杂度。
- **只在 idle 状态热更新**：比全量热更新安全，但仍需处理 registry 版本和 summary/checkpoint 兼容。
- **手动 reload 命令**：可控但增加控制面，v1 没有必要。

不选替代方案的原因：冻结 snapshot 更利于长流程恢复、审计和问题复盘。

### 2.7 Plugin 后移

v1 只做 skill/agent discovery。MCP 和 Plugin 都后移为可选扩展，其中 MCP 先于 Plugin。

为什么这样设计：

- 早期核心风险在 runtime、ToolBroker、subagent、workflow，不在外部工具生态和分发格式。
- plugin 如果太早进入主线，会引入版本、覆盖、依赖、动态加载等复杂度。
- shader-debug-loop 主路径不需要 MCP，shader-debug 相关资源后续可以作为静态包组织，但不需要影响 v1 runtime。

替代方案：

- **Phase 1 就实现完整 plugin**：功能完整，但拖慢最小可运行闭环。
- **彻底不要 plugin**：最简单，但后续技能、agent、MCP 配置分发缺少边界。
- **支持动态 plugin hook**：扩展性强，但安全和调试成本高。

不选替代方案的原因：静态 plugin 足够，runtime hook 不符合安全和不过度设计原则。

## 3. 核心模块

### 3.1 CLI Entrypoint

职责：

- 提供 `debug-agent`、`debug-agent -p`、`resume`、`status`、`trace` 等入口。
- 提供 REPL slash commands。
- 本地解析 slash commands，不交给 LLM。

关键设计：

- REPL 默认 `normal`。
- one-shot 默认 `yolo`。
- 运行中普通 prompt 拒绝，控制命令和只读 slash command 允许。

### 3.2 Runtime Orchestrator

职责：

- 创建 session 和 run。
- 调用 registry 和 skill resolver。
- 根据 execution mode 路由到 prompt/subagent/workflow executor。
- 管理 run stack 和 active runner。

关键设计：

- 单 session 单 active runner。
- 单 project root / git worktree 单 active session。
- v1 只支持单层 handoff。
- workflow run 由 orchestrator 创建，不由 agent 自行伪造。

### 3.3 Registry

职责：

- 发现 skills 和 agents；Phase 5 起发现 MCP config。
- 解析 `SKILL.md` header 和 `agent.toml`。
- 执行 source precedence 和覆盖规则。

关键设计：

- 显式路径 > 项目级 > 全局级 > builtin。
- 同名资源整体覆盖，不做目录 merge。
- session 启动时冻结 registry/config snapshot，不支持热更新。
- plugin discovery 后移到 Phase 6。

### 3.4 Agent Runtime

职责：

- 执行 prompt run。
- 管理 active skills。
- 通过 adapter 调用具体模型框架。
- 启动 subagent run。

关键设计：

- `AgentLoopAdapter` 隔离 LangChain。
- prompt 组合顺序固定：runtime safety prefix、agent system prompt、active skills、任务输入。
- subagent 继承 approval mode，但可独立配置模型和工具白名单。

### 3.5 ToolBroker

职责：

- 统一工具入口。
- 执行审批、路径、风险和 tool allowlist。
- 包装 native、shell/git tools；Phase 5 起包装 MCP tools。
- 输出标准 `ToolResult`。
- 写 tool audit event。

关键设计：

- yolo 也不能绕过 ToolBroker。
- MCP 不直接暴露。
- subagent 不能直接调用未包装工具。

### 3.5.1 SchemaValidator

职责：

- 校验 agent/subagent 结构化输出。
- 校验 `ToolResult` 和 `StepResult`。
- 校验 workflow state/checkpoint。
- 校验 artifact metadata 和 manifest。

关键设计：

- 只校验结构，不校验业务推理。
- 校验失败写入 run event。
- workflow step schema 校验失败后进入 error-handling，不自动重试。

### 3.6 Persistence Services

职责：

- 保存 session/run metadata。
- 写入 run events。
- 保存 checkpoint。
- 管理 artifact。
- 支持 trace 派生和 resume。

关键设计：

- SQLite 是 metadata 真值。
- filesystem 是 artifact 真值。
- checkpoint 不保存大输出。
- event log 不替代 checkpoint。

### 3.7 ContextManager

职责：

- 管理 LLM 可见上下文。
- 压缩 conversation history。
- 控制 skill/tool/subagent 输出预算。

关键设计：

- `/compress` 只压缩 LLM 历史。
- workflow state 不靠 summary 恢复。
- 大输出进入 artifact，模型只看摘要。

### 3.8 Workflow Runtime

职责：

- 执行 code-first workflow。
- 管理 step、transition、checkpoint、interrupt、resume。
- 提供 python/shell/subagent/interrupt step executor。

关键设计：

- Workflow 是一级 executor。
- transition 由 Python handler 返回。
- v1 不做 YAML DSL。
- v1 不做通用 step-level retry。
- step boundary 是强恢复边界。

### 3.9 MCP Integration

职责：

- Phase 5 起加载全局和项目级 `mcp.toml`。
- Phase 5 起管理 MCP server 生命周期。
- Phase 5 起将 raw MCP tools 包装后交给 ToolBroker。

关键设计：

- 支持 `stdio` 和 basic `http`；`streamable_http` 作为可选 transport。
- 子代理通过 `agent.toml.mcp_servers` 选择 server。
- MCP tool 不允许绕过 approval 和 path policy。

### 3.10 Plugin Packaging

职责：

- Phase 6 后作为静态资源包组织 skills、agents、MCP config。

关键设计：

- plugin 不引入 runtime hook。
- plugin 不做 dynamic loader。
- plugin 不能绕过 ToolBroker。

## 4. Phase 路线

### Phase 0: Minimal Runtime Slice

目标：最小 CLI agent 可运行，并能写 session/run/event/checkpoint。

交付：

- CLI/REPL/one-shot。
- `SessionStore`、`RunStore`、`EventWriter`、`CheckpointStore`、`ArtifactStore`。
- `ModelFactory`、`LangChainAgentLoopAdapter`、`PromptAgentExecutor`。
- 最小 `ToolBroker`。
- workspace active session ownership。

不做 skill、subagent、MCP、workflow、plugin。

### Phase 1: Skills And Native Tools

目标：支持 prompt skill 和受控 native/shell tools。

交付：

- `SkillRegistry`、`AgentRegistry`。
- `activate_skill` 和 `SkillPromptMiddleware`。
- `ContextManager`、path policy、approval grants。
- session-level registry/config snapshot。
- `/skills`、`/agents`、`/models`、`/compress`。

不做 subagent、MCP、workflow、plugin。

### Phase 2: Subagents And Session Control

目标：支持子代理、run control、interrupt/resume，同时维持统一安全边界。

交付：

- `SubagentExecutor`。
- 子 run 生命周期。
- cancellation token、timeout、`RunController`、`Ctrl+C`、`/resume`。

不做 MCP、workflow、plugin、parallel subagents。

### Phase 3: Workflow Core

目标：支持 code-first workflow，并能表达长流程 debug loop。

交付：

- `WorkflowDefinition`、`WorkflowEngine`、`WorkflowContext`。
- python/shell/subagent/interrupt step executors。
- workflow checkpoint/resume。
- `WorkflowSkillExecutor`。
- workflow error-handling without generic step-level retry。

不做 YAML DSL、nested workflow、parallel workflow。

### Phase 4: Shader-Debug Readiness

目标：验证 runtime 能承载 `shader-debug-loop`。

交付：

- shader workflow adapter。
- build/test wrapper。
- artifact collection helper。
- diff/path validation helper。
- final trace/report generator。
- fake runner fixtures 和 Windows e2e 文档。

要求 shader workflow 使用通用 runtime，不修改 runtime core。

### Phase 5: Optional MCP Integration

目标：shader-debug readiness 稳定后，支持 MCP tool re-binding 作为可选外部工具扩展。

交付：

- `MCPServerManager`。
- `mcp.toml` loader。
- MCP tool wrapper。
- `stdio` 和 basic `http` transport。
- optional `streamable_http` transport。

要求 MCP tool 使用通用 ToolBroker，不修改 runtime core。

### Phase 6: Optional Packaging

目标：核心稳定后支持静态 plugin 分发。

交付：

- `PluginRegistry`。
- plugin manifest。
- plugin 内 skill/agent/MCP discovery。
- `debug-agent plugins list`。

## 5. 设计边界

v1 明确不做：

- 完整 plugin 平台。
- 动态 plugin hook。
- skill/agent/config 热更新。
- 通用 YAML workflow DSL。
- nested workflow。
- parallel workflow。
- generic step-level retry。
- 同一工作目录多 session 并行。
- Postgres。
- 云 artifact store。
- 完整 provider abstraction。
- token-level resume。
- tool-mid-flight resume。
- subagent-mid-thought resume。

这些边界不是能力缺失，而是为了让 v1 聚焦在能稳定运行长流程 debug workflow 的最小架构上。

## 6. 与 docs/project-plan.md 的关系

`docs/project-plan.md` 是跨阶段总计划和历史规划上下文。

`docs/adr/overview.md` 是设计 rationale，解释为什么这样设计、替代方案是什么、为什么不选替代方案。

当它们与当前 implementation contract 冲突时，以 `docs/project-contract.md` 和当前 phase 文档作为实现真值。
