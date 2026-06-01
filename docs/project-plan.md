# debug-agent 技术方案与路线图（project-plan.md）

> 本文档是跨阶段总计划。实现工作仍以 `docs/project-contract.md`、当前 phase
> 文档和 accepted ADRs 为更高优先级契约。
>
> Phase 0、Phase 0.5 和 Phase 1 已完成并已有对应 phase 文档与实现；本文档保留
> 这些阶段的已完成边界，不用重排方案重新解释或扩展它们。
>
> Phase 2 起的路线按 Phase 1 完成后的批准重排方案执行：v1 先完成
> RenderDoc Debug Runtime，v2 再完成 Prompt-Skill Driven Shader Loop。

## Summary

`debug-agent` 是一个本地长流程调试 agent runtime。项目主线收窄为：优先稳定支撑
当前仓库 `.debug-agent/skills/` 下的真实调试流程，而不是构建通用万能 agent 平台。

当前里程碑：

- **v1: RenderDoc Debug Runtime**
  - Phase 范围：Phase 0-4。其中 Phase 0/0.5/1 已完成，Phase 2-4 交付本次重排后
    剩余的 v1 能力。
  - 目标：`debug-agent` 能正常运行 `.debug-agent/skills/renderdoc-gpu-debug`。
  - 能力重点：短时结构化 `rdc` 命令、`view_image` 专用视觉分析、runtime-owned
    Todo Plan、轻量 session control。
- **v2: Prompt-Skill Driven Shader Loop**
  - Phase 范围：Phase 5-6。
  - 目标：完成 `.debug-agent/skills/shader-debug-loop` 及其 named subagents 对
    `debug-agent` 平台的业务层适配，并通过 fake Ralph Loop readiness 验证
    prompt-skill 路线的最小可行性。
  - 能力重点：继续走普通 prompt skill 路线，不要求 workflow skill；在 v1 平台能力
    之上补齐 Basic Subagent Framework。

核心路线调整：

- Runtime Core 自研，LangChain/LangGraph 只作为可替换的 `AgentLoopAdapter`。
- Phase-first delivery：每个 phase 都交付可运行、可测试的垂直切片。
- Phase 0/0.5/1 已交付的 CLI、TUI、prompt skill、ToolBroker、安全策略、context
  compression 和 persistence 边界保持不变。
- v1/v2 主线正式从 workflow-first 改为 prompt skill + Todo Plan + foreground
  named subagent。
- Workflow Runtime 从 v1/v2 必经主线移出，作为 deferred architecture module 保留；
  后续仅在真实需求触发时重新评估。
- v1/v2 不实现 PTY、interactive terminal、long-running shell runtime、通用
  tool-call cache、MCP 或 plugin packaging。
- Runtime core 不写 shader 专用逻辑；RenderDoc、Ralph Loop、业务 schema 和业务
  trace 由 skill 负责。

## Core Architecture

主线架构层：

1. `CLI Entrypoint`：`debug-agent`、REPL/TUI、one-shot、status、trace、后续 resume
   和 registry 查询。
2. `Runtime Orchestrator`：创建 session/run，调度 prompt agent、后续 foreground
   subagent、session control。
3. `Registry`：发现并冻结 prompt skills；Phase 5 起发现并冻结 named agents。
4. `Prompt Agent Runtime`：执行主 prompt agent、后续 foreground named subagent、
   runtime-supplied active skill context。
5. `Execution Services`：ToolBroker、Approval、CommandRunner、TraceWriter、
   SchemaValidator 边界能力。
6. `Persistence Services`：SessionStore、RunStore、EventStore、CheckpointStore、
   ArtifactStore、skill/context/approval snapshots。

Deferred architecture modules：

- `Workflow Runtime`：后续在 prompt skill 路线无法稳定遵循 Ralph Loop，或需要跨
  turn/session 恢复 workflow step 时重新评估。
- `Task / Background Task System`：后续在需要并行测试、跨 turn 查询/attach、异步
  触发后续 agent 行为时重新评估。
- `MCP Integration`：后续在 `shader-nav` 成为验收硬依赖或需要外部工具生态接入时
  重新评估。
- `Plugin Packaging`：后续在需要跨项目分发 skills、agents、MCP config 时重新评估。
- `Tool-Call Cache`、`Memory System`、`Hook System`、`OS / Container Sandbox`：不进入
  v1/v2 主线。

核心执行模型：

- `Session = runtime container`。
- `Run = task / execution domain`。
- 一个 project root / git worktree 同时最多允许一个 active session。
- 一个 session 同时只允许一个 active runner。
- REPL 默认启动长寿命 prompt run。
- one-shot 默认启动单次 prompt run。
- Phase 0.5 起，TTY REPL 默认启动轻量 TUI；one-shot、非 TTY 和注入 I/O 场景保持
  纯 stdout/stdin。
- Phase 5 起，主 prompt run 可以通过 brokered `task` 工具启动 foreground named
  subagent child run。
- v2 只支持 foreground synchronous subagent barrier，不支持 parallel/background
  subagents、task graph 或 subagent 再启动 subagent。
- Workflow run 和 workflow handoff 不属于 v1/v2 主线。

核心状态分类：

- `authoritative state`：恢复执行所需的结构化真值，例如 session/run/status、
  checkpoint、active skills、Todo Plan、approval grants、frozen snapshots。
- `compressible observations`：只供 LLM 理解的历史，例如普通对话、工具观察、已加载
  skill reference 输出。
- `artifacts`：大文件、日志、图片、`.rdc`、diff、报告，由 ArtifactStore 管理。

## Runtime Contracts

Runtime Core 不依赖具体 agent framework。

最小对象：

