# debug-agent 技术方案与路线图（project-plan.md）

> 本文档是跨阶段总计划，由原根目录 `spec.md` 移动并重命名而来。Phase 0 编码以 `docs/project-contract.md` 和 `docs/phase-0/*` 为更高优先级契约；这些 Phase 文档可以细化或收窄本文档中的 v1 总体字段与后续阶段能力。

## Summary

本方案替代旧版 `Plan.md`。旧方案中可沿用的内容已合并：CLI 形态、Session/Run 模型、skill/agent 发现、prompt skill 动态激活、subagent、MCP 后置扩展、审批模式、ToolBroker、安全策略、持久化、session control、workflow runtime、测试计划和 phase roadmap。

新版方案的核心调整：

- Runtime Core 自研，LangChain/LangGraph 只作为 `AgentLoopAdapter`。
- Workflow 第一版采用 code-first，不提前设计复杂 YAML DSL。
- Phase 按垂直可运行切片推进。
- 在 Phase 0 和 Phase 1 之间加入 Phase 0.5：轻量 TUI 与 streaming REPL，用于补强人类可观测交互体验，但不改变 runtime 真值模型。
- MCP 不在 v1 主线中实现，后移为 shader-debug readiness 之后的可选扩展。
- Plugin 后移为可选静态打包层。
- 持久化采用 SQLite `event log + checkpoint snapshot`。
- 目标是支撑 `shader-debug-loop`，但 runtime 不写死 shader 专用逻辑。

## Core Architecture

系统分为 7 层：

- `CLI Entrypoint`：`debug-agent`、REPL/TUI、one-shot、resume、status、trace、registry 查询。
- `Runtime Orchestrator`：创建 session/run，解析 skill 路由，调度 prompt/subagent/workflow executor。
- `Registry`：发现 skills、agents，后续可发现 MCP config 和 plugins。
- `Agent Runtime`：执行 prompt agent、subagent、skill 动态注入。
- `Workflow Runtime`：执行 code-first deterministic workflow。
- `Execution Services`：ToolBroker、Approval、CommandRunner、SchemaValidator、TraceWriter。
- `Persistence Services`：SessionStore、RunStore、EventStore、CheckpointStore、ArtifactStore。

核心执行模型：

- `Session = runtime container`。
- `Run = task / execution domain`。
- 一个 project root / git worktree 同时最多允许一个 active session。
- 一个 session 同时只允许一个 active runner。
- v1 不支持同一工作目录内多 session 并行；如需并行，使用 git worktree 创建独立 repo 副本后分别启动 session。
- REPL 默认启动长寿命 prompt run。
- one-shot 默认启动单次 prompt run。
- Phase 0.5 起，TTY REPL 默认启动轻量 TUI；one-shot、非 TTY 和注入 I/O 场景保持纯 stdout/stdin。
- prompt run 命中 workflow skill 时，创建 workflow run 压栈执行，结束后返回原 prompt run。
- v1 只支持单层 handoff：`prompt run -> workflow run -> prompt run`。
- v1 不支持 nested workflow 或任意深度 run 嵌套。

核心状态分类：

- `authoritative state`：恢复执行所需的结构化真值，例如 workflow state、checkpoint state、active case、retry、artifact index。
- `compressible observations`：只供 LLM 理解的历史，例如长 stdout、搜索结果、文件片段、解释性输出。
- `artifacts`：大文件、日志、图片、rdc、diff、报告，由 ArtifactStore 管理。

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

`workspace_root` 是 session 的工作目录所有权边界。Session 创建时必须检查该目录是否已有 active session；若存在，则拒绝启动并提示用户等待、恢复或在 git worktree 副本中启动新 session。

```python
class Run:
    run_id: str
    session_id: str
    parent_run_id: str | None
    run_type: str  # prompt | subagent | workflow
    status: str    # running | completed | failed | interrupted
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
    step_id: str | None
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

```python
class StepResult:
    step_id: str
    status: str  # success | failed | interrupted | timeout
    output: object
    error: dict | None
    started_at: str
    finished_at: str
    artifacts: list[str]
    metadata: dict
