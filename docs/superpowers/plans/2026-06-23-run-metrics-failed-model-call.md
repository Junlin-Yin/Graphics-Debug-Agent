# Run Metrics Failed Model Call Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure retry-exhausted or otherwise failed provider-visible model calls contribute deterministic estimated token usage to Phase 4 `run_metrics_*.json`.

**Architecture:** Add metrics-only observation ids and estimated request usage to model-call run events, then have `RunMetricsCollector` correlate started, completed, and failed calls without changing durable conversation ids or provider tool-call ids. Failed calls record estimated input usage with zero output usage; completed calls continue to use existing completed-result usage and clear pending estimates to avoid double counting.

**Tech Stack:** Python, pytest, existing `debug_agent.runtime.usage_accounting`, `debug_agent.observability.run_metrics`, LangChain adapter event recorder, Phase 4 operations commands.

---

## File Structure

- Modify `src/debug_agent/adapters/langchain_adapter.py`
  - Add `model_call_observation_id`, `purpose`, and `estimated_usage` to main model-call events.
  - Keep adapter-local `model_call_id` and tool-call ids unchanged.
- Modify `src/debug_agent/runtime/prompt_executor.py`
  - Add equivalent observation ids and estimated usage for compression model calls.
  - Preserve existing durable conversation model-call ids.
- Modify `src/debug_agent/observability/run_metrics.py`
  - Track pending model-call estimates by `model_call_observation_id`.
  - Record failed calls as estimated token observations.
  - Clear pending estimates on completed calls without double counting.
- Modify `tests/unit/observability/test_run_metrics.py`
  - Add direct collector tests for failed-call estimate recording and completed-call clearing.
- Modify `tests/unit/runtime/test_orchestrator_one_shot.py`
  - Add one-shot timeout/retry-exhaustion metrics assertions.
- Modify `tests/unit/adapters/test_langchain_adapter.py`
  - Add event payload assertions proving observation ids exist while tool ids remain unchanged.
- Optionally modify `tests/unit/runtime/test_prompt_executor.py`
  - Add compression retry failure metrics/event assertion only if compression events need separate coverage after implementation.

## Task 1: Collector Failed Model-Call Accounting

**Files:**
- Modify: `src/debug_agent/observability/run_metrics.py`
- Test: `tests/unit/observability/test_run_metrics.py`

- [ ] **Step 1: Write failing collector tests**

Add tests that exercise event-only accounting without going through the adapter:

```python
def test_run_metrics_records_failed_model_call_from_pending_estimate() -> None:
    collector = RunMetricsCollector(
        session_id="sess_metrics",
        run_id="run_metrics",
        invocation_kind="start",
        started_at=datetime(2026, 6, 16, 9, 10, 0, tzinfo=UTC),
    )

    collector.observe_event(
        kind="model_call_started",
        payload={
            "purpose": "main",
            "model_call_observation_id": "turn-1:main:attempt-1:model_call-1",
            "estimated_usage": {
                "input_tokens": 12,
                "output_tokens": 0,
                "total_tokens": 12,
            },
        },
    )
    collector.observe_event(
        kind="model_call_failed",
        payload={
            "purpose": "main",
            "model_call_observation_id": "turn-1:main:attempt-1:model_call-1",
            "duration_ms": 25,
            "error": {
                "error_class": "model_error",
                "reason": "model_call_timeout",
            },
        },
    )

    payload = collector.build_payload(
        ended_at=datetime(2026, 6, 16, 9, 10, 1, tzinfo=UTC)
    )

    assert payload["tokens"] == {
        "provider_usage_available": False,
        "token_source": "estimated",
        "input_tokens": 12,
        "output_tokens": 0,
        "total_tokens": 12,
        "estimator_version": "char4-v1",
    }
    assert payload["timing"]["llm_time_ms_observed"] == 25
```

Also add a completed-call clearing test:

