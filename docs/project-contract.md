# debug-agent Project Contract

## Purpose

`debug-agent` 是一个本地长流程调试 agent runtime。它的目标不是做无边界的通用 agent 平台，而是稳定支撑当前仓库 `.debug-agent/skills/` 下的真实调试流程：RenderDoc frame inspection、shader 调试循环、长时间 build/test、失败产物收集、补丁生成、回归验证、中断恢复和可审计 trace。

新的主线目标分为两段：

- **v1: RenderDoc Debug Runtime**，覆盖 Phase 0-4，其中 Phase 3.5 是 Phase 3 与 Phase 4 之间的 runtime framework hardening phase。Phase 0/0.5/1 是已完成基础能力，Phase 2-3 补齐 `renderdoc-gpu-debug` 所需的核心平台能力，Phase 3.5 补强通用 runtime ergonomics、native tooling 和 auditability，Phase 4 只保留 RenderDoc 业务 readiness。
- **v2: Prompt-Skill Driven Shader Loop**，覆盖 Phase 5-6。v2 在 v1 平台能力上补齐基础 named subagent 能力，并让 `shader-debug-loop` 通过 fake Ralph Loop readiness 验证 prompt-skill 路线。

项目必须保持两条边界：

- Runtime Core 自主定义 session、run、tool、checkpoint、artifact、skill snapshot、Todo Plan、subagent run、event 和 trace 等核心 contract。
- LangChain/LangGraph 等 agent 框架只作为可替换的 `AgentLoopAdapter`，不能拥有 runtime 真值。

## Runtime Principles

- **Runtime state is authoritative.** 恢复执行所需状态必须结构化保存，不能依赖自然语言总结。
- **All model-visible tools pass through ToolBroker.** native tool、shell/git、runtime control、Plan tools、`view_image`、subagent `task` 和后续 MCP tool 都必须经过统一安全边界。
- **Event log and checkpoint have different jobs.** event log 负责审计事实，checkpoint 负责恢复真值，artifact store 负责大输出和外部产物。
- **UI and stream observations are non-authoritative.** REPL/TUI 和 streaming observation 只能观察 runtime，不能成为恢复真值，也不能替代 event log 或 checkpoint。
- **Phase-first delivery.** 每个 phase 都交付可运行的垂直切片，不提前横向铺满所有模块。
- **Prompt-skill-first for v1/v2.** v1/v2 主线使用 prompt skill + Todo Plan + foreground named subagent。Workflow Runtime 是 deferred architecture module，不是 v1/v2 必经主线。
- **No hidden shader coupling.** Runtime 必须能支撑 shader-debug-loop，但 runtime core 不能写 shader 专用分支。

## Architecture Layers

1. `CLI Entrypoint`：`debug-agent`、REPL/TUI、one-shot、status、trace、resume、后续 registry 查询。
2. `Runtime Orchestrator`：创建 session/run，调度 prompt agent、foreground subagent、session control。
3. `Registry`：发现 skills、agents，后续发现 MCP config 和 plugins。
4. `Prompt Agent Runtime`：执行 prompt agent、foreground named subagent、skill 动态注入。
5. `Execution Services`：ToolBroker、Approval、CommandRunner、SchemaValidator、TraceWriter。
6. `Persistence Services`：SessionStore、RunStore、EventStore、CheckpointStore、ArtifactStore。

以下模块是 deferred architecture modules，不是 v1/v2 主路径依赖：

- `Workflow Runtime`
- `Task / Background Task System`
- `MCP Integration`
- `Plugin Packaging`
- `Tool-Call Cache`
- `Memory System`
- `Hook System`
- `OS / Container Sandbox`

## Core Execution Model