```python
class Session:
    session_id: str
    workspace_root: str
    status: str
    approval_mode: str
    active_run_id: str | None
    artifact_root: str
    config_snapshot: dict
    latest_checkpoint_id: str | None
    version: int
```

```python
class Run:
    run_id: str
    session_id: str
    parent_run_id: str | None
    run_type: str  # prompt | subagent
    status: str
    active_skills: list[str]
    latest_checkpoint_id: str | None
    context_snapshot_id: str | None
```

```python
class RunEvent:
    event_id: str
    timestamp: str
    session_id: str
    run_id: str
    kind: str
    payload: dict
```

```python
class ToolResult:
    status: str  # ok | error | denied | timeout | cancelled
    output: str | dict | None
    error: dict | None
    artifacts: list[str]
    metadata: dict
    redacted_output: str | None
```

错误分类包含：

- `user_error`
- `config_error`
- `policy_denied`
- `tool_error`
- `model_error`
- `internal_error`
- `timeout`
- `cancelled`
- Phase 1 起：`context_limit_exceeded`
- Phase 1 起：`compression_failed`

Phase 2+ 兼容规则：

- 任何改变 runtime truth schema、snapshot shape、event kind 集合、artifact metadata
  contract、run type/status 集合或 tool result contract 的 phase，都必须 bump SQLite
  `PRAGMA user_version`。
- runtime 启动、`status`、`trace` 和 active ownership 解释 runtime truth 之前，必须
  先检查 schema version。
- missing、legacy、unknown 或不匹配的 schema version 必须 fail closed，并返回
  `config_error`。
- runtime 不自动迁移、删除、重写旧 `.sessions/runtime.db`。
- 用户提示必须说明当前 phase 不支持旧 runtime database，并要求用户移动或删除
  `.sessions/`，或使用 fresh workspace。

## CLI And Modes

CLI 入口：

- `debug-agent`：进入 REPL。
- `debug-agent -p "..."`：执行 one-shot。
- `debug-agent status <session_id>`：查询 session/run 状态。
- `debug-agent trace <session_id>`：输出 trace。
- `debug-agent resume <session_id>`：Phase 3 起支持重开特定 terminalized long-lived
  prompt session。

REPL slash commands：

- `/status`
- `/exit`
- `/skills`：Phase 1 起。
- `/tools`：Phase 1 起。
- `/compress`：Phase 1 起。

Slash command 由 CLI/REPL 本地解析，不进入 LLM。

审批模式：

- `normal`
- `semi-auto`
- `yolo`

Phase 1 已固定默认行为：

- REPL default approval mode 是 `normal`。
- one-shot default approval mode 是 `normal`。
- 用户可通过 CLI approval-mode option 显式选择 `normal`、`semi-auto` 或 `yolo`。
- TTY REPL 用户可在 idle 状态用 `Ctrl+Y` 按
  `normal -> semi-auto -> yolo -> normal` 切换。
- `yolo` 不绕过 ToolBroker、schema validation、path policy、shell policy、timeout、
  artifact handling 或 audit。

## Completed Phase Boundaries

### Phase 0: Minimal Runtime Slice

Phase 0 已完成。目标是跑通最小 CLI agent，并产生可恢复 session 记录。

已实现边界：

- CLI：`debug-agent`。
- CLI：`debug-agent -p "..."`。
- CLI：`debug-agent status <session_id>`。
- CLI：`debug-agent trace <session_id>`。
- REPL slash：`/status`、`/exit`。
- SQLite `.sessions/runtime.db`。
- `SessionStore`、`RunStore`、`EventWriter`、`CheckpointStore`、`ArtifactStore`。
- `ModelFactory`。
- `LangChainAgentLoopAdapter`。
- `PromptAgentExecutor`。
- 最小 `ToolBroker`。
- read-only native tools：`read_file`、`list_dir`、`search_text`、`git_status`。
- workspace active session ownership。
- engine log 和 trace rendering。

Phase 0 不做：

- skill registry 或 `activate_skill`。
- prompt skill injection。
- subagent。
- workflow。
- MCP。
- plugin。
- `/compress`。
- `/resume`。
- writable native tools。
- shell execution as a general tool。
- same-workspace concurrent active sessions。
- hot reload。

### Phase 0.5: Lightweight TUI And Streaming REPL

Phase 0.5 已完成。目标是在不改变 Runtime Core、Session/Run 模型、ToolBroker、
安全边界和持久化语义的前提下，为 REPL 提供轻量、稳定、可观测的命令行 TUI。

已实现边界：

- TTY REPL 默认使用轻量 TUI。
- one-shot 保持 plain stdout 行为。
- non-TTY、injected I/O 和 prompt_toolkit 初始化失败 fallback 到 `PlainReplView`。
- `ReplView` protocol。
- `ReplController`。
- `PromptToolkitReplView`。
- `PlainReplView`。
- prompt history。
- `Ctrl+J` multiline input，`Shift+Enter` best-effort。
- active turn input disablement。
- user/model/tool/slash/error/system message rendering。
- turn status、elapsed seconds、bottom status bar、session close summary。
- `AgentLoopAdapter.stream(...)`。
- `LangChainAgentLoopAdapter` native `model.stream()` path。
- non-streaming `invoke()` fallback。
- `PromptAgentExecutor.run_turn(..., agent_stream_callback=...)`。
- `AgentStreamEvent` to view event conversion。

Phase 0.5 不做：

- skill registry 或 `activate_skill`。
- prompt skill injection。
- subagent。
- workflow。
- MCP。
- plugin。
- approval UI popups。
- trace/diff/workflow viewer。
- session browser。
- cross-session prompt history persistence。
- mid-call cancel propagation。
- block-level incremental Markdown rendering。
- changes to Session/Run/Event/Checkpoint/Artifact runtime contracts。
- changes to ToolBroker、Approval、Path Policy 或 AgentLoopAdapter ownership。