```python
def test_run_metrics_completed_event_clears_pending_estimate_without_double_counting() -> None:
    collector = RunMetricsCollector(
        session_id="sess_metrics",
        run_id="run_metrics",
        invocation_kind="start",
        started_at=datetime(2026, 6, 16, 9, 10, 0, tzinfo=UTC),
    )

    collector.observe_event(
        kind="model_call_started",
        payload={
            "purpose": "main",
            "model_call_observation_id": "turn-1:main:attempt-1:model_call-1",
            "estimated_usage": {
                "input_tokens": 12,
                "output_tokens": 0,
                "total_tokens": 12,
            },
        },
    )
    collector.observe_event(
        kind="model_call_completed",
        payload={
            "purpose": "main",
            "model_call_observation_id": "turn-1:main:attempt-1:model_call-1",
            "duration_ms": 30,
        },
    )
    collector.record_model_window_usage(
        provider_usage=token_usage_from_mapping(
            {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}
        ),
        estimated_usage=token_usage_from_mapping(
            {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}
        ),
    )

    payload = collector.build_payload(
        ended_at=datetime(2026, 6, 16, 9, 10, 1, tzinfo=UTC)
    )

    assert payload["tokens"]["token_source"] == "provider"
    assert payload["tokens"]["input_tokens"] == 3
    assert payload["tokens"]["output_tokens"] == 5
    assert payload["tokens"]["total_tokens"] == 8
    assert payload["timing"]["llm_time_ms_observed"] == 30
```

- [ ] **Step 2: Run failing collector tests**

Run:

```bash
uv run pytest tests/unit/observability/test_run_metrics.py::test_run_metrics_records_failed_model_call_from_pending_estimate tests/unit/observability/test_run_metrics.py::test_run_metrics_completed_event_clears_pending_estimate_without_double_counting -v
```

Expected: both tests fail because `RunMetricsCollector.observe_event()` ignores `model_call_started` estimates and `model_call_failed` token observations.

- [ ] **Step 3: Implement collector pending estimates**

Update `RunMetricsCollector` with a pending map:

```python
pending_model_call_estimates: dict[str, TokenUsage] = field(default_factory=dict)
```

In `observe_event()`:

```python
if kind == "model_call_started":
    purpose = _model_purpose(payload)
    if purpose not in {"main", "compression"}:
        return
    observation_id = _model_call_observation_id(payload)
    estimated_usage = token_usage_from_mapping(payload.get("estimated_usage"))
    if observation_id is not None and estimated_usage is not None:
        self.pending_model_call_estimates[observation_id] = estimated_usage
    return

if kind == "model_call_completed":
    purpose = _model_purpose(payload)
    if purpose not in {"main", "compression"}:
        return
    observation_id = _model_call_observation_id(payload)
    if observation_id is not None:
        self.pending_model_call_estimates.pop(observation_id, None)
    duration_ms = _duration_payload_ms(payload)
    if duration_ms is not None:
        self.model_call_durations_ms.append(duration_ms)
    return

if kind == "model_call_failed":
    purpose = _model_purpose(payload)
    if purpose not in {"main", "compression"}:
        return
    observation_id = _model_call_observation_id(payload)
    estimated_usage = (
        self.pending_model_call_estimates.pop(observation_id, None)
        if observation_id is not None
        else None
    )
    if estimated_usage is not None:
        self.model_calls.append(
            ModelCallTokenObservation(
                provider_usage=None,
                estimated_usage=estimated_usage,
            )
        )
    duration_ms = _duration_payload_ms(payload)
    if duration_ms is not None:
        self.model_call_durations_ms.append(duration_ms)
    return
```

Add helper:

```python
def _model_call_observation_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("model_call_observation_id")
    if isinstance(value, str) and value:
        return value
    return None
```

- [ ] **Step 4: Run collector tests**

Run:

```bash
uv run pytest tests/unit/observability/test_run_metrics.py -v
```

Expected: all `test_run_metrics.py` tests pass.

- [ ] **Step 5: Commit collector change**

Run:

```bash
git add src/debug_agent/observability/run_metrics.py tests/unit/observability/test_run_metrics.py
git commit -m "Fix failed model call metrics accounting"
```

## Task 2: Main Model Event Observation Ids

**Files:**
- Modify: `src/debug_agent/adapters/langchain_adapter.py`
- Test: `tests/unit/adapters/test_langchain_adapter.py`

- [ ] **Step 1: Write failing adapter event tests**

Extend the existing timeout/failure event test to assert started and failed payloads include the same metrics-only observation id and estimated usage:

```python
def test_langchain_adapter_failed_model_call_events_include_metrics_observation_id() -> None:
    events: list[tuple[str, dict]] = []
    adapter = LangChainAgentLoopAdapter(model=FakeChatModel(timeout=True))

    result = adapter.run(
        AgentRunRequest(
            session_id="sess",
            run_id="run",
            model_config={"provider": "fake", "model": "fake-model"},
            model_context_frame=None,
            user_input="hello",
        ),
        RunContext(
            workspace_root=".",
            artifact_root=".",
            approval_mode="normal",
            model_event_recorder=lambda kind, payload: events.append((kind, payload)),
        ),
    )

    assert result.status == "timeout"
    started = [payload for kind, payload in events if kind == "model_call_started"][0]
    failed = [payload for kind, payload in events if kind == "model_call_failed"][0]
    assert started["purpose"] == "main"
    assert failed["purpose"] == "main"
    assert started["model_call_observation_id"]
    assert failed["model_call_observation_id"] == started["model_call_observation_id"]
    assert started["estimated_usage"]["input_tokens"] > 0
    assert started["estimated_usage"]["output_tokens"] == 0
    assert started["estimated_usage"]["total_tokens"] == started["estimated_usage"]["input_tokens"]
```

Add or extend a tool-loop test to assert provider tool ids are unchanged:

```python
assert tool_result["tool_call_id"] == "model_call_1_tool_1"
```

- [ ] **Step 2: Run failing adapter tests**

Run:

```bash
uv run pytest tests/unit/adapters/test_langchain_adapter.py::test_langchain_adapter_failed_model_call_events_include_metrics_observation_id -v
```

Expected: fails because event payloads lack `model_call_observation_id` and `estimated_usage`.

- [ ] **Step 3: Implement main model event fields**

In `LangChainAgentLoopAdapter.run()` and `stream()`, keep existing `model_call_id = f"model_call_{model_call_index + 1}"`.

Before invoking the provider, compute:

```python
provider_messages = list(messages)
model_call_observation_id = _model_call_observation_id(
    request=request,
    purpose="main",
    model_call_id=model_call_id,
    attempt_number=_retry_attempt_number_from_request(request),
)
started_estimated_usage = estimate_model_call_usage(
    provider_messages=provider_messages,
    accepted_output={"content": "", "tool_calls": []},
)
```

Add helper functions:

```python
def _model_call_observation_id(
    *,
    request: AgentRunRequest,
    purpose: str,
    model_call_id: str,
    attempt_number: int | None = None,
) -> str:
    turn_id = _request_turn_id(request) or "turn-unknown"
    attempt = attempt_number if attempt_number is not None else 1
    return f"{turn_id}:{purpose}:attempt-{attempt}:{model_call_id.replace('_', '-')}"

def _request_turn_id(request: AgentRunRequest) -> str | None:
    if request.model_context_frame is None:
        return None
    for segment in request.model_context_frame.ordered_message_segments():
        if isinstance(segment.turn_id, str) and segment.turn_id:
            return segment.turn_id
    return None

def _retry_attempt_number_from_request(request: AgentRunRequest) -> int | None:
    return None
```

If the existing retry loop cannot provide attempt number to the adapter without broader changes, rely on fresh event ordering plus unique ids derived from a monotonic per-adapter call counter. The id must be unique for each provider-visible attempt in the invocation.

Pass `model_call_observation_id` and `started_estimated_usage` into `_invoke_with_timeout()` and `_stream_model_call()`.

Emit `model_call_started` with:

```python
{
    "provider": request.model_config.get("provider"),
    "model": request.model_config.get("model"),
    "purpose": "main",
    "turn_id": _request_turn_id(request),
    "model_call_observation_id": model_call_observation_id,
    "estimated_usage": started_estimated_usage,
}
```

Emit `model_call_completed` and `_record_model_failure()` with the same `model_call_observation_id`, `purpose`, and `turn_id`. Do not change `_normalized_tool_calls()` or tool-call ids.

- [ ] **Step 4: Run adapter tests**

Run:

```bash
uv run pytest tests/unit/adapters/test_langchain_adapter.py -v
```

Expected: all adapter tests pass; existing tool-call id assertions remain valid.

- [ ] **Step 5: Commit adapter change**

Run:

```bash
git add src/debug_agent/adapters/langchain_adapter.py tests/unit/adapters/test_langchain_adapter.py
git commit -m "Add metrics observation ids to model events"
```

## Task 3: One-Shot Retry-Exhaustion Metrics Integration