```

错误分类固定为：

- `user_error`
- `config_error`
- `policy_denied`
- `tool_error`
- `model_error`
- `workflow_error`
- `internal_error`
- `timeout`
- `cancelled`

## CLI And Modes

CLI 入口：

- `debug-agent`：进入 REPL，默认 `--normal`。
- `debug-agent -p "..."`：执行 one-shot，默认 `--yolo`。
- `debug-agent resume <session_id>`：恢复 interrupted run。
- `debug-agent status <session_id>`：查询 session/run 状态。
- `debug-agent trace <session_id>`：输出 trace。
- `debug-agent skills list`：列出可发现 skills。
- `debug-agent agents list`：列出可发现 agents。
- `debug-agent plugins list`：Phase 6 后启用。

REPL slash commands：

- `/status`
- `/skills`
- `/agents`
- `/models`
- `/resume`
- `/exit`
- `/compress`

Slash command 由 CLI/REPL 本地解析，不进入 LLM。

Phase 0.5 REPL UI 规则：

- TTY 交互默认使用轻量 TUI。
- one-shot 模式始终使用裸 stdout 输出，不启动 TUI。
- 非 TTY、测试注入 `input_stream` / `output_stream` 或 prompt_toolkit 初始化失败时，自动回退到 `PlainReplView`。
- TUI 只属于 `CLI Entrypoint / REPL UI` 层，不新增 runtime 真值语义。
- TUI 可展示输入历史、多行输入、模型 streaming delta、工具调用状态、turn 状态、token/mode 状态栏和退出摘要。
- TUI 不改变 Session/Run/Event/Checkpoint/Artifact、ToolBroker、Approval、Path Policy 或 AgentLoopAdapter ownership。

审批模式：

- `normal`：所有工具调用都要求审批，可在当前 session 内记住同类批准。
- `semi-auto`：只读工具自动放行，写文件、shell、git、网络、MCP mutation 需审批。
- `yolo`：自动执行，但仍必须经过 ToolBroker、path policy 和审计。

REPL 支持 `Ctrl+Y` 切换模式：

```text
normal -> semi-auto -> yolo -> normal
```

运行中输入规则：

- 当前存在 `running` run 时，普通用户 prompt 一律拒绝。
- `/status`、`/skills`、`/agents`、`/models` 允许执行。
- `/exit` 允许执行，并先中断当前 run。
- `/resume`、`/compress` 在 `running` 状态下拒绝执行。

## REPL TUI And Streaming

Phase 0.5 在 Phase 0 最小 REPL 之后、Phase 1 skill/tool 扩展之前实现。它是 `CLI Entrypoint / REPL UI` 层增强，不是新的 runtime 语义层。

Phase 0.5 负责：

- 输入体验：prompt history、`Ctrl+J` 多行、输入锁定。
- 输出排版：用户消息、模型输出、工具调用信息、slash command 结果。
- 模型流式展示：streaming delta。
- 工具调用信息展示：名称、状态、耗时、预览截断。
- turn 状态与计时。
- session 退出摘要。
- 基础 token/mode/model 状态展示。

Phase 0.5 不负责：

- 改变 session/run/checkpoint/event/artifact 的权威状态。
- 改变 ToolBroker、Approval、Path Policy 的安全语义。
- 改变 AgentLoopAdapter 的 ownership。
- 引入 skill、subagent、workflow、MCP 或 plugin。
- 引入复杂 GUI、跨平台终端框架过度设计、跨 session prompt history 持久化或完整主题系统。
- 实现 mid-call cancel propagation；该能力等 Phase 2 run control/cancellation path 后再接入。

架构原则：

- TUI 消费 runtime event 和 adapter stream observation，但 runtime 不依赖 TUI。
- `Runtime Event` 是 authoritative/audit event，写入 `run_events`。
- `AgentStreamEvent` 是 runtime-neutral stream observation event，不作为恢复真值，永不写入 `run_events`。
- `ReplViewEvent` / `ReplRenderState` 是纯渲染层对象，由 Controller 从 `AgentStreamEvent` 映射而来。
- Runtime 保持 headless。若实现 `ReplRuntime`，它只能是 UI-facing facade，不拥有 Session/Run/Event/Checkpoint/Artifact 真值，也不复制 orchestration 语义。
- `PromptAgentExecutor` 不感知 TUI 存在。`AgentLoopAdapter.run()` 继续供 one-shot、测试、workflow 复用；`AgentLoopAdapter.stream()` 是 TUI 额外路径。

MVC 分层：

- Model = 现有 `RuntimeOrchestrator` / REPL runtime services；可选 `ReplRuntime` 只能做 UI-facing adapter。
- View = `ReplView` Protocol；Phase 0.5 默认实现为 prompt_toolkit + rich。
- Controller = `ReplController`；接收输入，调用 Model，把 stream observation 映射成 view state/event，显式更新 View。

Controller 职责：

- 维护 turn timer，每秒刷新 View。
- 处理用户输入、slash command 和 interrupt。
- 在后台线程执行 runtime turn，通过 queue 接收 `AgentStreamEvent`。
- 把 `AgentStreamEvent` 转换为 `ReplViewEvent` 或 `ReplRenderState` 后更新 View。
- 映射 `stream_tool_result` 时，若 `output` 为 dict，先用 `json.dumps(..., ensure_ascii=False, sort_keys=True)` 转换为字符串。
- Runtime 完成后更新 View 的最终状态。

`AgentStreamEvent` 形状：

```python
class AgentStreamEvent:
    kind: Literal[
        "stream_model_call_started",
        "stream_text_delta",
        "stream_model_call_completed",
        "stream_tool_call_started",
        "stream_tool_call_completed",
        "stream_tool_result",
    ]
    payload: dict
```

Payload 约定：

- `stream_model_call_started`: `{"model_call_id": str}`。
- `stream_text_delta`: `{"model_call_id": str, "text": str}`。
- `stream_model_call_completed`: `{"model_call_id": str, "is_final": bool, "usage": dict, "duration_ms": int}`。
- `stream_tool_call_started`: `{"tool_call_id": str, "model_call_id": str, "name": str, "args": dict}`。
- `stream_tool_call_completed`: `{"tool_call_id": str, "model_call_id": str, "name": str, "status": str, "duration_ms": int}`。
- `stream_tool_result`: `{"tool_call_id": str, "model_call_id": str, "output": str | dict | None, "redacted_output": str | None, "artifact_ids": list[str]}`。

`stream_tool_call_started.args` 是 runtime observation payload，不是默认 UI 输出。TUI 默认只展示 tool name、status、duration；如需展示参数，只展示 Controller 生成的 redacted/short preview。`stream_tool_result` 数据来自本次 adapter/tool invocation 返回的 `ToolResult`，不是从 persisted `run_events` 反查。

`model_call_id` 和 `tool_call_id` 是单 turn 内 correlation id。`tool_call_id` 优先使用 provider 返回的 tool call id；缺失时由 adapter/runtime 在单 turn 内生成。adapter/runtime 在生成 persisted runtime event 时，可以复制这些 correlation id 到 payload 用于 trace 关联，但这些 id 不作为恢复真值，也不承诺跨 session 稳定。

`ReplViewEvent` 形状：

```python
class ReplViewEvent:
    kind: Literal[
        "model_text_delta",
        "model_markdown_final",
        "tool_block",
        "system_message",
        "error_message",
    ]
    payload: dict
```

View 应按 `model_call_id` 维护可替换的 message block。`model_text_delta` 追加到对应 block；`model_markdown_final` 替换同一 block 的 plain text 渲染结果。View 不直接消费 `AgentStreamEvent`。

`ReplView` Protocol：

```python
class ReplView(Protocol):
    def run(self, controller: ReplController) -> int: ...
    def show_welcome(self, snapshot: WelcomeSnapshot) -> None: ...
    def set_input_enabled(self, enabled: bool) -> None: ...
    def append_user_message(self, message: str) -> None: ...
    def append_view_event(self, event: ReplViewEvent) -> None: ...
    def set_turn_status(self, turn_id: int, status: str, elapsed_seconds: int) -> None: ...
    def update_status_bar(self, snapshot: StatusBarSnapshot) -> None: ...
    def show_session_closed(self, summary: SessionCloseSummary) -> None: ...
    def show_error(self, message: str) -> None: ...
```

`ReplController` callback：

```python
class ReplController:
    def on_submit(self, text: str) -> None: ...
    def on_slash_command(self, cmd: str) -> None: ...
    def on_interrupt(self) -> None: ...
    def on_agent_stream_event(self, event: AgentStreamEvent) -> None: ...
    def notify_event_ready(self) -> None: ...
    def on_turn_finished(self, result: AgentRunResult) -> None: ...