### Phase 1: Skills And Native Tools

Phase 1 已完成。目标是交付 prompt skills、controlled native/shell tools、
session-local approval grants、path policy、shell policy 和 LLM-visible context
compression。

已实现边界：

- `SkillRegistry`。
- prompt `SKILL.md` manifest parsing。
- `references/**` file-level snapshots。
- registration-time full `SKILL.md` and reference snapshot。
- frozen-snapshot hash verification。
- `activate_skill` as a ToolBroker runtime-control tool。
- `load_skill_ref_file` as a ToolBroker runtime-control/read tool over frozen references。
- run-scoped `active_skills` persistence and audit。
- active `SKILL.md` context injection by `PromptComposer` before each model call。
- active skill content outside `/compress` scope。
- `ContextManager`。
- `ModelContextFrame`。
- `CompressionContextFrame`。
- deterministic runtime-owned token estimator。
- context snapshot storage。
- large output artifacting with summaries and artifact ids。
- old tool-result omission。
- automatic rolling conversation compression。
- `/compress` while idle。
- `ToolUseContext`。
- fixed permission decision pipeline。
- main-agent path/shell policy from `~/.debug-agent/agent.toml`。
- session-local approval grants。
- approval modes `normal`、`semi-auto`、`yolo`。
- Phase 1 model-visible tool set：
  - `read_file`
  - `list_dir`
  - `search_text`
  - `write_file`
  - `edit_file`
  - `shell_exec`
  - `activate_skill`
  - `load_skill_ref_file`
- `/skills`。
- `/tools`。
- `/compress`。
- idle-state `Ctrl+Y` approval mode cycling。

Phase 1 不做：

- `AgentRegistry`。
- `/agents`。
- `/models`。
- subagents。
- workflow execution。
- workflow skill activation。
- workflow skill manifests。
- MCP。
- plugin packaging。
- skill、agent、config 或 model hot reload。
- persistent approval grants across sessions。
- `deactivate_skill`。
- section-level progressive disclosure。
- semantic skill reference retrieval。
- token-level resume、tool-mid-flight resume 或 subagent-mid-thought resume。
- arbitrary unrestricted shell execution。
- regex-based shell policy matching。

Phase 1 compatibility：

- Phase 1 是 Phase 0/0.5 的 schema 和 safety-policy breaking change。
- Phase 1 使用 SQLite `PRAGMA user_version` 标识 schema。
- legacy runtime database fail closed with `config_error`。
- runtime 不自动迁移、删除或重写旧 `.sessions/runtime.db`。

## v1 Technical Plan

### Phase 2: view_image Vision Tool And Todo Plan

目标：先解决 v1 中最关键的“看图”和“长流程不漂移”问题。

实现内容：

- Brokered `view_image` Vision Tool。
- Runtime-Owned Todo Plan。

`view_image` 设计方向：

- 新增 model-visible native tool：`view_image`。
- `view_image` 通过 ToolBroker 执行，遵守 path policy、approval 和 audit。
- v1 输入支持授权本地 PNG 路径或 runtime image artifact id。
- v1 不支持远程 URL、cache hit、Anthropic-compatible vision path 或 fallback vision
  path。
- 主路径是 `view_image(local_path)`：本地 PNG 路径通过 ToolBroker path policy、
  approval 和 audit 后直接作为图片 source。
- `view_image` 记录 MIME type、byte size、SHA-256、width、height 等 metadata。
- `view_image` 内部临时 base64 编码图片，通过 OpenAI-compatible multimodal provider
  path 发起独立 vision model call。
- `view_image` 使用独立 multimodal 配置路径，不复用主 agent provider 配置。
- Phase 2 spec 可使用以下 multimodal 配置草案作为输入，而不是把该形状视为当前
  contract：

```toml
[multimodal.defaults]
provider = "openai"
model = "kimi-k2.5"
timeout_seconds = 60
max_tokens = 4096

[multimodal.auth]
api_key_env = "OPENAI_API_KEY"

[multimodal.providers.openai]
base_url_env = "OPENAI_BASE_URL"
```

- Kimi K2.5 可以作为 Phase 2 spec 的可选默认目标模型；最终模型选择由 Phase 2
  spec 固定。
- 图片 bytes/base64/image content part 不进入主 agent 普通历史消息。
- 主 agent 后续只看到结构化语义观察、图片 metadata 和错误事实。
- `view_image` tool result schema 应至少评估以下结构化观察字段：
  - `summary`。
  - `salient_findings`。
  - `visible_text`。
  - `renderdoc_relevance`。
  - `uncertainty`。
  - `source_image_metadata`。
- runtime trace 应记录 `view_image` tool call、vision model、图片 metadata、耗时、
  成功/失败事实和摘要；不得记录图片 base64。
- 本地路径输入不承诺自动复制进 artifact store。
- artifact id 输入只解析 runtime 已登记 image artifact，不能成为绕过 path policy
  或 `.sessions/` hard deny 的通用文件读取能力。

Todo Plan 设计方向：

- 新增 agent-scope Todo Plan 状态，实际绑定所属 `run_id`。
- 主 agent 和每个 subagent 各自拥有独立 Todo Plan，不继承、不共享、不互通。
- Todo Plan 是 runtime truth，不能从 conversation history 或 compression summary
  推断或恢复。
- 暴露单个 model-visible `todo` tool，用于整体重写当前 run 的 Todo Plan。
- `todo` 通过 ToolBroker 执行并写 tool audit。
- `todo` 是 runtime-owned audit-only 工具，类似有效的 `load_skill_resource`
  调用，在所有 approval mode 下跳过交互审批，但仍执行 schema/semantic validation、
  ToolBroker audit 和 runtime event 记录。