- `Session` 是 runtime container。
- `Run` 是 task 或 execution domain。
- 一个 project root / git worktree 同时最多允许一个 active session。
- 一个 session 同时只允许一个 active runner。
- REPL 默认启动长寿命 prompt run。
- one-shot 默认启动单次 prompt run。
- Phase 0.5 起，TTY REPL 默认使用轻量 TUI；one-shot、非 TTY 和注入 I/O 场景保持纯 stdout/stdin。
- TUI 不改变 session、run、event、checkpoint、artifact、ToolBroker、Approval、Path Policy 或 AgentLoopAdapter 的权威状态和安全语义。
- v2 只支持 foreground synchronous subagent barrier，不支持 parallel/background subagents、task graph 或 subagent 再启动 subagent。
- Workflow run 和 workflow handoff 不属于 v1/v2 主线。

## Platform And Skill Boundary

`debug-agent` runtime 只提供平台能力：skill 发现、冻结、激活与上下文注入，ToolBroker 安全边界，path policy、shell policy、approval、timeout、artifact、audit，runtime trace，context compression，Todo Plan，session control，`view_image`，以及 foreground named subagent lifecycle。

skill 负责业务行为：RenderDoc / `rdc` 的使用方式、Ralph Loop 步骤、业务 retry 规则、业务报告、业务 trace 文件、schema 使用方式和业务输出目录。

runtime 不应内置 shader 或 RenderDoc 专用语义，例如 shader project 名称、Ralph Loop 状态机、业务报告格式、shader 专用 trace/schema validator、RenderDoc 命令白名单或固定 procedure。

## Phase Roadmap

### Phase 0: Minimal Runtime Slice

跑通最小 CLI agent，并能写 session/run/event/checkpoint。交付 REPL、one-shot、status、trace、SQLite persistence、artifact store、LangChain adapter、PromptAgentExecutor、最小 ToolBroker、workspace active session ownership。

Phase 0 不做 skill、subagent、MCP、workflow、plugin、可写工具、`/compress`。

### Phase 0.5: Lightweight TUI And Streaming REPL

补强 Phase 0 最小 REPL 的人类交互体验，交付轻量 TUI、streaming output、turn 状态、工具调用展示、token/mode 状态栏和非 TTY fallback。

Phase 0.5 是 `CLI Entrypoint / REPL UI` 层增强，不是新的 runtime 语义层。它不得引入 skill、subagent、workflow、MCP、plugin，也不得改变 ToolBroker、Approval、Path Policy、Session/Run/Event/Checkpoint/Artifact 的合同。

### Phase 1: Skills And Native Tools

支持 prompt skill、registry、受控 native/shell tools、path policy、approval grants、ContextManager、`/skills`、`/compress`。

Phase 1 是 native 可写工具的最早引入点。可写工具必须经过 ToolBroker、path policy、approval 和 audit。

Phase 1 不做 AgentRegistry、`/agents`、`/models`、subagents、workflow、MCP、plugin、skill/agent/config/model hot reload、persistent approval grants、`deactivate_skill`。

### Phase 2: view_image Vision Tool And Todo Plan

支持 brokered `view_image` 专用视觉分析和 runtime-owned Todo Plan，解决 v1 中最关键的“看图”和“长流程不漂移”问题。

Phase 2 不做 subagent、workflow、MCP、plugin 或 RenderDoc readiness e2e。

### Phase 3: Session & Failure Control Light

支持 running turn 中断、idle session terminalization、active shell process best-effort termination、用户确认的 stale running session fail-close，以及符合共同 eligibility 判定的 terminalized prompt session resume，包括 long-lived REPL prompt session 和 one-shot terminal prompt session resume into REPL。

Phase 3 同时统一 runtime error handling 控制面：集中定义 normalized error taxonomy 和固定 reason，统一 failure fact persistence 与 terminal recovery checkpoint 策略，并提供窄 runtime retry、`output_token_limit_reached` continuation 和 shell timeout config cleanup。错误分类和 reason 必须是集中定义的固定符号；具体细节放在 message 和结构化 metadata，不能由调用点临时编造 reason。