**Files:**
- Modify: `tests/unit/runtime/test_orchestrator_one_shot.py`
- Modify if needed: `src/debug_agent/runtime/prompt_executor.py`

- [ ] **Step 1: Write failing one-shot metrics regression test**

Extend `test_one_shot_model_timeout_marks_failed_and_releases_ownership` or add a sibling test:

```python
def test_one_shot_retry_exhausted_timeout_metrics_use_estimated_tokens(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _config()
    config["fake_timeout"] = True

    result = RuntimeOrchestrator(workspace_root=workspace).run_one_shot("hello", config)

    assert result.exit_code == 1
    metrics_paths = list(
        (workspace / ".sessions" / result.session_id / "logs").glob(
            "run_metrics_*.json"
        )
    )
    assert len(metrics_paths) == 1
    payload = json.loads(metrics_paths[0].read_text(encoding="utf-8"))
    tokens = payload["tokens"]
    assert tokens["provider_usage_available"] is False
    assert tokens["token_source"] == "estimated"
    assert tokens["input_tokens"] > 0
    assert tokens["output_tokens"] == 0
    assert tokens["total_tokens"] == tokens["input_tokens"]
    assert tokens["estimator_version"] == "char4-v1"
```

- [ ] **Step 2: Run failing one-shot test**

Run:

```bash
uv run pytest tests/unit/runtime/test_orchestrator_one_shot.py::test_one_shot_retry_exhausted_timeout_metrics_use_estimated_tokens -v
```

Expected before Task 1 and Task 2 are fully integrated: fails with zero provider tokens.

- [ ] **Step 3: Fix integration gaps only if the test still fails**

If metrics still show zero after Tasks 1 and 2, inspect whether `_MetricsEventWriter` sees `model_call_started` and `model_call_failed` payloads. If missing, update `_append_model_event()` paths in `src/debug_agent/runtime/prompt_executor.py` to preserve the new event payload fields unchanged when normalizing model failures.

Do not change durable conversation append logic.

- [ ] **Step 4: Run targeted one-shot tests**

Run:

```bash
uv run pytest tests/unit/runtime/test_orchestrator_one_shot.py::test_one_shot_retry_exhausted_timeout_metrics_use_estimated_tokens tests/unit/runtime/test_orchestrator_one_shot.py::test_one_shot_model_timeout_marks_failed_and_releases_ownership -v
```

Expected: both pass.

- [ ] **Step 5: Commit integration regression**

Run:

```bash
git add src/debug_agent/runtime/prompt_executor.py tests/unit/runtime/test_orchestrator_one_shot.py
git commit -m "Cover failed model call run metrics"
```

## Task 4: Final Verification

**Files:**
- No production edits expected.
- May update tests only if a narrow missing assertion is found.

- [ ] **Step 1: Run Phase 4 targeted verification**

Run:

```bash
uv run pytest tests/unit/observability/test_run_metrics.py tests/unit/adapters/test_langchain_adapter.py tests/unit/runtime/test_orchestrator_one_shot.py -v
```

Expected: all selected tests pass.

- [ ] **Step 2: Run broader unit verification**

Run:

```bash
uv run pytest tests/unit -v
```

Expected: all unit tests pass.

- [ ] **Step 3: Inspect git diff for boundary violations**

Run:

```bash
git diff -- src/debug_agent/adapters/langchain_adapter.py src/debug_agent/runtime/prompt_executor.py src/debug_agent/observability/run_metrics.py
```

Expected: diff only adds metrics event fields/accounting and tests. It must not alter durable conversation `model_call_id`, provider tool-call ids, checkpoint schema, or resume projection.

- [ ] **Step 4: Commit any final verification-only adjustment**

If no files changed during verification, skip this step. If tests needed a small assertion fix, run:

```bash
git add <changed-files>
git commit -m "Stabilize failed model metrics tests"
```

## Self-Review

- Spec coverage: The plan covers failed-call estimates, retry accumulation, completed-call no-double-counting, and durable/tool-loop boundaries.
- Placeholder scan: No unfinished placeholder language remains.
- Type consistency: The plan uses existing `TokenUsage`, `ModelCallTokenObservation`, `estimate_model_call_usage`, and `token_usage_from_mapping` APIs.
- Scope check: The plan touches only metrics/event payloads and tests. It does not modify SQLite truth, checkpoint shape, resume, or provider tool-call ids.