```

`PromptAgentExecutor.run_turn()` 在 Phase 0.5 增加可选 callback：

```python
def run_turn(
    self,
    *,
    session: Session,
    run: Run,
    user_input: str,
    workspace_root: str,
    conversation: list[dict[str, Any]] | None = None,
    prompt_turn_counter: int = 1,
    agent_stream_callback: Callable[[AgentStreamEvent], None] | None = None,
) -> AgentRunResult: ...
```

线程模型：

- 主线程运行 prompt_toolkit `Application.run()`，负责键盘、渲染、定时器回调和 queue drain。
- Runtime 后台线程执行 `executor.run_turn()`，仅通过 `queue.put(AgentStreamEvent)` 推送事件。
- Runtime 线程绝不直接调用 View 方法或 prompt_toolkit 对象。
- Runtime 线程 `queue.put(event)` 后调用 Controller 注入的 `notify_event_ready()`；该回调只触发 thread-safe wakeup，例如封装后的 `app.invalidate()`，不读取 queue，不修改 View state。
- Controller 在 UI event loop 内 drain queue，按 `kind` 映射为 View state / View Event 后触发重绘。

输入与历史：

- 输入框采用 shell 风格，以 `>` 开头，并与输出区样式区分。
- 支持单行输入、`Ctrl+J` 换行、`Enter` 提交、上下键切换历史。
- `Shift+Enter` 换行是 terminal-dependent best-effort。
- 提交后用户输入保留在消息列表中，并通过 `set_input_enabled(False)` 锁定不可编辑。
- 历史只记录成功提交的用户 prompt；空 prompt 不提交、不进历史。
- slash command 进入历史；多行 prompt 作为一个历史项保存。
- prompt history 仅当前 session 内存态，跨 session 持久化留到后续 phase。
- 中文 IME 和中文退格属于独立可选能力；Phase 0.5 时间盒内无法解决时可标记为 known limitation。

消息渲染：

- 展示用户 prompt、模型输出、工具调用开始/完成、slash command 结果、error/interrupt/completed 状态。
- 每个有文本内容的 model call 在 streaming 期间以 plain text 增量显示；该 model call 结束后，一次性切换为 rich Markdown 渲染。
- table 不作为 Phase 0.5 验收能力；若 Markdown 渲染异常，fallback 到 plain text。
- 若单个 model call accumulated text 超过 `max_markdown_render_chars = 50_000`，完成后保留 plain text，避免 Markdown 解析阻塞 UI。该阈值只影响渲染策略，不截断输出。
- 如果某次 model call 没有任何 `stream_text_delta`，不渲染空模型输出块，只渲染 tool call / tool result view block。
- 工具调用和工具结果作为独立 view block 渲染，不与 model output 混在同一个 Markdown 文本中。
- 全局 scrollback 和长 assistant 输出依赖用户 terminal 的 scrollback 设置，不在 Phase 0.5 自建 hard limit。

工具结果预览：

```text
tool: read_file
status: ok
duration: 1.2s
```

```text
> line 1
> line 2
> ...
> [truncated: showing 10 of 325 lines, full output saved as artifact art_xxx]
```

默认阈值：

```text
max_tool_result_preview_lines = 10
max_tool_result_preview_chars = 1000
```

超过限制时，Controller 或独立 `ToolResultPreviewFormatter` 负责生成预览；runtime/adapter 不持有 UI 截断阈值。预览截断仅影响显示，不触发新的 artifact 生成。完整内容仍按 `ArtifactStore` / `run_events` 的既有规则持久化。

Turn 状态与状态栏：

- 每个用户 prompt 都有对应 turn 状态和秒级耗时。
- UI 可展示 `running` / `completed` / `failed` / `cancelled` / `timeout`。
- 持久层 `run/session status` 只有 `running` / `completed` / `failed`；`cancelled` 映射为 `failed + error_class=cancelled`，`timeout` 映射为 `failed + error_class=timeout`。
- active run 期间普通 prompt 仍按现有规则拒绝，TUI 在消息列表中追加 error/system 样式消息说明原因。
- active run 期间 `/status` 作为 system message 追加到消息列表，不做 modal，不替换状态栏。
- 状态栏至少展示 token usage、当前 approval mode 和当前 model。
- token usage 是 best effort；provider 返回则展示，否则显示 `unavailable` 或保留已知累计值。
- 不做 `context remaining percentage`，待 Phase 1 `ContextManager` 实现后再接入。

欢迎界面与退出摘要：

- REPL 启动时显示欢迎卡片，字段包括 `debug-agent`、版本号、当前模型、工作目录、approval mode、session id 短 id。
- 版本号来自 `importlib.metadata.version("debug-agent")`；editable/dev 环境无法读取时 fallback 为 `unknown`，不得阻塞 REPL 启动。
- 正常退出显示 `session <session_id> closed.` 和 token summary；usage 不可用时显示 `tokens used: unavailable`。
- `Ctrl+C` / 取消显示 `cancelled`；未捕获异常显示 `failed`；timeout 显示 `failed` + `error: timeout`，并提示 `debug-agent trace <session_id>`。

技术选型：

- prompt_toolkit 用于 REPL 输入、history、key binding 和事件循环。
- rich 用于 Markdown 渲染、panel、颜色和 status 展示。
- 新增运行时依赖：`prompt_toolkit >= 3.0.0`、`rich >= 13.0.0`。
- 迁移到 Textual 时只重写 UI 层；Controller 和 Runtime 不应需要改动。

## Agent Runtime

Agent loop 通过 adapter 接入。

```python
class AgentLoopAdapter:
    def run(self, request: AgentRunRequest, context: RunContext) -> AgentRunResult: ...
    def stream(
        self,
        request: AgentRunRequest,
        context: RunContext,
        on_event: Callable[[AgentStreamEvent], None],
    ) -> AgentRunResult: ...
    def cancel(self, run_id: str) -> None: ...