- Todo Plan 状态变更写专用 runtime event：
  - `todo_updated`
- 基础 Plan item 状态：
  - `pending`
  - `in_progress`
  - `completed`
- Todo Plan 最多 20 条，同一时间最多 1 条 `in_progress`。
- `items=[]` 表示清空当前 Todo Plan。
- 每次普通 model call 前注入当前 Todo Plan。
- Todo Plan 作为非持久化 `runtime_todo_plan` system segment 注入：
  - 位于 active `SKILL.md` context 之后。
  - 位于 rolling summary、retained raw conversation、live/unconsumed messages、
    tool-loop messages 和 current user input 之前。
  - segment 自带短指令，要求模型在任务状态变化或计划不再匹配当前工作时调用
    `todo` 重写计划。
- Todo Plan 参与 `ModelContextFrame` token estimate 和 context window usage。
- `/compress` 后 Todo Plan 继续可见，且不依赖 compression summary 恢复。
- REPL/TUI 可以展示简短 Todo Plan 状态，但 UI 展示不是 Todo Plan 的权威状态来源。
  默认展示标识：
  - `[o]` = `completed`
  - `[>]` = `in_progress`
  - `[ ]` = `pending`

Phase 2 spec gate：

- 必须先定义 Todo Plan 的持久化位置、checkpoint/continuity 恢复语义、event kind 和
  tool result schema。
- 必须定义 `view_image` tool result schema、audit metadata、错误处理和 multimodal
  model call 配置。
- 需要评估是否修订 `ADR 0010: ModelContextFrame As The LLM-Visible Context Boundary`
  和 `ADR 0011: Layered Context Compression For Runtime Continuity`，以记录 Todo Plan
  和 `view_image` 对 LLM-visible context / compression continuity 的影响。

不做：

- shell execution overhaul。
- session interruption semantics。
- subagent。
- workflow。
- RenderDoc readiness e2e。

### Phase 3: Session Control Light

目标：让运行中的 turn 可被用户中断，并允许用户重开已 terminalized 的长寿命 prompt
session，在新的 turn boundary 继续工作。

实现内容：

- idle `Ctrl+C`：终止 session，释放 active workspace ownership。
- running `Ctrl+C`：中断当前 turn，记录 turn-scoped cancellation fact，REPL/TUI
  回到可输入状态。
- running turn interruption 不 terminalize session，也不 terminalize 长寿命 prompt
  run。
- 当前 adapter 能支持时，model call 应观察 cancellation。
- 如果中断发生时有 active shell process，runtime 对该进程做 best-effort
  termination；这只是普通 shell safety 行为，不引入 long-running shell runtime。
- runtime trace 记录 interrupt、cancellation 和 turn result。
- 中断后，模型和用户应能看到足够错误事实，以便决定是否执行 `rdc close` 等业务
  清理。
- `debug-agent resume <session_id>` 只支持重开用户通过 `/exit` 正常关闭或 idle
  `Ctrl+C` 取消的 long-lived prompt session。
- resume 不支持进入 non-terminal session，不管它是 running、idle 还是 stale。
- resume 重新获取 active workspace ownership。
- resume 恢复 approval mode、active skills、Todo Plan、context summary 和最近且未被
  evicted 的 durable conversation continuity。
- resume 后 TUI 打开到最新底部位置；不要求恢复滚动位置、输入框草稿或 transient
  stream block。
- resume 后创建新的 prompt run，旧 terminal run 的终态不改写。
- Todo Plan 绑定 `run_id`。resume 创建新 prompt run 时，从已 terminalized 的
  long-lived prompt run 的 `TodoPlanStore` 当前计划复制主 agent Todo Plan snapshot 到
  新 run，并记录 provenance。
- Todo Plan copy-on-resume 必须发生在新 prompt run 的第一次 ordinary model call
  之前，使恢复后的 `runtime_todo_plan` segment 能稳定注入上下文。
- 复制内容包括 item order、content、status 和 activeForm；新 run 重新分配自己的
  1-based display indexes。
- 新 run 使用自己的 Todo Plan version 序列。初步方向是复制后从 `plan_version = 1`
  开始，而不是复用 source run 的 plan version。
- resume 必须写结构化 continuity/provenance event，至少记录 source run id、source
  plan version、new run id、new plan version 和 item counts。
- resume 不得从 conversation history、compression summary、trace 或 TUI 状态恢复
  Todo Plan。

Phase 3 spec 阶段待评估的范围扩张：

- 在既定 Session Control Light 之外，补充错误恢复机制设计。这里的错误恢复指
  runtime 对失败 turn、provider/tool timeout、`model_error`、cancellation 和
  terminalized long-lived prompt session 的结构化记录、可解释呈现、session
  继续/终止/恢复控制，以及 resume 后向 agent 暴露最近失败事实的方式。
- 错误恢复机制不等同于自动 retry。Phase 3 不应默认引入 runtime-level tool/model
  retry、generic step retry、workflow retry policy 或业务状态机。是否再次调用某个
  tool/provider 仍应由 prompt skill 或主 agent 显式决定，runtime 只提供清晰的恢复
  边界和审计事实。
- 评估将 timeout 配置来源归拢成紧凑的 runtime 配置列表，避免普通 native tools、
  `shell_exec`、主模型调用、compression model call、`view_image` 等各自分散管理。
  初步方向是通过 `config.toml` 暴露统一 timeout profile，例如区分
  `native_tool_seconds`、`shell_seconds`、`model_seconds`、
  `compression_model_seconds` 和 `vision_model_seconds`；具体命名、默认值、兼容性和
  schema version 影响由 Phase 3 specs 决定。
