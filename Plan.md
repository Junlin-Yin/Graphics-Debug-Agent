# Debug-Agent 0→1 技术方案与路线图（Plan v4）

## Summary
构建一个 **CLI 优先** 的 debug-agent，采用“三阶段递进”路线：

- **Phase 0：Core Runtime**
  - 跑通最小 LLM 对话
  - 建立 session、checkpoint、logging、context 基础设施
  - 支持 REPL 与 one-shot 两种 CLI 入口
  - 支持本地 slash command 骨架
- **Phase 1：Agent Capability**
  - 支持通用 prompt skill
  - 支持 tools、MCP、subagent
  - 支持两级 skill/agent/plugin 发现与加载
  - 支持会话级 interrupt / run control
- **Phase 2：Workflow Runtime**
  - 引入 `WorkflowEngine`
  - 支持 workflow 类型 skill
  - 把 `shader-debug-loop` 接入通用 runtime

v1 的核心原则已经确定：

- **workflow 是目标抽象，agent runtime 是启动层**
- **Phase 0/1 先验证 agent 基础设施，Phase 2 再上 workflow**
- **持久化采用 `SQLite first, Postgres-ready`**
- **skills / agents / plugins 支持全局级 + 项目级两级发现**
- **安全边界由 runtime 强制执行，不依赖 prompt 自觉**
- **普通模式 / semi-auto / yolo 三档执行策略**
- **workflow runtime 是一级执行系统，不退化为 agent loop**

## Architecture
### 1. 顶层分层
实现以下 7 层：

1. **CLI Entrypoint**
   - `debug-agent`
   - `debug-agent -p "xxx"`
   - `debug-agent resume <session_id>`
   - `debug-agent status <session_id>`
   - `debug-agent trace <session_id>`
   - `debug-agent skills list`
   - `debug-agent agents list`
   - `debug-agent plugins list`

2. **Runtime Orchestrator**
   - 统一入口调度
   - 管理 session、run stack、mode、context
   - 调用 `SkillResolver`
   - 根据 `execution_mode` 路由到：
     - `PromptSkillExecutor`
     - `WorkflowSkillExecutor`（Phase 2）

3. **Skill / Agent / Plugin Discovery**
   - `SkillRegistry`
   - `AgentRegistry`
   - `PluginRegistry`
   - 支持全局级与项目级两级扫描与覆盖

4. **Agent Runtime**
   - `PromptSkillExecutor`
   - `SubagentExecutor`
   - `LLMProvider`
   - `MCPServerManager`

5. **Workflow Runtime（Phase 2）**
   - `WorkflowLoader`
   - `WorkflowEngine`
   - `WorkflowSkillExecutor`

6. **Execution Services**
   - `ToolBroker`
   - `ApprovalModeManager`
   - `CommandRunner`
   - `SchemaValidator`
   - `TraceWriter`

7. **Persistence Services**
   - `CheckpointProvider`
   - `SessionStore`
   - `ArtifactStore`

### 1.1 Session / Run 模型
采用两层执行模型：

- `Session = runtime container`
- `Run = task / execution domain`

规则：

- 一个 session 持有一个 `run_stack`
- REPL 默认启动一个长寿命 `prompt run`
- one-shot 默认只创建一个单次 `prompt run`
- 当 prompt run 命中 workflow skill 时：
  1. 暂停当前 prompt run
  2. 创建新的 workflow run 压栈
  3. 运行 `WorkflowSkillExecutor`
  4. workflow run 结束后弹栈
  5. 恢复原 prompt run

v1 只支持单层 handoff：

- `prompt run -> workflow run -> 返回 prompt run`

不支持：

- workflow 内再次嵌套 workflow
- 任意深度 run 嵌套

### 2. 技术栈
- 语言：`Python`
- Phase 0/1 agent loop：优先复用现成框架能力，推荐 `LangChain create_agent`
- Phase 0/1 product/runtime harness：自研
- 工作流执行：Phase 2 使用自研 `WorkflowEngine`
- 版本策略：
  - 采用 **LangChain v1.x + LangGraph v1.x** 的最新稳定小版本组合
  - 不跟随 alpha / beta / rc 版本
  - 优先选择官方文档中仍推荐 `create_agent` + middleware 的版本线
- 持久化：
  - v1：SQLite + 本地文件系统
  - v2：Postgres + 本地或对象存储
- 配置格式：
  - skill：`SKILL.md` YAML header
  - workflow：`workflow.yaml`
  - agent：`agent.toml`
  - MCP：`mcp.toml`
  - plugin：`plugin.toml`

## CLI and Modes
### 1. CLI 形态
采用类似 `claude-code` 的入口行为：

- `debug-agent`
  - 进入 REPL
  - 默认 `--normal`
- `debug-agent -p "xxx"`
  - 执行 one-shot
  - 默认 `--yolo`

REPL 额外支持本地 slash command：

- `/skills`
- `/agents`
- `/models`
- `/status`
- `/resume`
- `/exit`
- `/compress`

无论 REPL 还是 one-shot，都允许显式覆盖：

- `--normal`
- `--semi-auto`
- `--yolo`

### 2. 审批模式
支持 3 档模式：

- `normal`
  - 任何工具调用都要求审批
  - 允许在当前 session 内记住同类批准
- `semi-auto`
  - 低风险只读自动放行
  - 写文件 / shell / git / 外部网络操作审批
- `yolo`
  - 所有工具调用直接执行

REPL 模式支持 `Ctrl+Y` 在会话中轮换：

- `normal -> semi-auto -> yolo -> normal`

规则：