```

`run()` 是 authoritative result path，继续供 one-shot、plain REPL、测试和 workflow 复用。Phase 0.5 起，`stream()` 是 TUI 使用的 observation path；它额外推送 `AgentStreamEvent`，但最终持久化和 checkpoint 仍以完整 `AgentRunResult` 与 runtime event log 为准。

Streaming consistency contract：

- `stream()` 的 `on_event` 回调为每次 model call 推送 `AgentStreamEvent`。
- 每次 model call 都有独立 lifecycle event。
- 有文本内容的 model call 推送 `stream_text_delta` 并可在 TUI 上渲染。
- 工具调用前置的 model response 如果包含 provider 明确返回的 user-visible content，也可产生 `stream_text_delta`；这些 delta 不参与 `AgentRunResult.assistant_output` consistency 约束。
- `tool_call_chunks`、function-call-only chunk、partial tool args 和内部 planning 数据不渲染为模型文本。
- 如果某次中间 model call 没有任何可展示文本，TUI 不渲染空模型输出块，只渲染后续 tool events。
- Authoritative assistant message 是 `PromptAgentExecutor` 最终持久化的 `AgentRunResult`。
- Adapter 必须保证最终 assistant model call 的所有 `stream_text_delta` 拼接结果等于 `AgentRunResult.assistant_output`。中间 model call 的 delta 不做此约束。
- `LangChainAgentLoopAdapter.stream()` 主路径使用 LangChain 原生 `model.stream()`；若当前 model/provider 不支持 `stream()`，fallback 为非流式 `invoke()`，不模拟 streaming。

Phase 0/0.5/1 默认 adapter：

- `LangChainAgentLoopAdapter`
- 使用 LangChain `create_agent`
- 使用 middleware 支持 dynamic prompt injection
- 不让 LangChain 拥有 session、run、checkpoint、approval、artifact、workflow 真值

Provider 策略：

- Phase 0/0.5/1 使用 LangChain 兼容 chat model。
- `ModelFactory` 只负责读取配置并实例化模型。
- 不在早期实现完整 provider abstraction。
- 后续 OpenAI-compatible provider 通过 `ModelFactory` 或新 adapter 扩展。

Prompt 组合顺序：

- runtime 固定安全前缀
- agent system prompt
- active skill 正文
- 当前任务输入

`agent.toml` 最小字段：

```toml
name = "shader-debugger"
provider = "anthropic"
model = "..."
system_prompt = "..."
skills = []
allowed_tools = []
disallowed_tools = []
mcp_servers = []
max_turns = 20
max_tokens = 8192
timeout_seconds = 600
temperature = 0.2
output_mode = "text"
output_parser = ""
output_schema = ""
```

字段继承规则：

- 可继承：`provider`、`model`、`skills`、`allowed_tools`、`disallowed_tools`、`mcp_servers`、`max_tokens`、`timeout_seconds`、`temperature`。
- 不继承：`name`、`system_prompt`、`output_mode`、`output_parser`、`output_schema`、`max_turns`。
- 优先级：`agent.toml` 显式值 > 主 agent 当前配置 > 系统默认值。

Subagent 规则：

- `SubagentExecutor` 创建子 run。
- 子代理共享 session approval mode。
- 子代理工具调用仍经过 ToolBroker。
- 子代理可使用不同模型。
- 子代理默认输出 text。
- 需要结构化输出时由 `agent.toml` 显式声明 parser/schema。
- Phase 2 不实现 `SubagentPipelineExecutor`，多阶段编排由 workflow 或上层 executor 显式串联。
- `mcp_servers` 是后续 MCP 扩展字段，Phase 0-4 可以解析但不启用。

## Skill System

发现路径：

- `~/.debug-agent/skills`
- `~/.debug-agent/agents`
- `<project_root>/.debug-agent/skills`
- `<project_root>/.debug-agent/agents`
- CLI 显式 `--skill-path`
- CLI 显式 `--agent-path`

覆盖规则：

- 显式路径优先。
- 项目级优先于全局级。
- 同名 skill/agent 整体覆盖。
- 不做文件级或目录级 merge。
- registry 返回对象必须带 `source_scope`：`explicit | project | global | builtin`。

`SKILL.md` 规则：

- 必须以 YAML front matter 开头。
- `name` 和 `description` 必填。
- front matter 解析失败则拒绝加载并记录 error。
- registry 启动时只解析 header。
- 激活 skill 时再读取正文。
- `SKILL.md` 正文追加到 agent system prompt 后，不覆盖系统 prompt。
- v1 不支持 skill/agent/config 热更新；session 创建时冻结 registry/config snapshot，修改 `SKILL.md`、`agent.toml`、`mcp.toml` 或 model config 后必须启动新 session 才会生效。

最小 manifest：

```yaml
name: shader-debug-loop
description: Automated iterative shader debugging loop
execution_mode: workflow
triggers:
  - shader bug
  - graphics test failure
depends_on:
  skills:
    - renderdoc-gpu-debug
  agents:
    - renderdoc-debugger
metadata:
  platform:
    - windows
  tags:
    - shader
    - debug
```

Skill 路由：

```text
CLI Request
-> Runtime Orchestrator
-> SkillResolver
-> SkillManifest
-> PromptAgentExecutor or WorkflowSkillExecutor
```

路由优先级：

- 用户显式指定 workflow skill。
- 触发词命中 workflow skill。
- 默认主 agent / PromptAgentExecutor。

Prompt skill 激活：

- 使用内置 `activate_skill(name)` tool。
- 只能激活当前 agent 白名单内的 prompt skills。
- 重复激活幂等。
- skill 正文在下一次 model call 生效。
- 不通过重启 agent loop 实现注入。
- `active_skills` 是 run-scope，run 结束后清空。

Workflow skill：

- 由 orchestrator 在 run 开始前直接路由。
- 不通过 `activate_skill` 激活。
- Phase 3 起支持。

## MCP Integration

MCP 是后置扩展能力，不是 `shader-debug-loop` 运行主路径的依赖。Phase 0-4 不要求实现 MCP server lifecycle、MCP tool discovery 或 MCP tool invocation。

配置文件：

- `~/.debug-agent/mcp.toml`
- `<project_root>/.debug-agent/mcp.toml`

合并规则：

- 先加载全局 `mcp.toml`。
- 再加载项目级 `mcp.toml`。
- 不同名 server 追加。
- 同名 server 项目级覆盖全局级。
- 子代理 `agent.toml.mcp_servers` 可选择使用哪些 server。

最小配置：

```toml
[servers.sequential-thinking]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-sequential-thinking"]

[servers.example-http]
transport = "http"
url = "http://localhost:8080/mcp"
```

Phase 5 起支持：

- `stdio`
- `http`
- `streamable_http` 作为可选 transport，不阻塞 Phase 5 最小验收。

硬规则：

```text
Raw MCP Tool
-> ToolBroker wrapper
-> approval / path / risk enforcement
-> runtime visible tool
```

MCP tool 不允许原样暴露给 agent。

## Global Config

全局配置文件：

- `~/.debug-agent/config.toml`

项目目录下不使用 `config.toml`。

`config.toml` 只负责：

- 默认 provider
- 默认 model
- api key env 名称
- 默认 timeout
- 默认 token 参数
- REPL/one-shot 默认行为

最小示例：

```toml
[defaults]
provider = "anthropic"
model = "claude-sonnet-4"
temperature = 0.2
max_tokens = 8192
timeout_seconds = 120