Phase 3 执行 checkpoint contract 的破坏性更新：Phase 3 prompt session/run 只写 terminal recovery checkpoint；不再写 ordinary turn、context、error、streaming、trace、UI 或其他 non-terminal provenance checkpoint/snapshot。Phase 3 新增 append-only `conversation_messages` 作为 durable conversation 主路径；运行期 in-memory conversation 只是该 durable truth 的投影。resume 只允许 explicit `debug-agent resume <session_id>` 恢复符合 eligibility 的 terminalized prompt session/run，并允许该 explicit resume 将同一 session/run lineage 从 terminal status 重新置为 `running`；其他路径不得复活 terminalized session/run。startup/config/schema failure 不可 resume；即使这类 failure 发生在 session/run 创建之后，也只能写审计 failure fact/event 并 terminalize，不能写 terminal recovery checkpoint。stale running session fail-close 只能由 user-triggered workflow 显式触发：当启动或 resume 因 active ownership blockage 发现候选 owner 已 proven-stale 时，runtime 可以在真正创建或恢复本 session 前提示用户确认；确认后才允许从 durable facts best-effort 写 terminal checkpoint、terminalize 原 session/run 并释放 ownership。当 explicit `debug-agent resume <session_id>` 的目标本身就是当前 proven-stale active owner 时，该 resume workflow 可以先对同一目标执行 user-confirmed stale-target fail-close pre-step；只有该 pre-step 已通过 `owner_token` fencing terminalize 目标、释放 ownership，并产出有效 terminal recovery checkpoint 后，才允许进入普通 resume validation。active ownership claim 必须包含 `owner_token` fencing；stale fail-close 和普通 ownership release 都只能通过匹配当前 owner facts 与 `owner_token` 的条件写入释放同一个 owner record。如果对方进程仍正常运行或 stale 证据不足，runtime 必须保持 active ownership blockage，不能 fail-close。非交互命令可以在进入后续非交互执行前复用该交互确认；无法获得确认时必须 fail closed。stale running session fail-close 不得 auto attach、auto-resume 或无确认自动释放 ownership。

Phase 3 的 retry 只允许 opt-in 的 runtime-owned transient retry。它不拥有最终失败处理策略；retry 禁用或耗尽后，由 normal error handling policy 决定是否继续 tool/model loop、中断 turn、terminalize session/run、必要时写 terminal recovery checkpoint、释放 ownership 或提示用户。允许的 retry 不得变成 generic step-level retry、已接受/已完成 model-call result 的 replay、token-level resume、tool-mid-flight resume 或默认的 runtime-level automatic tool retry。`output_token_limit_reached` continuation 不得把 partial output 当作 accepted final assistant message，也不得执行不完整 tool call。

Phase 3 的 provider cancellation 第一版保留 `AgentLoopAdapter.run()` / `stream()` public contract，不以新增 public async adapter method 作为前置条件。主模型 call 和 `view_image` provider call 都必须通过 runtime-owned cancellable worker / async provider task 执行，并通过 runtime-owned cancellation handle 支撑 best-effort local cancellation；Phase 3 不接受 sync-only provider execution fallback。provider cancellation 只能承诺本地 runtime 尝试停止等待并收束本地 truth，不能承诺远端停止执行或停止计费。

Phase 3 不做 `/cancel`、non-terminal session attach、startup/config/schema failure resume、generic step-level retry、已接受/已完成 model-call result replay、token-level resume、tool-mid-flight resume、默认 runtime-level automatic tool retry、subagent cancellation、PTY shell 或 long-running shell runtime。

### Phase 3.5: Runtime Ergonomics, Native Tooling, And Audit Hardening

集中完成与 v1 业务适配无直接绑定的 debug-agent framework 优化：归拢 runtime 常量并按模块集中定义，重新界定哪些常量允许通过 frozen `config.toml` 配置；扩展通用 native tools、优化 model-visible schema，并实现更精细的参数控制；优化 `engine.log` 和 `trace.md`，提高人工审计、失败回溯和长流程调试效率；持续优化 REPL/TUI 交互体验。

