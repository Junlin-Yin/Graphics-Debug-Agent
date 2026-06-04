# debug-agent Architecture Decision Overview

本文档是 ADR 总览和跨 ADR 的架构 rationale。它不定义 phase scope、交付计划或验收标准；这些内容由 `docs/project-contract.md`、当前 phase 文档和具体 ADR 分别承载。

## Accepted ADRs

- [ADR 0001: Phase-First Document Structure](0001-phase-first-document-structure.md)
- [ADR 0002: LangChain As Adapter](0002-langchain-as-adapter.md)
- [ADR 0003: SQLite Event Log Plus Checkpoint Snapshot](0003-sqlite-event-log-checkpoint.md)
- [ADR 0004: ToolBroker As Mandatory Execution Boundary](0004-toolbroker-execution-boundary.md)
- [ADR 0005: Workspace Active Session Ownership](0005-workspace-active-session-ownership.md)
- [ADR 0006: Frozen Session Config Snapshot And Narrow Provider Strategy](0006-frozen-config-snapshot-provider-strategy.md)
- [ADR 0007: AgentLoopAdapter Streaming Observation Path](0007-agentloopadapter-streaming-observation-path.md)
- [ADR 0008: Lightweight REPL TUI Architecture And Terminal UI Stack](0008-lightweight-repl-tui-architecture.md)
- [ADR 0009: Prompt Skills As Frozen Snapshots With Runtime-Supplied Active Context](0009-prompt-skills-frozen-snapshots-active-context.md)
- [ADR 0010: ModelContextFrame As The LLM-Visible Context Boundary](0010-modelcontextframe-llm-visible-context-boundary.md)
- [ADR 0011: Layered Context Compression For Runtime Continuity](0011-layered-context-compression-continuity.md)
- [ADR 0012: Runtime-Enforced Shell, Path, And Approval Policy](0012-runtime-enforced-shell-path-approval-policy.md)
- [ADR 0013: Runtime-Owned Todo Plan Continuity](0013-runtime-owned-todo-plan-continuity.md)
- [ADR 0014: Terminal Recovery Checkpoints And Durable Conversation](0014-terminal-recovery-checkpoints-durable-conversation.md)
- [ADR 0015: Normalized Error Taxonomy And Narrow Runtime Retry](0015-normalized-error-taxonomy-narrow-runtime-retry.md)

## Core Rationale

`debug-agent` 的架构核心是 runtime-owned truth。Session、Run、ToolBroker、checkpoint、event log、artifact、skill snapshot、Todo Plan 和 subagent run 都属于 runtime contract。LLM 框架、UI、streaming delta、prompt skill 和业务脚本都不能拥有恢复真值或安全真值。

这条原则带来几个稳定边界：

- LangChain/LangGraph 等 agent 框架只通过 `AgentLoopAdapter` 接入。框架可以被替换，但不能接管 session、checkpoint、artifact、approval 或 tool policy。
- `AgentLoopAdapter.run()` 是 authoritative result path；`AgentLoopAdapter.stream()` 只是 UI observation path。TUI 可以展示增量输出，但增量输出不成为 checkpoint 或 event log 的替代品。
- SQLite event log、terminal recovery checkpoint、durable conversation 和 filesystem artifact store 分工明确：event log 解释发生过什么，terminal recovery checkpoint 是 prompt session resume 入口，durable conversation 保存 accepted model-visible message truth，artifact store 保存大输出和外部产物。
- ToolBroker 是所有模型可见工具的强制执行边界。native tool、shell/git、runtime control、Plan tools、`view_image`、foreground subagent `task` 和后续 MCP tool 都不能绕过审批、路径策略、shell 策略、timeout 和 audit。
- Runtime error handling 使用集中定义的 normalized error taxonomy。窄 runtime retry 只处理明确 opt-in 的 transient runtime-owned failure 和 `output_token_limit_reached` continuation，不引入 generic step retry、tool replay、token-level resume 或已接受 model-call result replay。

## Prompt-Skill Mainline

当前主线选择 prompt skill + Todo Plan + foreground named subagent，而不是把 Workflow Runtime 作为默认执行层。

这个选择的理由是：

- 现有真实调试流程首先需要稳定使用 prompt skill、受控 shell、artifact、图片观察和上下文连续性；这些能力已经与当前 runtime 边界自然贴合。
- `renderdoc-gpu-debug` 可以由普通 prompt skill、短时结构化 `rdc` 命令、`view_image` 和 Todo Plan 支撑，不需要 runtime 托管 RenderDoc session state machine。
- `shader-debug-loop` 的业务循环、报告格式、schema 和 retry 规则属于 skill 业务协议，不应写入 runtime core。
- Todo Plan 适合作为 runtime-owned continuity state，解决长流程 prompt 执行中的计划漂移问题，同时不把业务状态机硬编码进 runtime。
- Foreground named subagent 提供明确的同步 barrier、child run、audit 和 artifact 边界，足以承载当前业务层 subagent 协作；parallel/background task graph 会显著扩大恢复、取消和策略语义。

因此，prompt skill 负责表达业务方法论，runtime 负责提供可恢复、可审计、可受控的平台能力。主 agent policy 是 capability ceiling；named subagent 只能从该上界收紧，不能额外开权。

## Why Workflow Is Deferred

Workflow Runtime 曾被视为长流程调试的核心执行层，但当前主线将它后移为 deferred architecture module。

后移的原因不是否定 workflow 价值，而是避免在需求尚未被真实流程证明前引入过重的执行语义：