- `Ctrl+Y` 仅在 REPL 中生效
- 模式切换写入 session log
- one-shot 不支持运行时切换，但可通过 `--normal / --semi-auto / --yolo` 在启动时指定

### 3. Slash Commands
slash command 由 **CLI / REPL 层本地解析**，不进入 LLM。

原因：

- 它们本质上是 runtime 控制面，不是任务语义
- 本地执行可避免模型误解和权限绕过
- 结果可直接读取 registry / session store，成本低、可预测

建议分期：

- Phase 0：
  - `/exit`
  - `/status`
  - `/skills`
  - `/agents`
  - `/models`
- Phase 1：
  - `/resume`
  - `/compress`

最小语义：

- `/skills`
  - 列出当前可发现 skills 及来源
- `/agents`
  - 列出当前可发现 agents 及来源
- `/models`
  - 显示当前默认模型与可选模型
- `/status`
  - 显示 session id、mode、active run、interrupted run、pending approval、active skill
- `/resume`
  - 恢复最近一个 `interrupted` run
- `/exit`
  - 退出当前 REPL；若有活跃 run，先请求中断或确认
- `/compress`
  - 触发一次会话摘要压缩，生成新的 conversation summary checkpoint

说明：

- `/compress` 不应只是截断历史，而应写入结构化 summary 并保留可追溯原始记录
- `/compress` 仅在 idle 状态执行，不在 active / interrupted run 上直接操作
- `/compress` 通过 `ConversationCompressionService` 读取 conversation state，生成新的 conversation summary checkpoint
- slash command parser 建议独立于 agent runtime，作为 REPL command bus
- `SlashCommandRouter` 依赖 `RunController`
  - 状态变更类命令：`/resume` `/exit`
  - 通过 `RunController` 修改 run 状态并写入 `SessionStore`
  - 只读类命令：`/status` `/skills` `/agents` `/models`
  - 直接读取 `SessionStore`、registry 和 `RunController` 的状态快照
- 若当前存在 `running` run：
  - 普通用户 prompt 一律拒绝执行，并提示等待运行结束或先 `Ctrl+C`
  - 允许只读 slash command：`/status` `/skills` `/agents` `/models`
  - 允许控制命令：`/exit`
  - `/resume` `/compress` 在 `running` 状态下拒绝执行

`/compress` 的边界：

- 只压缩 **LLM 可见历史**
- 不做 `state compaction`
- 不删除真实 checkpoint
- 不修改 workflow state
- 不修改 authoritative state

状态分类：

- `authoritative state`
  - 恢复执行与状态推进所依赖的结构化真值
  - 例如 workflow state、checkpoint state、关键结构化 tool/subagent 输出、artifact 索引
- `compressible observations`
  - 仅供 LLM 理解的历史文本与冗长输出
  - 例如 read_file 大段内容、搜索结果、解释性 stdout

## Discovery Model
### 1. 两级目录
支持两级发现：

- **全局级**：`~/.debug-agent/`
- **项目级**：`<project_root>/.debug-agent/`

建议目录结构统一为：

```text
~/.debug-agent/
  skills/
  agents/
  plugins/
  mcp.toml
  config.toml

<project_root>/.debug-agent/
  skills/
  agents/
  plugins/
  mcp.toml
```

### 2. 优先级与覆盖
优先级：

1. 项目级
2. 全局级
3. 内置默认

规则：

- 同名 `skill`：项目级完全覆盖全局级
- 同名 `agent`：项目级完全覆盖全局级
- 同名 `plugin`：项目级完全覆盖全局级
- 不做文件级或目录级 merge

`mcp.toml` 合并规则：

- 先加载全局 `mcp.toml`
- 再加载项目级 `mcp.toml`
- 不同名 server 追加
- 同名 server 由项目级覆盖全局级

所有 registry 返回的对象都带 `source_scope`：

- `project`
- `global`
- `builtin`

## Skill System
### 1. skill 目录结构
每个 skill 自包含：

```text
skills/
  shader-debug-loop/
    SKILL.md
    workflow.yaml
    agents/
      renderdoc-debugger/agent.toml
      shader-debugger/agent.toml
```

规则：

- 所有 skill 必须有 `SKILL.md`
- `SKILL.md` 开头使用 YAML header，作为 skill manifest
- `prompt` skill 只需要 `SKILL.md`
- `workflow` skill 需要 `SKILL.md`，并可额外带 `workflow.yaml`
- `SKILL.md` 同时承担：
  - 运行时 metadata
  - 被注入 prompt 的正文载体

front matter 格式规范：

- `SKILL.md` 必须以 `---` 开头
- YAML header 必须由第二个 `---` 显式闭合
- 开头不允许前置空白、注释或正文
- front matter 解析失败时，该 skill 拒绝加载并记录 error log

推荐结构：

```markdown
---
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
    - shader-debugger
metadata:
  platform:
    - windows
  tags:
    - shader
    - debug
---

# Shader Debug Loop

这里开始是 skill 正文。
```

### 2. `SKILL.md` YAML header 最小 schema

```yaml
name: shader-debug-loop
description: Automated iterative shader debugging loop
execution_mode: workflow   # workflow | prompt
triggers:
  - shader bug
  - graphics test failure
depends_on:
  skills:
    - renderdoc-gpu-debug
  agents:
    - renderdoc-debugger
    - shader-debugger
metadata:
  platform:
    - windows
  tags:
    - shader
    - debug
    - workflow
```

字段约束：