- 评估哪些 model-visible tools 允许在 tool input schema 中提供 `timeout_seconds`。
  如果开放，runtime 必须用配置上限进行 cap，并把 effective timeout 纳入
  ToolBroker audit、approval scope signature 和 trace。主 agent model call 的 timeout
  不应由模型自我修改；它仍应由 runtime/session config 控制。

不做：

- `/cancel`。
- full execution resume 或 attach。
- non-terminal session attach。
- one-shot session resume。
- startup/config/schema failure resume。
- stale active ownership recovery。
- token-level resume。
- tool-mid-flight resume。
- subagent cancellation。
- PTY shell。
- long-running shell runtime。
- 自动 retry 或 generic step-level retry。

### Phase 4: RenderDoc Debug Readiness Validation

目标：验证 v1 达到 RenderDoc Debug Runtime 标准。

实现内容：

- 修改 `renderdoc-gpu-debug`，移除旧平台图片工具名和旧 shell 假设。
- 将图片查看流程适配为 `view_image`。
- 将 RenderDoc/`rdc` 命令示例适配为 debug-agent 的短时结构化 `shell_exec` 语义。
- 明确 `rdc open/status/close` 管理的是 `rdc` 自己的 daemon-backed inspection
  session，不是 runtime-owned session truth。
- fake `rdc` CI scenario：
  - `rdc doctor`
  - `rdc open sample.rdc`
  - `rdc info --json`
  - `rdc draws --limit 20`
  - `rdc rt ... -o output.png`
  - `view_image output.png`
  - `rdc close`
- 如果 readiness 中发现 Phase 1 `shell_exec` 对 Windows argv/cwd、exit code、
  duration metadata、artifact registration 或 PNG output path 有缺口，只做支撑
  fake/real smoke 的最小兼容修补。
- Windows + `rdc` real smoke 是 v1 完成硬门槛。该 smoke 只验证 `rdc` CLI 与
  debug-agent 平台集成，不要求 RenderDoc UI 交互或 RenderDoc GUI smoke。

验收标准：

- `renderdoc-gpu-debug` 中的旧图片工具名、关键路径和 shell 调用假设已适配
  debug-agent。
- fake `rdc` e2e 使用短时结构化 `shell_exec` 命令序列，不依赖 PTY、interactive
  shell、long-running shell runtime 或 tool-call cache。
- fake `rdc` e2e 在 CI 中通过。
- Windows + `rdc` real smoke 通过。
- Phase 4 完成后标记：

```text
v1: RenderDoc Debug Runtime completed
```

不做：

- `shader-debug-loop`。
- subagent。
- workflow。
- shader patch loop。
- shader-specific runtime validators。
- long-running shell runtime。

## v2 Technical Plan

### Phase 5: Basic Subagent Framework

目标：完成 v2 的基础子代理框架，使主 prompt agent 可以通过 `task` 启动 named
subagent，并保持 policy、tool、skill、trace 边界清晰。

实现内容：

- `AgentRegistry`。
- 发现并冻结 named agent 配置。
- 发现路径：
  - `~/.debug-agent/agents/<agent-name>.toml`
  - `<workspace_root>/.debug-agent/agents/<agent-name>.toml`
- project agent 覆盖 global agent；同名 agent 整体覆盖，不做文件级或字段级 merge。
- v2 需要的 named agents：
  - `renderdoc-debugger`
  - `shader-debugger`
- agent 配置至少声明：
  - agent name。
  - system prompt。
  - path policy。
  - shell policy。
  - max turns。
  - max time。
- agent 配置不声明 provider/model；subagent 使用主 agent provider/model。
- agent 配置不声明 allowed_tools/disallowed_tools；v2 通过 path policy、shell policy、
  runtime-control policy 和 prompt-level 业务约束表达权限。
- agent 配置不声明 output schema；业务输出结构由 skill 和业务脚本校验。
- 最小 agent TOML 形状作为 Phase 5 spec 输入：

```toml
name = "shader-debugger"
system_prompt = "..."

max_turns = 20
max_time_seconds = 600

[path_policy]
deny = []

[shell_policy]
allow = []
deny = []
```

- agent snapshot 在 session 启动时冻结，修改 agent TOML 后必须启动新 session 才
  生效。
- agent snapshot 至少持久化：
  - `agent_snapshot_id`。
  - `session_id`。
  - `run_id`。
  - `agent_name`。
  - `source_scope`：`project` 或 `global`。
  - `source_path`。
  - `raw_toml_content`。
  - `normalized_config_json`。
  - `effective_policy_json`。
  - `config_content_hash`。
  - `policy_profile_hash`。
  - `created_at`。
  - `version`。
- `config_content_hash` 基于 normalized config：UTF-8、LF line ending、路径规范化、
  canonical JSON、字段稳定排序、SHA-256。
- `policy_profile_hash` 基于最终生效 policy facts：主 agent policy 加上 subagent
  strict delta 后的 canonical JSON、SHA-256。
- agent policy merge 只允许 subagent 收紧主 agent：
  - subagent 可以新增 path deny。
  - subagent 不可以新增 path trust。
  - subagent 可以新增 shell deny。
  - subagent 可以把 shell allowlist 收窄。
  - subagent 不可以放宽主 agent shell deny。
  - subagent 可以降低 `max_turns` 和 `max_time_seconds`。
  - subagent 缺省时继承主 agent policy。
  - 如果 subagent 配置试图放宽主 agent policy，启动时以 `config_error` fail
    closed。
- 主 agent policy 是 capability ceiling。运行 `shader-debug-loop` 时，主 agent config
  必须已经允许 v2 所需最大能力，例如 `renderdoc-debugger` 需要的 `rdc` 执行能力；
  named subagent 只能从该上界收紧，不能通过 subagent config 额外开权。
- 启动 subagent 时创建 child run：
  - `run_type = "subagent"`。
  - `parent_run_id` 指向主 prompt run。
  - 记录 subagent name、policy profile hash、active skills、approval mode。