[auth.anthropic]
api_key_env = "ANTHROPIC_API_KEY"
```

## Tooling And Safety

`ToolBroker` 是唯一工具出口。

职责：

- 校验 `allowed_tools`。
- 校验 `disallowed_tools`。
- 执行 approval mode。
- 执行 path policy。
- 包装 native tool。
- 包装 shell/git tool。
- Phase 5 起包装 MCP tool。
- 记录 tool audit event。
- 处理 timeout/cancel/error。
- 输出统一 `ToolResult`。

Path policy v1 支持：

- `read`
- `write`
- `execute`

示例：

```toml
[[path_policies]]
scope = "write"
paths = [
  "Packages/com.unity.render-pipelines.universal/",
  "Packages/com.unity.render-pipelines.core/",
  "Shaders/"
]
```

审批缓存 key：

- `tool_name`
- `risk_level`
- `scope_signature`

安全原则：

- 安全边界由 runtime 强制执行。
- 不依赖 prompt 自觉。
- yolo 模式也不能绕过 ToolBroker。
- subagent 不能绕过 ToolBroker。
- Phase 5 起，MCP 不能绕过 ToolBroker。
- git/shell/diff 操作必须作为高风险工具审计。

## Schema Validation

`SchemaValidator` 是 runtime contract 的边界校验器，不负责判断业务推理是否正确。

校验对象：

- agent/subagent 结构化输出。
- `ToolResult` 协议形状。
- `StepResult` 协议形状。
- workflow state/checkpoint 结构。
- artifact metadata。
- skill/agent/config manifest。

错误归类：

- 用户提供的 skill/agent/config schema 错误归类为 `config_error`。
- agent/subagent 结构化输出 schema 错误归类为 `model_error`；若发生在 workflow step 中，则 workflow 将其包装为 `workflow_error` 并进入 error-handling。
- tool wrapper 返回非法 `ToolResult` 归类为 `internal_error`。
- workflow state/checkpoint schema 错误归类为 `workflow_error`。

处理规则：

- schema 校验失败必须写入 `run_events`。
- workflow step 的 schema 校验失败不重试，直接进入 workflow error-handling。
- `SchemaValidator` 不做 shader 业务语义校验，也不判断修复是否正确。

## Persistence And Observability

v1 使用 SQLite + 本地文件系统。

目录：

```text
.sessions/
  runtime.db
  <session_id>/
    artifacts/
    logs/
    temp/
    trace.md
```

SQLite 最小表：

- `sessions`
- `runs`
- `run_events`
- `checkpoints`
- `artifacts`
- `approval_grants`

`SessionStore` 最小字段：

- `session_id`
- `status`
- `active_run_id`
- `run_stack`
- `artifact_root`
- `metadata_root`
- `config_snapshot`
- `latest_checkpoint_id`
- `approval_mode`
- `created_at`
- `updated_at`
- `error_summary`
- `version`

`RunState` 最小字段：

- `run_id`
- `parent_run_id`
- `run_type`
- `status`
- `active_skills`
- `latest_checkpoint_id`
- `run_local_summary`
- `run_context`
- `event_stream`

`ArtifactStore` 最小接口：

```python
class ArtifactStore:
    def create_session_root(self, session_id: str) -> str: ...
    def write_text(self, session_id: str, relative_path: str, content: str) -> str: ...
    def write_bytes(self, session_id: str, relative_path: str, content: bytes) -> str: ...
    def register_existing(self, session_id: str, path: str, metadata: dict) -> str: ...
    def resolve_path(self, artifact_id: str) -> str: ...
    def cleanup_temp(self, session_id: str) -> None: ...
```

Checkpoint 原则：

- checkpoint 保存 authoritative state。
- run_events 保存审计事实。
- artifacts 保存大输出。
- trace.md 由 run_events 和 artifacts 派生。
- 不用裸路径作为长期真值，必须通过 artifact id 或 registry 记录。

Observability：

- `engine.log` 记录 step start/end、session state、checkpoint、tool allow/deny、approval mode switch。
- workflow artifacts 记录外部 stdout/stderr、子代理原始输出、schema 校验失败详情。
- 日志最小字段：`timestamp`、`session_id`、`run_id`、`step_id`、`level`、`event`、`message`。

## Context And Compression

`ContextManager` 负责 LLM 可见上下文。

规则：

- `/compress` 只压缩 LLM 可见历史。
- `/compress` 不做 state compaction。
- `/compress` 不删除真实 checkpoint。
- `/compress` 不修改 workflow state。
- `/compress` 不修改 authoritative state。
- `/compress` 仅在 idle 状态执行。
- 压缩结果写入新的 conversation summary checkpoint。

上下文预算策略：

- skill 正文按需加载。
- 大 stdout 默认 artifact 化。
- 长 tool result 进入 artifact，模型只看到摘要和 artifact 引用。
- subagent 长输出进入 artifact，返回结构化摘要。
- workflow state 不靠自然语言 summary 恢复。

## Session Control

目标：

- `Ctrl+C` 中断当前活跃 run。
- REPL 在 run 活跃期间仍可接收 slash command。
- SessionStore 记录 run 状态迁移。
- `/resume` 从 checkpoint 恢复 interrupted run。
- 同一 `workspace_root` 同时只允许一个 active session。

状态机：

```text
running -> completed
running -> failed
running -> interrupted
interrupted -> running
```

`Ctrl+C` 语义：

- 第一次 `Ctrl+C` 请求优雅中断。
- cancellation token 传播到 LLM、tool、subagent、workflow step。
- 在最近 checkpoint 或安全边界标记 `interrupted`。
- 第二次 `Ctrl+C` 允许强制终止前台等待。

恢复边界：

- agent turn boundary 强保证。
- tool boundary 强保证。
- workflow step boundary 强保证。
- shell/subagent mid-flight best-effort。
- 不支持 token-level resume。
- 不支持 tool-mid-flight resume。
- 不支持 subagent-mid-thought resume。

工作目录所有权：

- session 创建时获得 `workspace_root` 的 active session ownership。
- active session ownership 记录在 SessionStore，并可由本地 anchor/lock 文件辅助快速检测。
- 若同一 `workspace_root` 已存在 active session，新 session 必须拒绝启动。
- v1 不实现同一工作目录多 session 并行。
- 并行调试应通过 git worktree 创建独立 repo 副本，每个 worktree 各自运行一个 session。

## Workflow Runtime

Workflow 是一级 executor，不退化为 prompt agent loop。

第一版 code-first：

```python
class WorkflowDefinition:
    name: str
    initial_state: dict
    steps: list[WorkflowStep]