Phase 3.5 只能补强通用 runtime、ToolBroker/native tool、observability/audit 和 REPL/TUI 层能力。它不得引入 RenderDoc、Ralph Loop 或 shader 专用 runtime 语义；不得把业务报告格式、RenderDoc 命令 allowlist、shader schema validator 或业务 trace 规则写入 runtime core；不得改变 TUI 和 streaming observation 的非权威地位。native tool 增强仍必须经过 ToolBroker、schema validation、path policy、approval、artifact handling 和 audit。新增或调整 config 项必须纳入 session frozen config snapshot；任何 runtime truth schema、event kind、tool result contract、error payload、artifact metadata 或 checkpoint 语义变化都必须按 Phase 2+ 兼容规则处理。

Phase 3.5 不做 `renderdoc-gpu-debug` 业务适配、fake `rdc` CI scenario、Windows + real `rdc` smoke、`shader-debug-loop`、subagent、workflow、MCP、plugin、PTY shell 或 long-running shell runtime。

### Phase 4: RenderDoc Debug Readiness

验证 runtime 能承载 `renderdoc-gpu-debug`。适配该 prompt skill，验证短时结构化 `rdc` 命令序列、fake `rdc` CI scenario，并通过 Windows + `rdc` real smoke 作为 v1 完成硬门槛。Phase 4 只处理 RenderDoc 业务 readiness 和业务层适配调试，不承担通用 framework hardening。

Phase 4 不做通用常量/config 重构、通用 native tool framework 扩展、通用 engine log/trace overhaul、通用 REPL/TUI 优化、`shader-debug-loop`、subagent、workflow、shader patch loop、shader-specific runtime validators 或 long-running shell runtime。

### Phase 5: Basic Subagent Framework

支持 named agent discovery、frozen snapshots、foreground child run lifecycle、brokered `task` tool、subagent policy profile、subagent ToolBroker/audit/artifact integration。

Phase 5 不做 shader-loop e2e readiness、workflow、MCP、plugin、parallel/background subagents、task graph 或 sandbox。

### Phase 6: Shader Loop Business Adaptation

适配 `shader-debug-loop`、`shader-src-debug`、`renderdoc-debugger` 和 `shader-debugger`，并通过 fake Ralph Loop readiness 达到 v2 验收标准。

Phase 6 不做 workflow adapter、MCP `shader-nav`、plugin 分发、background task、task graph 或 OS/container sandbox。

## Compatibility Rule For Phase 2+

从 Phase 2 起，任何改变 runtime truth schema、snapshot shape、event kind 集合、artifact metadata contract、run type/status 集合或 tool result contract 的 phase，都必须 bump SQLite `PRAGMA user_version`。

runtime 启动、`status`、`trace` 和 active ownership 解释 runtime truth 之前，必须先检查 schema version。missing、legacy、unknown 或不匹配的 schema version 必须 fail closed，并返回 `config_error`。除当前 phase 文档明确批准的破坏性 schema reset 外，runtime 不自动迁移、删除、重写旧 `.sessions/runtime.db`。

Phase 3 是该规则的显式例外：Phase 3 startup 可以在解释任何 legacy runtime truth 前删除 missing/legacy Phase 0/0.5/1/2 `.sessions/runtime.db`，撤销 legacy checkpoint/context schema，并创建 fresh Phase 3 database。Phase 3 不迁移、不解释、不重写 legacy rows；`status`、`trace`、`resume` 和未知 future schema 仍必须 fail closed，不能为了查询或恢复自动删除数据库。

用户提示必须说明当前 phase 不支持旧 runtime database。默认提示要求用户移动或删除 `.sessions/`，或使用 fresh workspace；如果当前 phase 文档明确批准 destructive schema reset，则提示必须说明 reset/delete 行为及其影响。