- 每个 subagent child run 拥有自己的独立 Todo Plan；主 agent、不同 subagent 之间
  的 Plan 不继承、不共享、不互通。
- 新增 model-visible `task` 工具。
- `task` 通过 ToolBroker 暴露给主 agent，遵守 runtime-control approval 和 audit。
- subagent 的 model-visible tool set 不包含 `task`。
- subagent 内部工具调用继续经过 ToolBroker。
- subagent 继承当前 session approval mode，但使用自己的 frozen policy facts。
- runtime 不增加 subagent-specific skill allowlist；subagent 可使用普通 prompt skill
  activation 机制。
- runtime 不增加 default active skill 机制；subagent 是否激活哪个 skill 由其 system
  prompt 和普通 prompt skill activation 语义驱动。
- `task` 是 foreground synchronous barrier；每次只运行一个 child run，主 agent
  等待其完成后继续。
- `task` 支持 timeout 和 `max_retries` 参数，但 runtime 不替业务决定是否 retry。
- runtime 保留 `task.max_retries` 作为通用平台参数；`shader-debug-loop` 的 Phase A/B
  业务协议必须不设置 retry 或显式设置 `max_retries = 0`，subagent failure 立即
  REPORT_FAIL。
- `task` 返回 raw output、status、error facts、artifact refs、attempt、duration 和
  child run id。
- `task` 输入 schema 作为 Phase 5 spec 输入：

```json
{
  "type": "object",
  "properties": {
    "subagent_name": {"type": "string"},
    "prompt": {"type": "string"},
    "timeout_seconds": {"type": "integer", "minimum": 1},
    "max_retries": {"type": "integer", "minimum": 0}
  },
  "required": ["subagent_name", "prompt"],
  "additionalProperties": false
}
```

- `timeout_seconds` 缺省时使用 agent `max_time_seconds`；`max_retries` 缺省为 `0`。
- `task` 返回 schema 作为 Phase 5 spec 输入：

```json
{
  "status": "ok | error | timeout | cancelled | policy_denied",
  "subagent_name": "shader-debugger",
  "child_run_id": "run_xxx",
  "attempt_count": 1,
  "duration_ms": 12345,
  "output": "raw output string or artifact_ref object",
  "summary": "short platform summary",
  "artifacts": ["art_xxx"],
  "error": {
    "error_class": "model_error",
    "message": "...",
    "source": "subagent"
  },
  "metadata": {
    "output_bytes": 123,
    "output_artifact_id": null,
    "policy_profile_hash": "sha256:..."
  }
}
```

- raw output 不超过 16 KiB 时放入 `output`；超过 16 KiB 时写入 text artifact，
  `output` 设置为 artifact reference 对象，`summary` 写 runtime 平台摘要，
  `artifacts` 和 `metadata.output_artifact_id` 引用同一 artifact。
- artifact reference 对象至少包含 `type = "artifact_ref"`、`artifact_id`、`sha256`、
  `bytes` 和 `mime_type`；`artifact_id` 是后续读取内容的定位符，`sha256` 只用于完整性
  校验，不能替代 artifact id。
- `summary` 是 runtime 生成的平台摘要，不做业务判断，长度保持在几百字符级别。
- `error.error_class` 只描述平台错误：`config_error`、`policy_denied`、`timeout`、
  `cancelled`、`model_error`、`tool_error`、`context_limit_exceeded`、`internal_error`。
- child run 的完整 model/tool 事实保留在 runtime trace/event log；`task` result 只是
  主 agent 可消费的稳定摘要入口。

Phase 5 spec guidance：

- Phase 5 复杂度较高，正式 phase spec 应拆成多个 milestone，例如：
  AgentRegistry/snapshot、child run lifecycle、`task` brokered tool、subagent policy
  merge、subagent prompt skill activation、trace/result integration。

不做：

- shader-loop e2e readiness。
- workflow。
- MCP。
- plugin。
- parallel subagents。
- background task。
- task graph。
- multi-agent planner。
- cross-session task queue。
- subagent 间通信协议。
- subagent-mid-thought resume。
- sandbox。

### Phase 6: Shader Loop Business Adaptation And Readiness Validation

目标：修改业务层 skills 和 named subagents，使 `.debug-agent/skills/shader-debug-loop`
适配 `debug-agent` 平台，并通过 fake Ralph Loop readiness 达到 v2 验收标准。

实现内容：

- 修改 `shader-debug-loop`，移除旧平台工具名和旧路径假设。
- 必要时修改 `shader-src-debug`，让 Phase B 方法论与 debug-agent agent 配置、工具名
  和 policy 语义一致。
- 新增或修改 `renderdoc-debugger` agent config。
- 新增或修改 `shader-debugger` agent config。
- 将旧 allowed/disallowed tool 语义改写为 path policy、shell policy、
  runtime-control policy 和 prompt-level 业务约束。
- fake Phase A subagent：
  - 启动 `renderdoc-debugger`。
  - 激活 `renderdoc-gpu-debug`。
  - 产生可被业务脚本校验的 fake `rdc_report`。
- fake Phase B subagent：
  - 启动 `shader-debugger`。
  - 激活或注入 `shader-src-debug`。
  - 产生 fake `shader_report` 和 `patch.<retry>.diff`。
  - 验证 `shader-debugger` shell policy 可以禁止 git。