- `name`：唯一标识，默认要求与目录名一致
- `description`：必填，供发现、列表和路由展示
- `execution_mode`：可选，`workflow` 或 `prompt`
  - 仅当同时满足以下条件时，skill 才按 `workflow` 处理：
    - 存在 `workflow.yaml`
    - `execution_mode: workflow` 显式声明
  - 其余情况一律按 `prompt` skill 处理
- `depends_on`：依赖的 skill 和 agent
- `metadata`：只用于展示和过滤

必填字段：

- `name`
- `description`

可选字段：

- `execution_mode`
- `triggers`
- `depends_on`
- `metadata`
- 未来扩展字段

实现建议：

- registry 启动时只解析 YAML header，不读取整段正文
- runtime 激活 skill 时再按需读取正文部分
- `SKILL.md` 正文应通过运行时动态 prompt 机制追加到 agent system prompt 之后，而不是覆盖它
- Phase 1 不支持一个 skill 同时作为 prompt/workflow 两种入口的多态语义

### 3. skill 路由
调用链固定为：

```text
CLI Request
-> Runtime Orchestrator
-> SkillResolver
-> SkillManifest (from SKILL.md header)
-> if execution_mode == workflow:
     WorkflowSkillExecutor.run(...)
   else:
     PromptSkillExecutor.run(...)
```

路由优先级：

1. 用户显式指定 `workflow` skill
2. 触发词/规则命中 `workflow` skill
3. 默认主 agent / `PromptSkillExecutor`

说明：

- 这里的“skill 调用链”是 **顶层路由链**
- 它只负责决定本次请求进入哪个 executor
- 对 `workflow` skill：由 orchestrator 在 run 开始前直接路由
- 对 `prompt` skill：orchestrator 只提供 skill catalog、白名单和候选集合，不预先注入正文
- 它不等同于模型在运行中调用某个工具

### 4. skill 激活机制
Phase 0/1 对 `prompt` skill 建议采用 **`activate_skill` 内置工具 + state-driven dynamic prompt injection**。

原因：

- skill 的正文往往较长，不适合在 run 开始时预先加载多个 skill
- skill 依赖链可能在运行中才暴露，适合按需渐进式激活
- 这更符合成熟 agent 的常见模式：工具更新状态，后续模型调用按状态改变上下文

结论：

- `activate_skill` 是 Phase 1 核心能力，不再是后续扩展
- `workflow` skill 仍然由 orchestrator 直接路由，不通过 `activate_skill`
- `prompt` skill 的正文不在 pre-run 全量拼装，而是在运行中按需加载
- runtime 仍然负责白名单校验、去重、预算控制和激活状态维护，不允许 agent 任意绕过边界

最小调用链：

```text
User prompt
-> PromptSkillExecutor / create_agent
-> model sees base prompt + skill catalog summary
-> model calls activate_skill("skill_x")
-> runtime validates + loads SKILL.md body
-> tool updates active_skills state
-> middleware rebuilds effective prompt before next model call
-> model continues in same loop
```

最小状态：

- `available_skills`
  - 来自 registry/header，用于给 agent 看到轻量 catalog
- `active_skills`
  - 当前 run 已激活的 prompt skills

最小约束：

- `activate_skill(name)` 只能激活当前 agent 白名单内的 prompt skills
- 重复激活同一 skill 应幂等
- skill 正文生效于 **下一次 model call**
- 不通过重启当前 loop 实现 skill 注入

### 5. 与 `LangChain create_agent` 的集成策略
Phase 1 固定采用 **middleware 路径**，不自定义 graph node，也不通过 callback/hook 临时拼接历史消息。

结构：

```text
PromptSkillExecutor
-> create_agent(model, tools=[..., activate_skill], middleware=[SkillPromptMiddleware, ...])
-> agent state includes: active_skills
-> ActivateSkillTool updates active_skills
-> SkillPromptMiddleware.before_model(...)
-> rebuild effective system prompt for this model call
```

规则：

- `ActivateSkillTool` 负责：
  - 校验 skill 是否存在
  - 校验是否属于当前 agent 白名单
  - 读取对应 `SKILL.md` 正文
  - 更新 `active_skills`
- `SkillPromptMiddleware` 负责：
  - 在每次 model call 前读取 `active_skills`
  - 以“基础 system prompt + active skill 正文”的顺序重建本次调用的有效 prompt
- 不修改历史 message 列表中的旧 system message
- 不为 skill 激活而重启当前 loop

## Agent Runtime
### 1. Provider 策略
Phase 0/1 的 provider 策略：

- **直接采用 LangChain 兼容的 Anthropic chat model**
- **不在 Phase 0/1 自己实现完整 provider abstraction**
- **后续如需支持 OpenAI-compatible，再在 `ModelFactory` 层扩展**

子代理和主代理允许使用不同模型，模型配置写在各自的 `agent.toml` 中。

### 2. `agent.toml` 最小 schema
最小字段：

- `name`
- `provider`
- `model`
- `system_prompt`
- `skills`
- `allowed_tools`
- `disallowed_tools`
- `mcp_servers`
- `max_turns`
- `max_tokens`
- `timeout_seconds`
- `temperature`
- `output_mode`
- `output_parser`
- `output_schema`

字段说明补充：

- `skills`：该 agent **可激活**的 skill 白名单，不代表启动时全部注入

字段继承规则：

- 对以下字段支持**字段级继承/覆盖**：
  - `provider`
  - `model`
  - `skills`
  - `allowed_tools`
  - `disallowed_tools`
  - `mcp_servers`
  - `max_tokens`
  - `timeout_seconds`
  - `temperature`
- 优先级：
  1. `agent.toml` 显式值
  2. 主 agent 当前配置
  3. 系统默认值

不继承的字段：

