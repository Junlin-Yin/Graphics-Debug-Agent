# Phase 0 Test Plan

## Unit Tests

- `SessionStore` creates session rows.
- `SessionStore` rejects a second active session for the same workspace root.
- `SessionStore` releases ownership when session becomes `completed` or `failed`.
- `RunStore` creates prompt runs.
- `RunStore` allows Phase 0 transitions `running -> completed` and `running -> failed`.
- `RunStore` stores `context_snapshot_id` as `NULL` in Phase 0.
- `EventWriter` appends events and exposes no update/delete runtime path.
- `CheckpointStore` saves and loads checkpoint state.
- `CheckpointStore` accepts Phase 0 checkpoint kinds `turn`, `terminal`, and `error`.
- `ArtifactStore` creates session roots, writes text artifacts, registers existing files, resolves artifact ids.
- `ToolBroker` denies unknown tools.
- `ToolBroker` denies paths outside workspace root.
- `ToolBroker` denies write intent in Phase 0.
- `ToolBroker` returns standardized `ToolResult`.
- `ToolBroker` writes audit events.
- `read_file`, `list_dir`, `search_text`, `git_status` work through ToolBroker.
- `ModelFactory` maps config snapshot to a model instance or clear `config_error`.
- `LangChainAgentLoopAdapter` maps model success, model failure, timeout, and cancellation to `AgentRunResult`.
- `PromptAgentExecutor` writes model events and final checkpoint using a fake model.
- `TraceWriter` renders trace from persisted run events.
- `TraceWriter` refreshes trace on terminal session state and on explicit trace command when missing or stale.

## Integration Tests

- `debug-agent -p "hello"` completes with fake model.
- one-shot writes session, run, run events, checkpoint, artifact root, and engine log.
- `debug-agent` REPL accepts two fake-model turns and exits with `/exit`.
- REPL `/status` prints current session status without calling model.
- `debug-agent status <session_id>` works after one-shot exits.
- `debug-agent trace <session_id>` refreshes `trace.md` if missing or stale, then prints the trace path plus a short summary.
- active workspace ownership conflict returns exit code `3`.
- read-only native tool invocation is visible in run events and trace.
- model failure marks run/session failed and records `model_error`.
- config failure exits with code `4`.
- config failure records `config_error` when a session exists.
- Ctrl+C or mid-call cancellation records `failed` with error class `cancelled` and releases workspace ownership.
- `engine.log` is JSON Lines.

## Failure Scenarios

- missing session id for `status`
- missing session id for `trace`
- active workspace conflict
- invalid config
- model provider failure
- model timeout
- ToolBroker denied path traversal
- ToolBroker unknown tool
- artifact path missing during trace rendering
- SQLite unavailable or migration failure

## Fake Model Testing

Fake model must support:

- deterministic assistant text
- deterministic tool call request if adapter path needs tool exercise
- forced model error
- forced timeout

Tests should not require network access.

## Fake Tool Testing

Fake tool or fixture workspace must cover:

- file read success
- directory listing success
- text search success and no-match
- git status success in a temporary git repository
- denied path outside workspace
- output large enough to become artifact

## SQLite Verification

Acceptance tests must assert rows exist in:

- `sessions`
- `runs`
- `run_events`
- `checkpoints`
- `artifacts`

They must also assert `run_events` order is chronological by timestamp or insertion order.

## Smoke Commands

```bash
pytest tests/unit -v
pytest tests/integration -v
debug-agent -p "hello"
debug-agent status <session_id>
debug-agent trace <session_id>
```

REPL smoke test:

```text
debug-agent
> hello
> /status
> tell me one more thing
> /exit
```

## Phase 0 Acceptance

Phase 0 is accepted only if all smoke commands pass with fake model configuration and no Phase 1+ feature is required.