- 明确 Phase A / Phase B prompt template 与 `task` schema 对齐。
- JSON/schema 校验保留为 skill 业务协议，runtime 不内置 shader schema。
- `validateSchema.py` 通过 frozen reference 读/执行例外调用：
  - Phase 1 hard-denies model-visible tools from live skill source roots:
    `~/.debug-agent/skills/` and `<workspace_root>/.debug-agent/skills/`。
  - Phase 6 只能为当前 session 已冻结 skill snapshot 中登记的 `references/**`
    建立收窄例外，不能把整个 live skill root 变成 trusted path。
  - 只能请求执行当前 session 已冻结 skill snapshot 中登记的
    `references/validateSchema.py`。
  - runtime 必须从 frozen reference snapshot materialize 一个受控临时/业务目录中的
    executable copy。
  - 执行前校验 materialized copy hash 等于 frozen reference hash。
  - 不得直接读取或执行 live skill source root 下的 `validateSchema.py`。
  - 不得允许模型直接拼 live skill source path 执行。
  - hash mismatch、缺失、未冻结 reference、path traversal、目录外路径或
    materialization 失败必须 fail closed。
  - write/edit 对 skill source root 和 `references/**` 继续 hard deny。
  - 该例外不授权读取 live `SKILL.md` 或未冻结 source 文件；模型仍应通过 frozen
    snapshot、active skill context 和 `load_skill_ref_file` 获取 skill 内容。
  - runtime 不把 `rdc_report`、`shader_report` 或 `final_report` schema 写入 core。
- shader-loop 业务 trace/output 是 skill-owned，但输出目录必须显式通过主 agent path
  policy 授权；不得使用 `.sessions/`、live skill source root 或旧 `.codely-cli` 路径
  作为默认输出目录。
- fake Ralph Loop e2e 在单个 user turn 内完整执行：
  - apply case。
  - build。
  - test。
  - collect artifacts。
  - Phase A `task`。
  - Phase B `task`。
  - skill 调用 `validateSchema.py`。
  - apply patch。
  - report。
- fake Ralph Loop 中的 build/test 使用短时、可控的 fake command，不证明真实 Tuanjie
  build/test 的长时间执行稳定性。
- fake readiness 覆盖多轮 retry、CASE_SWITCH、subagent failure、schema validation
  failure、禁止 subagent retry 和 compression 后继续流程。
- fake readiness 必须验证 Phase A/B `task` 调用不设置 retry 或显式设置为 `0`，并在
  subagent failure 后立即 REPORT_FAIL。
- Windows + Tuanjie + RenderDoc real smoke 有文档或可选测试入口，但不作为真实 shader
  case 稳定自动修复成功的承诺。
- 如果真实 Tuanjie build/test 因 timeout、交互需求、后台查询或跨 turn attach 需求
  无法稳定运行，才重新评估 timeout profile、background task system 或 long-running
  shell runtime。
- 验证 runtime core 没有 shader 专用分支。

Phase 6 spec gate：

- 必须严格定义 frozen reference executable materialization 的执行目录、文件权限、
  hash 校验点、cwd/env、清理策略、audit metadata 和所有失败分支。
- 必须严格定义 shader-loop 业务输出目录及其 path policy 要求。
- 需要评估是否修订 `ADR 0010: ModelContextFrame As The LLM-Visible Context Boundary`
  和 `ADR 0011: Layered Context Compression For Runtime Continuity`，以记录 foreground
  subagent result、business trace 和 compression continuation 的架构边界。

验收标准：

- `shader-debug-loop` 和 `shader-src-debug` 中的旧平台工具名、关键路径和平台假设已
  适配 debug-agent。
- `renderdoc-debugger` 和 `shader-debugger` named agent 配置已适配完成。
- fake Ralph Loop e2e 在单个 user turn 内通过。
- fake Phase A/B subagent 输出可被 `validateSchema.py` 校验。
- `validateSchema.py` 通过 frozen reference materialized executable copy 运行。
- Phase A 能获得 `renderdoc-gpu-debug` skill context。
- Phase B 能获得 `shader-src-debug` skill context。
- subagent policy profile 在 fake test 中生效。
- fake readiness 不只覆盖 happy path。
- runtime core 无 shader 专用分支。
- Phase 6 完成后标记：

```text
v2: Prompt-Skill Driven Shader Loop completed
```

不做：

- 真实 shader case 在 CI 中稳定自动修复成功。
- workflow adapter。
- MCP `shader-nav`。
- plugin 分发。
- background task。
- task graph。
- OS/container sandbox。

## Deferred General Agent Modules

### Workflow Runtime

不选择原因：

- `shader-debug-loop` 当前决定继续走普通 prompt skill 路线。
- Ralph Loop 在同一 session、同一 turn 内由 skill 指令和 Todo Plan 软性控制。
- 引入 workflow runtime 会带来 step checkpoint、workflow state、resume 语义和
  workflow skill executor，开发成本高于 v2 当前收益。

后续重新评估条件：

- prompt skill 路线在真实 case 中无法稳定遵循 Ralph Loop。
- 需要跨 turn 或跨 session 恢复 workflow step。

### Task / Background Task System

不选择原因：

- `shader-debug-loop` 的 build/test/RenderDoc 检查在业务流程上是同步屏障。
- 即使后台执行，主 agent 或 subagent 也必须等待结果才能进入下一步。
- v1/v2 通过 brokered `shell_exec`、artifact、trace 和 session-control 行为覆盖当前
  主线需要。

后续重新评估条件：

- 需要并行跑多个测试项目。
- 需要启动任务后跨 turn 查询或 attach。
- 需要任务完成后异步触发后续 agent 行为。

### Tool-Call Cache

不选择原因：

- `shell_exec` 和 `rdc` 命令可能受外部 daemon-backed session、文件系统、cwd、env 和
  时间状态影响，runtime 无法安全判断通用缓存有效性。
- `view_image` 理论上可以按 image SHA-256、model、prompt/schema version 和 tool
  version 做专用缓存，但 v1/v2 当前不需要。

后续重新评估条件：