- `name`
- `system_prompt`
- `output_mode`
- `output_parser`
- `output_schema`
- `max_turns`

### 3. Prompt 组合规则
子代理与主代理统一采用以下 prompt 组合顺序：

1. runtime 固定安全前缀
2. `agent.toml.system_prompt`
3. 当前 `active_skills` 对应的 skill 正文
4. 任务输入

规则：

- runtime 安全前缀优先级最高
- `system_prompt` 定义角色与固定边界
- `SKILL.md` 只补充方法论和任务领域知识
- 用户输入不覆盖系统层内容
- `agent.toml.skills` 只声明“可激活 skill 集”
- skill 采用两阶段加载：
  - 启动时只读取 header / metadata
  - 运行时按需渐进式披露正文
- `prompt` skill 的激活由 agent 通过 `activate_skill` 发起，由 runtime 校验并更新状态
- `workflow` skill 仍由 orchestrator 直接路由
- dynamic prompt middleware 在每次 model call 前按 `active_skills` 重建有效 prompt
- skill 注入不通过修改历史消息或重启 loop 实现
- 不允许 agent config 强制要求预激活 skill
- 不允许 subagent 自主展开任意未授权 skill

### 4. Agent 执行循环
Phase 0/1 不手搓完整 ReAct loop，优先复用现成框架能力，推荐 `LangChain create_agent` 作为 agent loop 内核。

系统分工：

- 框架负责：
  - LLM tool-calling loop
  - tool call parsing
  - turn-by-turn agent execution
  - middleware / state-driven prompt rebuilding
- 自研 runtime 负责：
  - session
  - approval mode
  - ToolBroker
  - skill progressive loading
  - `activate_skill` tool
  - skill catalog / active state 管理
  - MCP server 管理
  - logging / trace
  - subagent launch 包装

最小执行循环语义必须满足：

1. LLM call
2. parse tool calls
3. ToolBroker dispatch
4. feed tool results back
5. 直到 final answer / max_turns / timeout

终止条件：

- LLM 返回最终文本且无 tool call
- 达到 `max_turns`
- 达到 `timeout`
- 出现不可恢复错误

tool error 策略：

- 默认将 tool 错误结果回喂给 LLM 一次
- 若连续失败超过阈值，则终止当前 agent run

说明：

- `PromptSkillExecutor` 的本质是“prompt-style agent run executor”
- 它内部实现为 agent loop 是正确的
- skill 不是 run 启动前一次性拼装完成的静态上下文
- 对 `prompt` skill，executor 在同一 loop 内支持“工具激活 -> 状态更新 -> 下一次 model call 生效”
- 即使没有任何 skill，也应能进入同一个 agent loop，只是此时只运行基础 agent prompt

命名建议：

- 若后续实现阶段觉得 `PromptSkillExecutor` 容易误导，可重命名为：
  - `PromptAgentExecutor`
  - 或 `AgentExecutor`

### 4.1 ModelFactory
Phase 0/1 只实现一个很薄的 `ModelFactory` / `ModelResolver`：

- 从 `~/.debug-agent/config.toml` 读取全局默认模型配置
- 结合 `agent.toml` 解析实际使用的模型参数
- 输出 LangChain 兼容的 chat model 实例

这层不承担通用 provider 抽象职责，只负责配置解析与实例化。

### 5. `SubagentExecutor`
Phase 1 只实现 **单个子代理执行器**，不做 `SubagentPipelineExecutor`。

`SubagentExecutor` 负责：

1. 读取 `agent.toml`
2. 解析继承后的 agent 配置
3. 装配 MCP tools 与 native tools
4. 注入 prompt
5. 执行子代理
6. 返回文本输出或解析后的结构化输出

说明：

- 多阶段子代理编排先由上层显式串联
- 到 Phase 2 再决定是否需要 pipeline executor
- `SubagentExecutor` 只存在于 prompt run 或 workflow step 中，不改变当前 run 的 primary executor

### 6. 子代理输出约束
框架**不强制**子代理输出 JSON。

规则：

- 默认输出模式：`text`
- 如果某个 agent 或 workflow 需要结构化输出，则在 `agent.toml` 中显式声明：
  - `output_mode`
  - `output_parser`
  - `output_schema`

也就是说：

- 输出约束由 agent 配置和 skill 决定
- framework 只提供“可选解析/校验能力”

### 7. approval mode 与 agent 的关系
`approval_mode` 属于 **session/runtime**，不属于 agent。

规则：

- 同一 session 内，主 agent 和所有 subagent 共享同一个 `approval_mode`
- `Ctrl+Y` 切换的是整个 session 的 mode
- `ToolBroker` 每次工具调用时读取当前 session 的 `approval_mode`
- agent 配置文件中不声明 approval mode

## MCP Integration
### 1. 目标
框架支持 MCP，但不把 `sequential thinking` 写死成框架内建能力。

含义：

- 只要框架支持 MCP，就可以接入任意 MCP server
- `sequential thinking` 是 `shader-debug-loop` 运行时的重要依赖，但不是框架核心耦合点

### 2. 配置分层
MCP 配置分两层：

- 主代理：全局 / 项目级 `mcp.toml`
- 子代理：`agent.toml` 中的 `mcp_servers`

配置优先级：

1. 子代理显式 `mcp_servers`
2. 主代理当前配置
3. 无配置则不注入 MCP

### 3. `mcp.toml`
全局文件名固定为：

- `~/.debug-agent/mcp.toml`
- `<project_root>/.debug-agent/mcp.toml`

最小结构：

