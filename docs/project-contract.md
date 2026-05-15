# debug-agent Project Contract

## Purpose

`debug-agent` 是一个本地长流程调试 agent runtime。它的目标不是做无边界的通用 agent 平台，而是稳定支撑 `shader-debug-loop` 这类任务：长时间 build/test、失败产物收集、RenderDoc 或子代理分析、补丁生成、回归验证、中断恢复和可审计 trace。

项目必须保持两条边界：

- Runtime Core 自主定义 session、run、tool、checkpoint、artifact、workflow 等核心 contract。
- LangChain/LangGraph 等 agent 框架只作为可替换的 `AgentLoopAdapter`，不能拥有 runtime 真值。

## Runtime Principles

- **Runtime state is authoritative.** 恢复执行所需状态必须结构化保存，不能依赖自然语言总结。
- **All tools pass through ToolBroker.** native tool、shell/git、subagent tool、后续 MCP tool 都必须经过统一安全边界。
- **Event log and checkpoint have different jobs.** event log 负责审计事实，checkpoint 负责恢复真值，artifact store 负责大输出和外部产物。
- **UI and stream observations are non-authoritative.** REPL/TUI 和 streaming observation 只能观察 runtime，不能成为恢复真值，也不能替代 event log 或 checkpoint。
- **Phase-first delivery.** 每个 phase 都交付可运行的垂直切片，不提前横向铺满所有模块。
- **Workflow is first-class after Phase 3.** 长流程调试由 runtime 显式驱动，不靠 prompt agent 自由循环。
- **No hidden shader coupling.** Runtime 必须能支撑 shader-debug-loop，但 runtime core 不能写 shader 专用分支。

## Architecture Layers

1. `CLI Entrypoint`：`debug-agent`、REPL/TUI、one-shot、status、trace、后续 registry 查询。
2. `Runtime Orchestrator`：创建 session/run，调度 prompt、subagent、workflow executor。
3. `Registry`：发现 skills、agents，后续发现 MCP config 和 plugins。
4. `Agent Runtime`：执行 prompt agent、subagent、skill 动态注入。
5. `Workflow Runtime`：执行 code-first deterministic workflow。
6. `Execution Services`：ToolBroker、Approval、CommandRunner、SchemaValidator、TraceWriter。
7. `Persistence Services`：SessionStore、RunStore、EventStore、CheckpointStore、ArtifactStore。

## Core Execution Model

- `Session` 是 runtime container。
- `Run` 是 task 或 execution domain。
- 一个 project root / git worktree 同时最多允许一个 active session。
- 一个 session 同时只允许一个 active runner。
- REPL 默认启动长寿命 prompt run。
- one-shot 默认启动单次 prompt run。
- Phase 0.5 起，TTY REPL 默认使用轻量 TUI；one-shot、非 TTY 和注入 I/O 场景保持纯 stdout/stdin。
- TUI 不改变 session、run、event、checkpoint、artifact 的权威状态。
- Phase 3 起，prompt run 命中 workflow skill 时可创建 workflow run 压栈执行。
- v1 只支持单层 handoff：`prompt run -> workflow run -> prompt run`。

## Phase Roadmap

### Phase 0: Minimal Runtime Slice

跑通最小 CLI agent，并能写 session/run/event/checkpoint。交付 REPL、one-shot、status、trace、SQLite persistence、artifact store、LangChain adapter、PromptAgentExecutor、最小 ToolBroker、workspace active session ownership。

Phase 0 不做 skill、subagent、MCP、workflow、plugin、可写工具、`/compress`。

### Phase 0.5: Lightweight TUI And Streaming REPL

补强 Phase 0 最小 REPL 的人类交互体验，交付轻量 TUI、streaming output、turn 状态、工具调用展示、token/mode 状态栏和非 TTY fallback。

Phase 0.5 是 `CLI Entrypoint / REPL UI` 层增强，不是新的 runtime 语义层。它不得引入 skill、subagent、workflow、MCP、plugin，也不得改变 ToolBroker、Approval、Path Policy、Session/Run/Event/Checkpoint/Artifact 的合同。

### Phase 1: Skills And Native Tools

支持 prompt skill、registry、受控 native/shell tools、path policy、approval grants、ContextManager、`/skills`、`/agents`、`/models`、`/compress`。

Phase 1 是 native 可写工具的最早引入点。可写工具必须经过 ToolBroker、path policy、approval 和 audit。

### Phase 2: Subagents And Session Control

支持 subagent、子 run 生命周期、cancellation token、timeout、`Ctrl+C` interrupt、`/resume`。

### Phase 3: Workflow Core

支持 code-first workflow、workflow checkpoint/resume、step executors、WorkflowSkillExecutor。仍不做 YAML DSL、nested workflow、parallel workflow。

### Phase 4: Shader-Debug Readiness

验证 runtime 能承载 `shader-debug-loop`。实现 shader workflow adapter、build/test wrapper、artifact collection、diff/path validation、final report。shader workflow 使用通用 runtime，不修改 runtime core。

### Phase 5: Optional MCP Integration

支持 MCP server lifecycle、`mcp.toml` loader、MCP tool wrapper、stdio/basic http transport。MCP tool 不能绕过 ToolBroker。

### Phase 6: Optional Packaging

支持静态 plugin 分发 skills、agents、MCP config。plugin 不引入 runtime hook 或 dynamic loader。

## Long-Term Constraints

- v1 metadata store 使用 SQLite。
- v1 artifact store 使用本地文件系统。
- v1 workflow 使用 code-first。
- v1 不支持同一工作目录多 active session。
- v1 不支持 skill/agent/config 热更新。
- v1 不支持 token-level resume、tool-mid-flight resume、subagent-mid-thought resume。
- v1 不支持完整 provider abstraction；早期只保留一个稳定 provider path。
- `AgentLoopAdapter.run()` 是 authoritative result path；`AgentLoopAdapter.stream()` 是 UI observation path，不改变持久化真值。
- MCP 和 Plugin 都不是 Phase 0-4 主路径依赖。

## Non-Goals For v1

- 通用 agent 平台。
- 完整 plugin 平台。
- 动态 plugin hook。
- 通用 YAML workflow DSL。
- nested workflow。
- parallel workflow。
- generic step-level retry。
- 完整 GUI 平台。
- 跨 session prompt history 持久化。
- trace viewer、diff viewer、workflow viewer。
- 通过 TUI 绕过或弱化 ToolBroker、Approval、Path Policy。
- Phase 0.5 引入 mid-call cancel propagation。
- Postgres 或云 artifact store。
- 同一工作目录内多 session 并行。

## Source Of Truth Order

当文档之间产生冲突时，优先级为：

1. `docs/project-contract.md`
2. 当前 phase 文档，例如 `docs/phase-0/*`
3. `docs/adr/*`
4. 根目录历史输入文档，例如 `docs/project-plan.md`