```

```python
class WorkflowStep:
    step_id: str
    step_type: str  # python | shell | subagent | interrupt
    timeout_seconds: int | None
    handler: object
```

```python
class WorkflowContext:
    session_id: str
    run_id: str
    state: dict
    data: dict
    artifacts: list[str]
    cancellation_token: object
```

Step 类型：

- `python`
- `shell`
- `subagent`
- `interrupt`

Transition：

- v1 由 Python handler 明确返回 next step。
- 不在 v1 引入通用 expression DSL。
- 不在 v1 引入 `ConditionEvaluator`。
- 不在 v1 引入 YAML-first workflow DSL。

WorkflowEngine 职责：

- 加载 workflow definition。
- 初始化 context。
- 执行 step。
- 写 run event。
- 写 checkpoint。
- 运行 step post-processing。
- 决定下一步。
- 处理中断、超时、失败、恢复。

失败与重试语义：

- v1 不提供通用 step-level retry。
- shell、python、subagent、interrupt step 一旦返回 `failed`、`timeout`、`denied` 或 schema 校验失败，workflow 直接进入 error-handling。
- 业务级循环只能由 workflow handler 显式表达，例如 `shader-debug-loop` 的全局 retry 计数。
- 业务级 retry 不等于 step executor 自动重试。

不支持：

- nested workflow。
- dynamic workflow generation。
- runtime step injection。
- parallel execution。
- event-driven transition。
- arbitrary Python expression in config。
- generic step-level retry policy。

YAML 位置：

- Phase 3 不要求 YAML DSL。
- 后续可引入轻量 `workflow.yaml` 作为入口声明。
- YAML 不承载复杂控制逻辑。

## Plugin Strategy

Plugin 不进入 v1 主线。

Phase 6 可选实现静态 plugin/package loader。

Plugin 定义：

- 纯组织/分发单元。
- 包含 skills、agents、mcp config。
- 不引入新的 runtime 语义。
- 不允许 Python hook。
- 不允许 runtime patch。
- 不允许 dynamic loader。
- 不允许 event subscription。

可选目录：

```text
plugins/
  shader-debug/
    plugin.toml
    skills/
    agents/
    mcp.toml