```toml
[servers.sequential-thinking]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-sequential-thinking"]

[servers.example-http]
transport = "http"
url = "http://localhost:8080/mcp"
```

### 4. `MCPServerManager`
职责：

- 加载 `mcp.toml`
- 按配置启动或连接 MCP server
- 管理 server 生命周期
- 为 agent runtime 提供可注入的 MCP tools

Phase 1 只支持：

- `stdio`
- `http` / `streamable_http`

## Global Config
### 1. `config.toml`
保留全局配置文件：

- `~/.debug-agent/config.toml`

项目目录下**不使用** `config.toml`。

`config.toml` 只负责全局默认值：

- 默认 provider
- 默认 model
- api key / env key 定位
- 默认 timeout / token 参数
- REPL / one-shot 的全局缺省行为参数

不负责：

- skill 定义
- agent 定义
- MCP server 定义
- workflow 定义

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

## Plugins
### 1. 是否支持
Phase 1 支持**轻量 plugin/package loader**，不做完整插件平台。

目标：

- 支持将一组 `skill + agent + config` 打包组织
- 支持两级发现与加载
- 不做远程市场、动态代码 hook、复杂依赖求解

定义：

- `Skill`：运行时可激活的能力单元
- `Plugin`：纯组织/分发单元，包含一组 skills/agents/config，不引入新的运行时语义

### 2. 目录与结构
发现路径：

- `~/.debug-agent/plugins/`
- `<project_root>/.debug-agent/plugins/`

最小结构：

```text
plugins/
  shader-debug/
    plugin.toml
    skills/
    agents/
```

`plugin.toml` 最小字段：

- `name`
- `version`
- `description`
- `skills`
- `agents`

## Workflow Runtime
### 1. 总体策略
Workflow runtime 放到 **Phase 2** 实现。

原则：

- 只以 `shader-debug-loop` 为目标验证 workflow 能力
- 避免过早把 DSL 设计成通用工作流语言
- 不在 Phase 2 引入 LangGraph 编译，继续使用自研 `WorkflowEngine`
- `WorkflowSkillExecutor` 是一级执行系统，不允许内部退化为 `PromptSkillExecutor` 主循环

### 2. 最小 DSL
只支持最小必要集：

- `state`
- `steps`
- `transitions`
- `post_hook`
- `timeout`

补充规则：

- 多路分支通过 `transitions` 数组按顺序求值实现
- 第一条命中的 transition 生效
- runtime 自动维护少量系统字段：
  - `phase`
  - `status`
  - `last_step_id`
- 业务字段不做隐式自动写入 `state`
- 业务字段由 `python handler` 显式更新

Phase 2 的 step 类型仅保留：

- `shell`
- `python`
- `subagent`
- `interrupt`

不在 Phase 2 引入：

- `branch` step
- `subagent_pipeline`
- `append_trace` step
- `schema_validate` step
- `set_state` step

这些能力由 runtime 或 handler 承担。

明确不支持：

- nested workflow
- dynamic workflow generation
- runtime step injection
- loop construct
- parallel execution
- event-driven transition
- arbitrary Python expression

目标保持为：

- **deterministic finite workflow**

### 3. `workflow.yaml` 精简示例

```yaml
state:
  retry: 0
  max_retry: 3
  active_case: null

steps:
  - id: apply_case
    type: python
    function: init_active_case
    transitions:
      - when: "error != ''"
        target: report_failure
      - when: ""
        target: build

  - id: build
    type: shell
    command: jam WinEditor && jam WinPlayerNoDevelopment -sCONFIG=master
    timeout: 600
    transitions:
      - when: "exit_code == 0"
        target: test
      - when: ""
        target: report_failure

  - id: test
    type: shell
    command: perl utr.pl ...
    timeout: 600
    post_hook: parse_test_result
    transitions:
      - when: "data.test.meta.result == 'PASS'"
        target: report_success
      - when: "state.retry >= state.max_retry"
        target: report_failure
      - when: ""
        target: collect

  - id: collect
    type: python
    function: collect_artifacts
    transitions:
      - when: "error != ''"
        target: report_failure
      - when: ""
        target: pause_for_review

  - id: pause_for_review
    type: interrupt
    message: "Review collected artifacts before debugging"
    transitions:
      - when: ""
        target: debug

  - id: debug
    type: subagent
    agent: shader-debugger
    input:
      active_case: "{{state.active_case}}"
    transitions:
      - when: "error != ''"
        target: report_failure
      - when: ""
        target: apply_fix

  - id: apply_fix
    type: python
    function: apply_fix_and_increment_retry
    transitions:
      - when: "error != ''"
        target: report_failure
      - when: ""
        target: build

  - id: report_success
    type: python
    function: finalize_success
    transitions:
      - when: ""
        target: cleanup

  - id: report_failure
    type: python
    function: finalize_failure
    transitions:
      - when: ""
        target: cleanup

  - id: cleanup
    type: python
    function: cleanup_session
```

### 4. transition DSL
采用**受限 Python-like 布尔表达式**。

允许：

- 比较：`== != < <= > >=`
- 逻辑：`and or not`
- 常量：字符串、数字、布尔、null
- 以白名单根名称开头的多级属性访问：
  - `state.retry`
  - `data.test.meta.result`
  - `exit_code`
  - `error`

补充规则：

- `when: ""` 表示默认/兜底分支
- 兜底分支应排在其他带条件 transition 之后
- `transitions` 按顺序求值，第一条命中的规则生效
- 若某个 step 的 `transitions` 为空或未定义，则该 step 执行完成后 workflow 以当前状态终止

模板语法：