具体新增 schema、事件、状态、tool result 字段和恢复语义必须在对应 phase 的 `scope.md` / specs / tests 中定义。错误 payload、error reason 集合、tool result contract、event/checkpoint payload 语义或 retry metadata 的变化也属于 runtime truth / contract 变化，必须按本规则处理。

## Phase Spec Gates

每个未完成 phase 在进入实现前，必须先建立完整的 `docs/<phase>/` 文档集：

- `scope.md`：定义本 phase 的目标、范围、验收边界和明确不做项。
- `architecture.md`：定义本 phase 对现有架构层、模块职责和数据流的影响。
- `specs/*`：定义本 phase 新增或改变的 runtime truth、tool contracts、persistence、policy、failure handling 和 user-visible behavior。
- `tests.md`：定义 acceptance criteria、必须覆盖的测试场景和 legacy/fail-closed 要求。
- `operations.md`：定义本 phase 的 canonical verification commands。
- `implementation-plan.md`：定义按 milestone 顺序执行的实现计划；实现必须按该顺序推进，不能跳到未来 phase scope。

Phase spec 必须先定义清楚本 phase 新增或改变的 runtime truth、event kinds、checkpoint/continuity 语义、model-visible tool input/output/error/audit contract、schema/version 影响、acceptance criteria 和 canonical verification commands。

Phase spec 只能细化和收窄 project contract，不能扩展或违反 project contract，也不能把 deferred modules 作为隐含前置条件。

## Deferred Module Re-evaluation

Workflow Runtime 后续只有在 prompt skill 路线无法稳定遵循真实长流程，或需要跨 turn / 跨 session 恢复 workflow step 时，才重新评估。

Task / Background Task System、MCP、Plugin Packaging、OS / Container Sandbox 等 deferred modules 只有在真实需求触发后才可重新评估。若要进入 v1/v2，必须先经人工批准修改 `docs/project-contract.md`；不得只通过 phase 文档把 deferred module 纳入 v1/v2 scope。

`SchemaValidator` 只表示 runtime/tool/config schema validation 边界，不表示 runtime 内置 shader 业务 schema validator。`rdc_report`、`shader_report`、`final_report` 等业务 schema 属于 skill 业务协议。

## Long-Term Constraints

- v1 metadata store 使用 SQLite。
- v1 artifact store 使用本地文件系统。
- v1/v2 不支持同一工作目录多 active session。
- v1/v2 不支持 skill/agent/config/model hot reload。
- v1/v2 不支持 token-level resume、tool-mid-flight resume、subagent-mid-thought resume。
- v1/v2 不支持完整 provider abstraction；早期只保留稳定 provider path。
- `AgentLoopAdapter.run()` 是 authoritative result path；`AgentLoopAdapter.stream()` 是 UI observation path，不改变持久化真值。Phase 3 provider cancellation 可以在 adapter 内部引入 async task 和 runtime-owned cancellation handle，但不得把 agent framework 或 stream observation 变成 runtime truth。
- MCP 和 Plugin 都不是 v1/v2 主路径依赖。

## Non-Goals For v1/v2

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
- Postgres 或云 artifact store。
- 同一工作目录内多 session 并行。
- PTY、interactive terminal 或 long-running shell runtime。
- 通用 tool-call cache。
- shader 专用 runtime validator。
- shader 专用 patch/diff tool。

## Source Of Truth Order

实现工作必须按以下来源理解需求：

1. `docs/project-contract.md`
2. 当前 phase 文档，例如 `docs/phase-2/*`
3. accepted `docs/adr/*`

当前 phase 文档用于细化和收窄 project contract 在该 phase 的行为、范围、验收和操作命令。phase 文档不得扩展或违反 project contract。

如果 project contract、当前 phase 文档和 accepted ADRs 之间产生冲突，不得按优先级静默覆盖；必须停下并请求澄清。

`docs/project-plan.md` 是历史规划上下文，不是实现真值来源，不得用于扩展 active phase scope。
