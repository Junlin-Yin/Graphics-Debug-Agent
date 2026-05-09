# debug-agent 技术方案与路线图（spec.md）

## Summary

本方案替代旧版 `Plan.md`。旧方案中可沿用的内容已合并：CLI 形态、Session/Run 模型、skill/agent 发现、prompt skill 动态激活、subagent、MCP 后置扩展、审批模式、ToolBroker、安全策略、持久化、session control、workflow runtime、测试计划和 phase roadmap。

新版方案的核心调整：

- Runtime Core 自研，LangChain/LangGraph 只作为 `AgentLoopAdapter`。
- Workflow 第一版采用 code-first，不提前设计复杂 YAML DSL。
- Phase 按垂直可运行切片推进。
- MCP 不在 v1 主线中实现，后移为 shader-debug readiness 之后的可选扩展。
- Plugin 后移为可选静态打包层。
- 持久化采用 SQLite `event log + checkpoint snapshot`。
- 目标是支撑 `shader-debug-loop`，但 runtime 不写死 shader 专用逻辑。

## Core Architecture

系统分为 7 层：

- `CLI Entrypoint`：`debug-agent`、one-shot、resume、status、trace、registry 查询。
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

## Agent Runtime

Agent loop 通过 adapter 接入。

```python
class AgentLoopAdapter:
    def run(self, request: AgentRunRequest, context: RunContext) -> AgentRunResult: ...
    def cancel(self, run_id: str) -> None: ...
```

Phase 0/1 默认 adapter：

- `LangChainAgentLoopAdapter`
- 使用 LangChain `create_agent`
- 使用 middleware 支持 dynamic prompt injection
- 不让 LangChain 拥有 session、run、checkpoint、approval、artifact、workflow 真值

Provider 策略：

- Phase 0/1 使用 LangChain 兼容 chat model。
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

## Assumptions

- 语言使用 Python。
- Phase 0/1 默认通过 LangChain adapter 接模型。
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