- 使用 **Jinja2 子集**
- 仅允许变量插值，不允许控制结构
- 模板上下文当前支持：
  - `state`
  - `data`
  - `output`
  - `error`

禁止：

- 函数调用
- 下标访问
- import
- comprehension

### 5. `ConditionEvaluator`
使用 AST 白名单模型。

允许节点：

- `Expression`
- `BoolOp`
- `UnaryOp`
- `Compare`
- `Name`
- `Attribute`
- `Constant`
- `Load`

允许操作符：

- `And`
- `Or`
- `Not`
- `Eq`
- `NotEq`
- `Lt`
- `LtE`
- `Gt`
- `GtE`

名称白名单：

- `state`
- `data`
- `exit_code`
- `stdout`
- `stderr`
- `error`
- `output`
- `true`
- `false`
- `null`

### 6. `data` 的来源与生命周期
`data` 定义为 **workflow 级累积结果字典**。

规则：

- 每个 step 执行完成后，runtime 将该 step 的结果写入 `data.<step_id>`
- 推荐结构：
  - `data.<step_id>.exit_code`
  - `data.<step_id>.error`
  - `data.<step_id>.output`
  - `data.<step_id>.meta`
- `post_hook` 可以向 `data.<step_id>.meta` 写结构化结果
- `state` 用于当前流程控制
- `data` 用于历史结果归档

### 7. `WorkflowEngine`
职责：

- 加载 workflow
- 初始化 `WorkflowContext`
- 执行 step
- 写 checkpoint
- 运行 `post_hook`
- 解析 transition
- 决定下一步或中断/终止

`post_hook` 执行规则：

1. step 执行完成后先生成 `StepResult`
2. 再执行 `post_hook`
3. 然后再做 transition 求值

`post_hook` 可修改：

- `state`
- `data.<step_id>.meta`
- `StepResult.error`

`interrupt` 语义：

- 只有 **step boundary** 保证 resume correctness
- step 内 interrupt：best-effort
- step 完成后 checkpoint：strong guarantee

明确不支持：

- token-level resume
- tool-mid-flight resume
- subagent-mid-thought resume

最小核心结构：

- `WorkflowContext`
- `StepResult`
- `ConditionEvaluator`
- `BaseStepExecutor`
- `ShellExecutor`
- `PythonExecutor`
- `SubagentStepExecutor`
- `InterruptExecutor`
- `WorkflowEngine`

### 8. `StepResult Contract`
所有 workflow step executor 必须输出统一的 `StepResult` 结构。

最小协议：

```python
class StepResult:
    step_id: str
    status: Literal["success", "failed", "interrupted", "timeout"]
    output: Any
    error: str | None
    started_at: datetime
    finished_at: datetime
    artifacts: list[str]
    meta: dict[str, Any]
```

说明：

- `ConditionEvaluator`、transition、trace、resume、observability 都依赖这一统一协议
- 不允许不同 step type 返回不同形状的结果

## Persistence
### 1. 总体策略
采用 **`SQLite first, Postgres-ready`**：

- v1：SQLite + 本地文件系统
- v2：Postgres + 可替换 ArtifactStore

session 根目录固定放在项目根目录：

- `<project_root>/.sessions/`

说明：

- v1 默认保持 metadata 与 artifacts 同根，方便本地可见性
- 后续支持将 `artifact_root` 与 `metadata_root` 分离
- 未来可迁移到 `~/.debug-agent/artifacts/`

建议目录：

```text
.sessions/
  runtime.db
  <session_id>/
    trace.jsonl
    trace.md
    logs/
    temp/
    artifacts/
```

### 2. `CheckpointProvider`
职责：

- 保存运行快照
- 恢复最近 checkpoint
- 列出 checkpoint 历史

最小接口：

```python
class CheckpointProvider(Protocol):
    def init(self) -> None: ...
    def save_checkpoint(self, session_id: str, state: dict, step_id: str) -> str: ...
    def load_latest(self, session_id: str) -> dict | None: ...
    def load_checkpoint(self, checkpoint_id: str) -> dict | None: ...
    def list_checkpoints(self, session_id: str) -> list[dict]: ...
```

Phase 区分：

- Phase 0/1：conversation/session-level persistence
- Phase 2：workflow-level checkpoint

### 3. `SessionStore`
职责：

- 管理 session 元数据
- 服务于 CLI 查询与恢复
- 管理并发恢复控制

最小接口：

```python
class SessionStore(Protocol):
    def create_session(self, session: dict) -> None: ...
    def update_session(self, session_id: str, patch: dict) -> None: ...
    def get_session(self, session_id: str) -> dict | None: ...
    def list_sessions(self, status: str | None = None) -> list[dict]: ...
```

最小字段：

- `session_id`
- `run_stack`
- `status`
- `artifact_root`
- `metadata_root`
- `config_snapshot`
- `latest_checkpoint_id`
- `approval_mode`
- `created_at`
- `updated_at`
- `error_summary`
- `version`

说明：

- `SessionStore` 保存的是产品层 session 元数据，不等同于 checkpoint 真值
- `skill_name` 属于 orchestrator / session metadata，可存于 session 表但不是早期通用 runtime state 的最小要求
- `retry/max_retry` 属于 workflow-level state，只在 Phase 2 的 workflow context 中出现
- `version` 是 **乐观锁版本号**，用于并发更新控制，不是 schema version
- `config_snapshot` 是 **immutable** 的 session 配置快照，run 不允许修改

并发安全：

- 使用最小乐观锁机制
- 同一 session 同时只能有一个 active runner

### 4. `ArtifactStore`
职责：

- 创建 session 目录
- 存储日志、trace、捕获文件、diff、报告
- 清理临时文件