- vision 调用成本或延迟成为实际瓶颈。
- 有明确只读、无副作用、输入完整声明的专用 tool，并能定义稳定 cache key 和失效规则。

### Memory System

不选择原因：

- `shader-debug-loop` 不涉及跨会话 memory。
- 业务长期状态由 skill 的 `trace.jsonl` / `trace.md` 定义。
- 当前会话内流程状态由 Todo Plan、active skill context、tool observations 和 runtime
  trace 支撑。

后续重新评估条件：

- 需要跨 session 复用历史 shader fixes 或项目知识。
- 需要可控的长期知识库，而不是当前 skill reference 文件。

### Hook System

不选择原因：

- v2 需要的是明确的 subagent lifecycle、policy profile 和 ToolBroker 边界。
- 通用 hook 会引入隐式扩展点，增加执行顺序、权限和审计复杂度。

后续重新评估条件：

- 多个 runtime 模块都需要稳定 lifecycle extension point。
- hook 行为可以被明确纳入 ToolBroker 和 audit contract。

### MCP

不选择原因：

- `shader-src-debug` 中的 `shader-nav` 仍是 planned tool，不是 v2 必需依赖。
- Phase B 当前可以通过已有文件、搜索、编辑和 shell 能力完成。
- MCP 会引入 server lifecycle、transport、tool wrapper、配置加载和权限桥接。

后续重新评估条件：

- 决定把 `shader-nav` 作为验收硬依赖。
- 需要外部工具生态接入，并且能保证 MCP tool 不绕过 ToolBroker。

### Plugin Packaging

不选择原因：

- 当前所需 skills 已在 `.debug-agent/skills/` 下，可由本地 registry 发现。
- v2 需要的是本地 agent/skill 组合，不需要分发格式。

后续重新评估条件：

- 需要把 skills、agents、MCP config 作为可安装包分发。
- 需要跨项目复用固定调试能力包。

### OS / Container Sandbox

不选择原因：

- 现有 path policy / shell policy 已能表达 v2 的主要需求，例如 Phase B 禁止 `git`。
- v2 不声明 runtime 可硬性保证 shader 文件修改范围；该范围暂由 prompt、skill
  schema、fake readiness tests 和后续真实验证约束。
- sandbox 边界更硬，但实现成本明显高于 subagent framework 和 readiness validation。

后续重新评估条件：

- 真实运行中出现 policy 无法接受的隐藏副作用。
- 需要强制阻断 opaque wrapper、脚本或外部二进制内部的文件系统/git 操作。

## Test Plan

Phase 0/0.5/1 使用各自 phase 文档中的已完成测试计划和 operations 命令。

Phase 2+ 测试原则：

- 每个 phase 都必须先在 `docs/<phase>/tests.md` 定义 acceptance criteria。
- Operational commands 必须来自对应 phase 的 `operations.md`。
- 改变 schema/version 的 phase 必须测试 legacy database fail-closed。
- 所有 model-visible tools 必须测试 ToolBroker、path policy、shell policy、approval、
  artifact 和 audit 边界。
- 所有新 runtime truth 必须测试 persistence、checkpoint/continuity、trace/status 行为。
- fake readiness 必须覆盖 happy path 和关键失败路径。

Phase 2 重点测试：

- `view_image` path input、artifact input、policy denial、metadata、vision call audit、
  no base64 in ordinary history。
- Todo Plan `todo` schema、20 条上限、最多 1 条 `in_progress`、audit-only policy、
  `todo_updated` event persistence、compression survival、ModelContextFrame
  injection。

Phase 3 重点测试：

- running `Ctrl+C` turn-scoped cancellation。
- idle `Ctrl+C` terminalization。
- resume 只重开允许的 terminalized long-lived prompt session。
- resume active ownership reacquire。
- resume 创建新 prompt run 且不改写旧 terminal run 终态。
- resume 从 source run `TodoPlanStore` 复制 Todo Plan snapshot 到新 run，并在第一次
  ordinary model call 前重新注入 `runtime_todo_plan`。
- resume 的 Todo Plan provenance event 记录 source run id、source plan version、
  new run id、new plan version 和 item counts。
- active skills / context summary / recent conversation continuity restore。

Phase 4 重点测试：

- fake `rdc` e2e。
- `renderdoc-gpu-debug` skill adaptation。
- `rdc` short-lived structured `shell_exec` sequence。
- `view_image` over exported PNG。
- Windows + `rdc` real smoke。

Phase 5 重点测试：

- AgentRegistry discovery/snapshot。
- child run lifecycle。
- `task` brokered tool。
- subagent policy merge and fail-closed on attempted policy widening。
- subagent prompt skill activation。
- `task` result and artifact behavior。
- `shader-debugger` fake config can deny git.

Phase 6 重点测试：

- `shader-debug-loop` and `shader-src-debug` platform adaptation。
- named agent configs。
- fake Ralph Loop e2e in one user turn。
- fake Phase A/B schema validation through frozen reference materialized executable。
- subagent failure immediate REPORT_FAIL。
- schema validation failure immediate REPORT_FAIL。
- no subagent retry for Phase A/B。
- compression continuation through Todo Plan / business trace。
- runtime core has no shader-specific branches。

## Assumptions

- v1 metadata store 使用 SQLite。
- v1 artifact store 使用本地文件系统。
- 同一工作目录内不支持多 active session。
- 不支持 skill/agent/config/model hot reload。
- 不支持 token-level resume、tool-mid-flight resume、subagent-mid-thought resume。
- 不支持完整 provider abstraction；早期只保留稳定 provider path。
- `AgentLoopAdapter.run()` 是 authoritative result path；`AgentLoopAdapter.stream()` 是
  UI observation path，不改变持久化真值。
- MCP 和 Plugin 都不是 v1/v2 主路径依赖。