- workflow 需要 step boundary、transition、checkpoint/resume、error handling、schema validation、tool execution 和可能的 retry 语义。这些语义一旦进入 core contract，就会影响所有后续恢复和审计设计。
- 当前最紧迫的不确定性不是“如何设计通用 workflow engine”，而是 prompt skill 路线能否稳定承载真实 RenderDoc inspection 和 shader-loop readiness。
- 如果过早把 `shader-debug-loop` 改造成 workflow skill，runtime 很容易吸收 Ralph Loop、报告 schema、case switch 等业务语义，破坏平台/skill 边界。
- prompt skill + Todo Plan + foreground subagent 能先验证真实使用路径，同时保留未来把已验证的稳定流程下沉为 workflow 的可能性。

Workflow Runtime 保留为后续可重新评估的架构模块。只有当 prompt skill 路线在真实 case 中无法稳定遵循长流程，或确实需要跨 turn / 跨 session 恢复 workflow step 时，才应重新打开 workflow 设计。

## Platform And Skill Boundary

Runtime 提供平台能力：

- skill 发现、冻结、激活与 runtime-supplied active context。
- ToolBroker、path policy、shell policy、approval、timeout、artifact 和 audit。
- event log、checkpoint、trace 派生和 context continuity。
- `view_image` 这类 brokered tool，把图片语义观察作为普通 tool result 交还给 agent。
- Todo Plan、durable conversation、terminal checkpoint resume 和 foreground named subagent lifecycle。

Skill 负责业务行为：

- 如何使用 RenderDoc 和 `rdc`。
- Ralph Loop 步骤、业务 retry 规则、业务报告和业务 trace。
- 哪些业务输出需要 JSON、使用哪个 schema、校验失败如何处理。
- shader-loop 业务输出目录和报告组织方式。

Runtime 不应内置 shader project 名称、Ralph Loop 状态机、业务报告格式、shader 专用 trace/schema validator、RenderDoc 命令白名单或固定 procedure。

## Deferred General Agent Modules

以下模块目前是 deferred architecture modules。它们大多是通用 agent 平台能力，而不是当前调试主线的最小必需能力。将它们暂缓的目的，是避免 runtime core 在真实需求尚未稳定前吸收过宽的执行模型、插件模型、任务模型或沙箱模型。

这些模块不是永久排除项。它们只有在真实需求触发、合同边界清楚，并由 project contract、accepted ADR 和后续 phase 文档明确纳入范围后，才应进入实现。若要在 v1/v2 内重新纳入 deferred module，必须先修改 `docs/project-contract.md`，不能只通过 phase 文档扩展当前主线。

- `Workflow Runtime`：适合表达稳定、可枚举、需要 step-level checkpoint/resume 的确定性流程。当前先验证 prompt skill + Todo Plan + foreground subagent 是否足够承载真实调试流程，避免过早把业务状态机下沉进 runtime。
- `Task / Background Task System`：适合后台并发、跨 turn 查询、attach、异步完成通知等通用任务能力。当前主线只需要 foreground synchronous barrier；后台任务会显著扩大 cancellation、ownership、audit、resume 和 UI 状态语义。
- `MCP Integration`：适合接入外部工具生态。当前已有 native/shell tools、artifact 和 skill resource path 能覆盖主线需要；MCP 一旦引入，必须先保证 raw MCP tool 不能绕过 ToolBroker、path policy、approval 和 audit。
- `Plugin Packaging`：适合分发 skills、agents、MCP config 等资源包。当前优先验证本地 skill 和 named agent 的 runtime 合同；过早引入 plugin 会增加版本、覆盖、依赖、安装顺序和动态加载边界。
- `Tool-Call Cache`：适合缓存昂贵或重复的工具调用。当前调试流程更需要可审计、可复现的真实执行结果；缓存会引入失效、命中可解释性、side effect classification 和 artifact provenance 问题。Runtime trace 和 artifact store 记录事实，供 agent 和用户复查；它们不是自动复用旧 tool result 的缓存层。
- `Memory System`：适合跨 session 的长期偏好、经验或项目知识。当前恢复真值由 checkpoint、event log、artifact 和 runtime-owned continuity state 提供；长期 memory 容易把不可审计的历史语义混入当前调试决策，也可能污染 subagent isolation 和 schema-driven 业务输入。
- `Hook System`：适合在 lifecycle 事件上扩展行为。当前所有高风险动作应通过显式 ToolBroker 和 runtime path 执行；hook 会形成隐式控制流，增加审计和安全边界复杂度。
- `OS / Container Sandbox`：适合强制阻断 opaque wrapper、脚本或外部二进制内部副作用。当前主线先依赖 ToolBroker、path policy、shell policy 和 approval；只有当这些策略无法接受真实工具副作用时，才需要引入更重的 OS/container 隔离。v1/v2 不要求容器、chroot、Windows Job Object 或文件系统虚拟化。

Deferred 的共同规则：

- 不能作为当前主线能力的隐含前置条件。
- 不能通过 dormant scaffolding 预先进入 runtime core。
- 不能绕过已经接受的 runtime truth、ToolBroker、checkpoint、event log、artifact 和 policy 边界。
- 一旦重新评估，必须先明确该模块新增的状态、事件、恢复语义、安全边界和失败模式。

## Document Relationship

- `docs/project-contract.md` 定义项目级实现合同和硬边界。
- 当前 phase 文档定义该 phase 的 scope、spec、tests 和 operations。
- 单篇 ADR 记录具体架构决策、替代方案和取舍。
- 本 overview 只串联 accepted ADRs 和跨 ADR 的设计 rationale。

如果本文档与 project contract、当前 phase 文档或 accepted ADRs 发生冲突，应停止实现并请求澄清，不应按优先级静默覆盖。