最小接口：

```python
class ArtifactStore(Protocol):
    def create_session_root(self, session_id: str) -> str: ...
    def write_text(self, session_id: str, relative_path: str, content: str) -> str: ...
    def write_bytes(self, session_id: str, relative_path: str, content: bytes) -> str: ...
    def resolve_path(self, session_id: str, relative_path: str) -> str: ...
    def cleanup_temp(self, session_id: str) -> None: ...
```

### 5. `RunState`
`RunState` 是执行态的一等对象。

最小字段：

- `run_id`
- `parent_run_id`
- `run_type`: `prompt | workflow`
- `status`
- `active_skills`
- `checkpoints`
- `run_local_summary`
- `run_context`
- `event_stream`

scope 规则：

- `active_skills` 是 **run-scope**
- run 结束后自动清空
- REPL 中一个连续任务通常对应一个长寿命 run

### 6. `RunContext`
采用统一外壳，但不抹掉 agent / workflow 的差异：

- `agent_context`（可选）
- `workflow_context`（可选）
- `runtime_vars`
- `tool_bindings`
- `memory_view`

### 7. Tool State Scope
tool state 不默认全部归 run。

规则：

- tool state 必须声明 scope：
  - `session`
  - `run`

示例：

- MCP connection：session-scope
- 单次执行中的临时结果：run-scope

### 8. Run Event Stream
每个 run 维护一个轻量结构化事件流：

- `event_id`
- `timestamp`
- `kind`
- `run_id`
- `step_id`（可选）
- `payload`

用途：

- replay
- audit
- debug

## Tooling and Safety
### 1. ToolBroker
统一权限层，按以下维度裁剪：

- agent
- approval mode
- workflow phase
- path scope

最小权限模型：

- `allowed_tools`
- `disallowed_tools`
- `path_policies`

概念分层：

```text
ToolBroker
 ├── ToolPolicyEngine
 ├── ApprovalService
 ├── ToolDispatcher
 └── ToolAuditLogger
```

Phase 1 可以先不物理拆文件，但概念上按此分层设计。

### 2. path scope
v1 的 path policy 先支持：

- `read`
- `write`

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

### 3. 审批缓存
`normal` 模式下允许当前 session 内记住“同类批准”。

approval key 最小定义为：

- `tool_name`
- `risk_level`
- `scope_signature`

### 4. MCP Tool Isolation
MCP tool 不允许原样直接暴露给 agent。

硬规则：

```text
Raw MCP Tool
 -> ToolBroker wrapper / re-binding
 -> approval / policy / risk / path enforcement
 -> runtime visible tool
```

原因：

- 避免 MCP tool 绕过 approval mode
- 避免绕过 path policy / risk policy
- 保证 native tool 与 MCP tool 走统一审计链路

### 5. Observability
区分两类日志：

- `engine.log`
  - step start/end
  - session state change
  - checkpoint save/load
  - tool allow/deny
  - approval mode switch
- `workflow/log artifacts`
  - 外部命令 stdout/stderr
  - 子代理原始输出
  - schema 校验失败详情

最小结构化字段：

- `timestamp`
- `session_id`
- `step_id`
- `level`
- `event`
- `message`

## Session Control
### 1. 目标
Phase 1 引入最小会话控制能力：

- `Ctrl+C` 中断当前活跃 run
- REPL 在 run 活跃期间接收新的用户输入
- session store 能记录 run 状态迁移

### 2. `Ctrl+C` 的必要性与边界
这是高必要性能力，原因：

- CLI agent 如果不可中断，交互体验和安全性都明显不足
- shell/tool/subagent 都可能出现长时间阻塞
- 这是后续 workflow interrupt / resume 的前置能力

最小实现边界：

- 第一次 `Ctrl+C`
  - 请求优雅中断当前 run
  - 传播 cancellation token / interrupt flag
  - 在最近 checkpoint / 安全边界将 run 标记为 `interrupted`
- 第二次 `Ctrl+C`
  - 允许强制终止当前前台等待

中断粒度：

- 优先在 turn 边界、tool 调用边界、子代理等待边界生效
- 对不可抢占的外部阻塞命令，只保证 best-effort 终止

### 3. 运行中接收新 prompt
该能力在 Phase 0/1 收敛为 **单 session、单 active run，运行中拒绝新的普通 prompt**。

原因：

- 同一 session 内接受运行中追加 prompt，会显著增加状态机和恢复语义复杂度
- Phase 0/1 优先保证 interrupt / resume 路径稳定，而不是做运行中多输入管理

Phase 1 建议语义：

- 若当前 run 活跃，新的普通 prompt 一律拒绝执行
- runtime 返回明确提示：
  - 等待当前 run 完成后再发送
  - 或先使用 `Ctrl+C` 中断
- 只读 slash command 仍允许执行
- runtime 需要给每个 run 分配 `run_id`，并显式记录：
  - `running`
  - `interrupted`
  - `completed`
  - `failed`

### 3.1 Run 状态流转
最小状态机：

- `running -> completed | failed | interrupted`
- `interrupted -> running`

语义：

- `interrupted` 不是终态，支持 `/resume`
- `/resume` 恢复最近一个 `interrupted` run，并从最近 checkpoint / 安全边界继续
- `/exit` 会：
  - 中断当前 `running` run
  - 保留当前 session 与 interrupted 状态
  - 退出当前 REPL

### 4. 实现要点

