# Phase 0 Scope

## Goal

Phase 0 交付最小可运行 runtime slice：CLI agent 能完成 REPL 和 one-shot 模型问答，并把 session、run、event、checkpoint、artifact store、日志和 trace 写入本地 `.sessions/`。

Phase 0 的重点是 runtime 真值模型和可恢复记录，不是 agent 能力扩展。

## Must Implement

- CLI:
  - `debug-agent`
  - `debug-agent -p "..."`
  - `debug-agent status <session_id>`
  - `debug-agent trace <session_id>`
- REPL slash commands:
  - `/status`
  - `/exit`
- Persistence:
  - `SessionStore`
  - `RunStore`
  - `EventWriter`
  - `CheckpointStore`
  - `ArtifactStore`
- Runtime:
  - `RuntimeOrchestrator`
  - `PromptAgentExecutor`
  - `ModelFactory`
  - `LangChainAgentLoopAdapter`
  - workspace active session ownership
  - hardcoded default system prompt
- Tooling:
  - minimum `ToolBroker`
  - read-only native tools: `read_file`, `list_dir`, `search_text`, `git_status`
  - standardized `ToolResult`
  - tool audit events
- Observability:
  - `.sessions/runtime.db`
  - per-session `engine.log`
  - per-session `trace.md`
  - status and trace query paths

## Must Not Implement

- skill registry or `activate_skill`
- prompt skill injection
- subagent
- workflow
- MCP
- plugin
- `/compress`
- `/resume`
- `Ctrl+Y` approval mode switching
- writable native tools
- shell execution as a general tool
- same-workspace concurrent active sessions
- hot reload of config, skills, agents, or models

## Minimum Runnable Slice

1. User runs `debug-agent -p "hello"`.
2. Runtime creates a session and prompt run.
3. Runtime writes session/run start events.
4. PromptAgentExecutor calls LangChain through `AgentLoopAdapter`.
5. Final assistant response is printed.
6. Runtime writes run completion event and checkpoint.
7. `debug-agent status <session_id>` shows session and active/latest run state.
8. `debug-agent trace <session_id>` renders a readable trace from events.

REPL uses the same runtime path but keeps a long-lived prompt run until `/exit`.

## Completion Definition

Phase 0 is complete when all of these pass:

- one-shot can complete one model answer and exits with code `0`.
- REPL can complete at least two user turns and exit with `/exit`.
- `.sessions/runtime.db` contains session, run, run event, and checkpoint records for baseline executions.
- Artifact records are written only when an execution produces artifacts, such as large tool output.
- only one active session can exist for a workspace root.
- read-only native tools can only be invoked through ToolBroker.
- `status` shows session id, status, active/latest run, approval mode, latest checkpoint, and updated time.
- `trace` renders session lifecycle, model call, tool audit event, checkpoint, and terminal status.
- no Phase 1+ feature is required for Phase 0 acceptance.

## Approval Mode In Phase 0

Phase 0 supports the mode field because it is part of session contract:

- REPL default: `normal`
- one-shot default: `yolo`

Because Phase 0 has only read-only native tools, approval behavior is minimal. The important rule is that `yolo` still cannot bypass ToolBroker, event logging, or path validation.

## Cancellation And Interruption In Phase 0

Phase 0 does not persist long-lived `interrupted` sessions because `/resume` is not implemented until a later phase.

If the user exits through `/exit`, runtime should complete the active prompt run and release workspace ownership. If the process receives Ctrl+C or a mid-call cancellation is observed, runtime records the terminal state as `failed` with error class `cancelled`, writes an `error` checkpoint when a session exists, and releases workspace ownership.

The `interrupted` state remains part of the long-term v1 design but is not a Phase 0 terminal ownership state.