```

## Phase Roadmap

### Phase 0: Minimal Runtime Slice

目标：跑通最小 CLI agent，并产生可恢复 session 记录。

实现内容：

- CLI：`debug-agent`
- CLI：`debug-agent -p "..."`
- CLI：`debug-agent status <session_id>`
- CLI：`debug-agent trace <session_id>`
- REPL slash：`/status`
- REPL slash：`/exit`
- `SessionStore`
- `RunStore`
- `EventWriter`
- `CheckpointStore`
- `ArtifactStore`
- `ModelFactory`
- `LangChainAgentLoopAdapter`
- `PromptAgentExecutor`
- 最小 `ToolBroker`
- 基础日志

验收标准：

- one-shot 能完成一次模型问答。
- REPL 能连续对话。
- session/run/event/checkpoint 能写入 `.sessions/runtime.db`。
- native read-only tool 必须经过 ToolBroker。
- `/status` 能显示 session、active run、mode、latest checkpoint。

不做：

- skill
- subagent
- MCP
- workflow
- plugin
- `/compress`

### Phase 0.5: Lightweight TUI And Streaming REPL

目标：在不改变 Runtime Core、Session/Run 模型、ToolBroker、安全边界和持久化语义的前提下，为 `debug-agent` 提供轻量、稳定、可观测的命令行 TUI，使后续 Phase 1+ 的 skill、tool、subagent、workflow 能被人类直观评估。

Phase 0.5 是 Phase 0 最小 REPL 的 UX 补强，不是 agent 能力层面的新语义。

Milestone A：非流式 TUI shell。

实现内容：

- `ReplView` Protocol。
- `ReplController`。
- `PromptToolkitReplView`。
- `PlainReplView` fallback。
- welcome panel。
- shell 风格输入框。
- 当前 session 内 prompt history。
- `Ctrl+J` 多行输入。
- 用户消息固定显示与输入锁定。
- slash command 结果追加到消息列表。
- 工具调用和工具结果 view block。
- `ToolResultPreviewFormatter`。
- turn running/completed/failed/cancelled/timeout 展示。
- 底部 token/mode/model 状态栏。
- session close summary。
- TTY / 非 TTY / 注入 I/O / prompt_toolkit 初始化失败 fallback。

Milestone A 验收标准：

- REPL 启动显示欢迎界面。
- 用户输入区与模型输出区不会混杂。
- 支持 prompt history 上下键。
- 支持 `Ctrl+J` 多行输入；`Shift+Enter` 为 best-effort。
- 提交后的用户 prompt 固定显示，输入框锁定。
- 工具调用和工具结果有独立样式。
- 长工具结果被截断预览，不撑爆 UI。
- 每个 turn 有状态和秒级耗时。
- 底部状态栏展示 token/mode/model 的最小信息。
- `/exit` 后显示 `session <session_id> closed.` 和最终 token 用量。
- 非 TTY 或注入 I/O 环境自动回退到 `PlainReplView`。
- prompt_toolkit 初始化失败时自动降级到 `PlainReplView`，warning 最多输出一次。
- one-shot 模式不启动 TUI，保持裸 stdout。

Milestone B：streaming adapter。

实现内容：

- `AgentLoopAdapter.stream()`。
- `LangChainAgentLoopAdapter` 的 `model.stream()` 路径。
- 不支持 streaming 的 model/provider 走现有 `invoke()` fallback，不做 simulated streaming。
- `PromptAgentExecutor.run_turn(..., agent_stream_callback=...)`。
- `AgentStreamEvent` 到 queue 到 `ReplViewEvent` / `ReplRenderState` 的链路。
- streaming text delta 增量渲染。
- model call 完成后的 Markdown final render。
- tool call started/completed/result stream observation。

Milestone B 验收标准：

- mock streaming model delta 能逐步渲染。
- mock tool call start/result 能显示工具块。
- provider/model 不支持 `stream()` 时 fallback 到 `invoke()`，并提示一次 `streaming unavailable for this model; using non-streaming response.`。
- function-call-only chunk、partial tool args 和内部 planning 数据不渲染为模型文本。
- 无文本 model call 不创建 model output block，只展示 tool events。
- 有文本的中间 model call 会渲染 model output block，但不参与 `AgentRunResult.assistant_output` consistency contract。
- 最终 assistant model call 的 delta 拼接结果等于 `AgentRunResult.assistant_output`。
- `AgentStreamEvent` 不写入 persisted `run_events`。

完整 Phase 0.5 验收标准：

- Milestone A 和 Milestone B 均通过。
- TUI 不改变 Session/Run/Event/Checkpoint/Artifact runtime contract。
- ToolBroker、Approval、Path Policy 安全边界不被 TUI 绕过或弱化。
- Runtime 线程不直接调用 View 或 prompt_toolkit 对象。
- View 不直接消费 `AgentStreamEvent`。
- `display_status=cancelled` 和 `display_status=timeout` 到持久层 `failed + error_class` 的映射在实现中显式存在。
- active execution 期间 `/exit` 沿用 runtime safe-boundary 行为，不引入新的 mid-call cancellation 语义。

不做：

- skill、subagent、workflow、MCP、plugin。
- approval UI 弹窗。
- trace viewer、diff viewer、workflow viewer。
- session list/browser。
- 跨 session 输入历史持久化。
- 完整主题系统。
- 完整 provider/token abstraction。
- 中文 IME 完整支持。
- mid-call cancel propagation。
- 块级增量 Markdown 渲染。
- mouse 交互、多 pane layout、消息折叠/展开。

正式文档拆分 TODO：

- 新建 `docs/phase-0.5/`。
- 拆分为 `scope.md`、`architecture.md`、`tests.md`、`operations.md`。
- 新增 `specs/repl-tui.md`：View、Controller、fallback、输入、状态栏、退出摘要。
- 新增 `specs/agent-streaming.md`：`AgentLoopAdapter.stream()`、`AgentStreamEvent`、streaming consistency contract、id 规则。
- 新增或更新 `implementation-plan.md`，明确 Milestone A/B 验收顺序。
- 补 ADR：`ADR 0007: AgentLoopAdapter Streaming Observation Path`，记录 `stream()` 作为长期 adapter public contract、`run()` 作为 authoritative result path、`AgentStreamEvent` 不写入 `run_events`、provider 不支持 streaming 时的 `invoke()` fallback。
- `operations.md` 明确 Phase 0.5 标准验证命令。

### Phase 1: Skills And Native Tools

目标：支持 prompt skill 和受控工具调用。

实现内容：

- `SkillRegistry`
- `AgentRegistry`
- `SKILL.md` manifest loader
- `agent.toml` loader
- `activate_skill`
- `SkillPromptMiddleware`
- `ContextManager`
- path policy
- approval grants
- shell/native tool wrappers
- `/skills`
- `/agents`
- `/models`
- `/compress`

验收标准：

- prompt skill 可按需激活。
- skill 正文在下一次 model call 生效。
- tool result 统一写入 run_events。
- 大 stdout 能进入 artifact。
- `/compress` 只压缩 LLM 可见历史，不修改 authoritative state。

不做：

- subagent
- MCP
- workflow
- plugin

### Phase 2: Subagents And Session Control

目标：支持子代理、run control、interrupt/resume，并保持统一安全边界。

实现内容：

- `SubagentExecutor`
- 子 run 生命周期
- 子代理输出 parser
- cancellation token
- timeout handling
- `RunController`
- `Ctrl+C` interrupt
- `/resume`

验收标准：

- 主 agent 能调用单个 subagent。
- subagent 共享 session approval mode。
- subagent 工具调用仍经过 ToolBroker。
- `Ctrl+C` 可在 tool/subagent boundary 中断。
- interrupted run 可从最近 checkpoint resume。

不做：

- workflow
- MCP
- plugin
- 多 subagent pipeline executor
- parallel subagents

### Phase 3: Workflow Core

目标：支持 code-first workflow，并能表达长流程 debug loop。

实现内容：

- `WorkflowDefinition`
- `WorkflowEngine`
- `WorkflowContext`
- `StepResult`
- `PythonStepExecutor`
- `ShellStepExecutor`
- `SubagentStepExecutor`
- `InterruptStepExecutor`
- workflow checkpoint/resume
- workflow trace writer
- `WorkflowSkillExecutor`

验收标准：

- workflow run 可启动、暂停、恢复、失败、完成。
- 每个 step 完成后写 checkpoint。
- shell step 支持 timeout、stdout artifact、exit code。
- subagent step 能串联两个子代理。
- workflow state 与 prompt context 分离。
- workflow 不依赖 prompt agent 自由循环推进。

不做：

- YAML DSL
- nested workflow
- parallel workflow
- dynamic workflow generation

### Phase 4: Shader-Debug Readiness

目标：验证 runtime 足以承载 `shader-debug-loop`，但不把 runtime 写死成 shader 专用系统。

实现内容：

- `shader-debug-loop` code-first workflow adapter。
- build/test shell step wrapper。
- artifact collection helper。
- diff/path validation helper。
- final trace/report generator。
- Windows e2e runner 文档。
- fake command runner fixtures。

验收标准：

- fake runner 下完整跑通 apply/build/test/collect/debug/apply-fix/report 控制流。
- Windows Tuanjie 环境可运行真实 workflow。
- shader workflow 使用通用 WorkflowEngine、ToolBroker、SubagentExecutor。
- shader workflow 不需要修改 runtime core。

不做：

- 完整 plugin 平台
- 通用 workflow DSL
- Postgres
- 云 artifact store

### Phase 5: Optional MCP Integration

目标：核心 debug workflow 稳定后，支持 MCP tool re-binding 作为可选外部工具扩展。

实现内容：

- `MCPServerManager`
- `mcp.toml` loader
- MCP tool wrapper
- MCP tool audit events
- `stdio` transport
- basic `http` transport
- optional `streamable_http` transport

验收标准：

- MCP tool 不原样暴露给 agent。
- MCP tool 必须经过 ToolBroker。
- MCP server 启动失败能归类为 `tool_error` 或 `config_error`。
- 没有 MCP 配置时不影响 Phase 0-4 能力。

不做：

- MCP marketplace
- MCP dynamic trust policy
- MCP tool bypass

### Phase 6: Optional Packaging

目标：核心能力稳定后支持静态分发。

实现内容：

- `PluginRegistry`
- plugin manifest
- plugin 内 skill/agent/MCP config discovery
- plugin source_scope
- `debug-agent plugins list`

验收标准：

- 一个 plugin 能携带多个 skills 和 agents。
- plugin 不引入 runtime hook。
- plugin 不能绕过 ToolBroker。

## Test Plan

Unit tests：

- `SessionStore` 创建、更新、乐观锁。
- `SessionStore` 阻止同一 workspace root 的并行 active session。
- `RunStore` 状态流转。
- `EventWriter` append-only 语义。
- `CheckpointStore` 保存与恢复。
- `ArtifactStore` 注册、解析、清理 temp。
- `ToolBroker` allow/deny、approval、path policy、timeout。
- `ToolResult` 标准化。
- `SchemaValidator` 正确归类 config/model/internal/workflow schema 错误。
- `SkillManifestLoader` header 解析失败处理。
- `AgentConfigLoader` 字段继承。
- `ContextManager` summary 不修改 authoritative state。
- `SubagentExecutor` 创建子 run 并继承 approval mode。
- `MCPServerManager` wrapper 注入（Phase 5）。
- `WorkflowEngine` step transition、checkpoint、resume。
- `ShellStepExecutor` stdout artifact 化。
- `InterruptExecutor` checkpoint 后中断。
- `PromptHistory` 上下切换。
- 多行输入提交规则。
- `ToolResultPreviewFormatter` 行数/字符数截断。
- Markdown render fallback；table 不作为验收能力。
- `AgentStreamEvent` 到 view state / view event 的转换。
- `ReplView` 不直接消费 `AgentStreamEvent`。
- `AgentStreamEvent.kind` 使用 `stream_*` 前缀，且不会写入 persisted `run_events`。
- 无文本 model call 不创建 model output block，只展示 tool events。
- 有文本的中间 model call 会渲染 model output block，但不参与 `AgentRunResult.assistant_output` consistency contract。
- function-call-only chunk / partial tool args 不渲染为模型文本。
- tool args 默认只生成 redacted/short preview。
- 重复同名 tool call 通过 `tool_call_id` 正确关联 started/completed/result。
- status bar snapshot formatting。
- token usage unavailable fallback。
- session cumulative token usage aggregation。
- session close summary formatting。
- `agent_stream_callback` 到 queue 到 view 的链路。
- `AgentLoopAdapter.stream()` 一致性 contract。
- provider/model 不支持 `stream()` 时 fallback 到 `invoke()`，并只提示一次 non-streaming system message。
- `/status` 在 TUI 中追加 system message。
- prompt_toolkit 初始化失败降级 warning 最多输出一次。
- welcome version lookup failure fallback to `unknown`。

Integration tests：

- REPL 对话可用。
- one-shot 对话可用。
- prompt skill 可用。
- `activate_skill` 可在同一 loop 内激活 prompt skill。
- native tool 经 ToolBroker 调用。
- shell tool timeout。
- 单个 subagent 调用。
- slash command 可用。
- `Ctrl+C` interrupt 可用。
- `/resume` 恢复 interrupted run。
- run 活跃期间新的普通 prompt 被拒绝。
- code-first workflow 成功路径。
- code-first workflow 失败路径。
- workflow interrupt/resume。
- workflow step failed/timeout/schema-invalid 后不自动重试并进入 error-handling。
- fake shader-debug-loop 完整控制流。
- MCP fake server 调用（Phase 5）。
- 全局级 / 项目级覆盖规则正确。
- REPL 启动显示 welcome panel。
- 输入 prompt 后用户消息固定显示。
- mock streaming model delta 能逐步渲染。
- mock tool call start/result 能显示工具块。
- 长 tool output 被截断展示。
- turn running 状态每秒更新，完成后固定为 completed。
- timeout 结果在 TUI 显示为 `timeout`，持久层保持 `failed + error_class=timeout`。
- `/exit` 显示 session closed 和 token summary。
- active execution 期间 `/exit` 不引入 mid-call cancel propagation，沿用 runtime safe-boundary 行为。
- active run 期间普通 prompt 被拒绝，并在 TUI 中清楚显示。
- one-shot 模式不启动 TUI，保持裸 stdout。
- 非 TTY 或注入 I/O 环境下自动回退到 `PlainReplView`，不启动 prompt_toolkit。
- prompt_toolkit 初始化失败时降级到 `PlainReplView`，并验证 warning 只输出一次。

Failure scenarios：

- shell timeout。
- 子代理超时。
- MCP server 启动失败（Phase 5）。
- agent 配置继承冲突。
- skill/agent/config 修改后当前 session 不热更新。
- 同一 workspace root 已有 active session 时启动失败。
- approval mode 切换记录失败。
- workflow checkpoint 存在但 workflow definition 已变化。
- tool denied。
- artifact 丢失。
- schema/parser 失败。
- model provider 调用失败。

Manual tests：

- macOS Terminal / iTerm2。
- 中文输入、中文退格（best-effort）。
- 多行 prompt。
- 快速连续输入历史切换。
- 长 markdown 输出。
- 长 tool result。
- 窄终端宽度下布局不崩溃，文本换行，状态栏可截断，工具块仍可读。

## Assumptions

- 语言使用 Python。
- Phase 0/0.5/1 默认通过 LangChain adapter 接模型。
- Phase 0.5 插入在 Phase 0 和 Phase 1 之间，只补强 REPL/TUI 与 streaming observation path，不提前引入 Phase 1+ agent 能力。
- 早期只实现一个稳定 provider 路径。
- OpenAI-compatible 后续通过 `ModelFactory` 或新 adapter 扩展。
- v1 metadata store 只支持 SQLite。
- v1 artifact store 只支持本地文件系统。
- v1 workflow 使用 code-first。
- YAML workflow DSL 不进入 v1。
- Plugin 不进入 v1 主线。
- MCP 不进入 v1 主线，Phase 5 才作为可选扩展实现。
- v1 不支持 workflow step-level retry；失败 step 直接进入 workflow error-handling。
- v1 同一工作目录最多一个 active session；并行场景使用 git worktree。
- v1 不支持 skill/agent/config 热更新；修改配置后必须启动新 session。
- Phase 4 才要求 shader-debug-loop readiness。
- Runtime 必须能支撑 shader-debug-loop，但不能包含 shader 专用分支。
- 删除旧 `Plan.md` 后，本 spec 足以作为技术方案和路线图的唯一来源。