- `SessionStore` 之外增加 `RunController` / `RunState`
- LLM 调用、tool 执行、subagent 执行都读取同一个 cancellation token
- REPL 输入循环与 runner 分离，避免输入阻塞执行线程
- `slash command` 与 `Ctrl+C` 都通过同一控制面修改 run 状态
- `RunController` 负责 interrupt / resume 的唯一状态机入口

这也意味着：

- `/status` 需要展示 active run / interrupted run
- `/resume` 需要展示目标 interrupted run 或默认恢复最近一个
- `/exit` 需要先处理活跃 run
- `/compress` 仅在 idle 状态执行

## Phase Roadmap
### Phase 0: Core Runtime
目标：搭起最小可对话基础设施

实现内容：

1. CLI 双入口
   - `debug-agent`
   - `debug-agent -p "xxx"`
2. `ModelFactory` + LangChain 兼容 Anthropic model
3. session / context / checkpoint 基础设施
4. SQLite 持久化
5. `.sessions/` 目录结构
6. 基础日志
7. 审批模式骨架
8. `RunState` / `run_stack` 骨架
8. REPL slash command 骨架

验收标准：

- CLI 能和 LLM 进行最小对话
- REPL 和 one-shot 都能启动 session
- one-shot 默认 `yolo`，REPL 默认 `normal`
- session、checkpoint、log 能写入 `.sessions/`
- `/exit` `/status` `/skills` `/agents` `/models` 可本地执行

Phase 0 的“基础日志”仅包括：

- session 生命周期日志
- agent turn 日志
- tool dispatch / approval 日志
- provider 调用错误日志

### Phase 1: Agent Capability
目标：支持通用 agent 能力

实现内容：

1. `SKILL.md` YAML header loader
2. `agent.toml`
3. `mcp.toml`
4. `SkillRegistry` / `AgentRegistry`
5. `PluginRegistry`
6. `PromptSkillExecutor`
7. `SubagentExecutor`
8. `ToolBroker`
9. `MCPServerManager`
10. 全局 / 项目 两级发现
11. MCP tool re-binding
12. `run.events[]`
11. `RunController` / interrupt handling
12. `/compress`
13. `activate_skill` 内置工具
14. `SkillPromptMiddleware` / dynamic prompt injection
15. `/resume`

验收标准：

- 能加载通用 prompt skill
- 能调用 tools
- 能接 MCP server
- 能调用单个 subagent
- 能从全局级和项目级目录发现 skill/agent/plugin
- `prompt` skill 可在运行中通过 `activate_skill` 激活
- skill 激活后无需重启 loop，并在下一次 model call 生效
- `Ctrl+C` 可中断活跃 run
- `interrupted` run 可通过 `/resume` 从最近 checkpoint / 安全边界恢复
- run 活跃期间新的普通 prompt 会被拒绝，并提示等待结束或先中断
- `/compress` 可在 idle 状态生成会话摘要并继续对话

### Phase 2: Workflow Runtime
目标：支持 workflow skill 并接入 `shader-debug-loop`

实现内容：

1. `workflow.yaml`
2. `WorkflowEngine`
3. `ConditionEvaluator`
4. workflow checkpoint / resume
5. `WorkflowSkillExecutor`
6. `shader-debug-loop` 接入
7. 与 `renderdoc-debugger` / `shader-debugger` 联通

验收标准：

- workflow skill 能跑通
- interrupt / resume 正常
- `shader-debug-loop` 能以通用 runtime 方式接入

## Test Plan
### Unit Tests
- `SkillManifestLoader` 正确读取 `SKILL.md` YAML header
- `AgentConfigLoader` 正确读取与继承 `agent.toml`
- `MCPConfigLoader` 正确读取 `mcp.toml`
- `PluginRegistry` 正确发现插件包
- `ConditionEvaluator` 正确处理白名单表达式
- `SqliteCheckpointProvider` 正确保存与恢复
- `SqliteSessionStore` 正确更新状态与并发版本
- `LocalArtifactStore` 正确创建与解析路径
- `SubagentExecutor` 正确装配 prompt / MCP / tools
- `ActivateSkillTool` 正确校验白名单、去重并更新 `active_skills`
- `SkillPromptMiddleware` 正确按 `active_skills` 注入 skill 正文
- `SlashCommandRouter` 正确本地处理 `/status` `/skills` `/agents` `/models` `/resume` `/exit`
- `RunController` 正确处理中断、恢复与状态流转
- `ConversationCompressionService` 仅在 idle 状态运行，并正确回写 checkpoint

### Integration Tests
- REPL 对话可用
- one-shot 对话可用
- prompt skill 可用
- `activate_skill` 可在同一 loop 内激活 prompt skill
- MCP 可用
- 单个 subagent 可用
- slash command 可用
- `Ctrl+C` interrupt 可用
- `/resume` 可恢复 interrupted run
- run 活跃期间新的普通 prompt 会被拒绝
- workflow interrupt/resume 可用
- 全局级 / 项目级覆盖规则正确

### Failure Scenarios
- shell timeout
- 子代理超时
- MCP server 启动失败
- agent 配置继承冲突
- 审批模式切换时 session 记录异常
- workflow checkpoint 存在但定义已变更

## Assumptions
- Phase 0/1 只实现 LangChain 兼容的 Anthropic 模型接入
- `OpenAI-compatible` 暂不在早期实现
- 子代理可与主代理使用不同模型
- MCP 支持是框架能力，`sequential thinking` 不是框架内建特例
- plugin 在 Phase 1 只实现轻量 package loader
- workflow DSL 以承载 `shader-debug-loop` 为目标，不做过度设计
- plugin 在 Phase 1 仅作为**静态资源包**，不支持 Python hook / runtime patch / dynamic loader / event subscription
