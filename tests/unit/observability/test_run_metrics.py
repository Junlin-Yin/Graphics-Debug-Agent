from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from debug_agent.observability.run_metrics import (
    RunMetricsCollector,
    RunMetricsWriteError,
    write_run_metrics,
)
from debug_agent.runtime.usage_accounting import token_usage_from_mapping


def _collector() -> RunMetricsCollector:
    collector = RunMetricsCollector(
        session_id="sess_metrics",
        run_id="run_metrics",
        invocation_kind="start",
        started_at=datetime(2026, 6, 16, 9, 10, 0, 123000, tzinfo=UTC),
    )
    provider = token_usage_from_mapping(
        {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}
    )
    estimate = token_usage_from_mapping(
        {"input_tokens": 7, "output_tokens": 11, "total_tokens": 18}
    )
    assert provider is not None
    assert estimate is not None
    collector.record_model_call(
        purpose="main",
        duration_ms=1200,
        provider_usage=provider,
        estimated_usage=estimate,
    )
    collector.record_tool_call(status="ok", duration_ms=25)
    collector.record_tool_call(status="error", duration_ms=None)
    return collector


def test_run_metrics_writer_emits_schema_one_json_with_required_sections(tmp_path) -> None:
    collector = _collector()

    path = write_run_metrics(
        tmp_path,
        collector,
        ended_at=datetime(2026, 6, 16, 9, 15, 30, 456000, tzinfo=UTC),
        timestamp=datetime(2026, 6, 16, 9, 15, 30, 456000, tzinfo=UTC),
    )

    assert path.name == "run_metrics_20260616T091530.456Z.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "schema_version": 1,
        "session_id": "sess_metrics",
        "run_id": "run_metrics",
        "metrics_started_at": "2026-06-16T09:10:00.123Z",
        "metrics_ended_at": "2026-06-16T09:15:30.456Z",
        "invocation_kind": "start",
        "timing": {
            "wall_time_ms": 330333,
            "llm_time_ms_observed": 1200,
            "tool_time_ms_observed": 25,
            "tool_time_coverage": {
                "timed_tool_calls": 1,
                "total_tool_calls": 2,
            },
        },
        "tokens": {
            "provider_usage_available": True,
            "token_source": "provider",
            "input_tokens": 3,
            "output_tokens": 5,
            "total_tokens": 8,
            "estimator_version": None,
        },
        "tools": {
            "total_tool_calls": 2,
            "successful_tool_calls": 1,
            "failed_tool_calls": 1,
            "tool_success_rate": 0.5,
            "tool_failure_rate": 0.5,
            "failure_breakdown": {
                "error": 1,
                "timeout": 0,
                "denied": 0,
                "cancelled": 0,
            },
        },
    }
    assert "estimated_context_tokens" not in payload
    assert "reasoning_tokens" not in payload["tokens"]
    assert "thinking_tokens" not in payload["tokens"]


def test_run_metrics_writer_uses_deterministic_collision_suffixes(tmp_path) -> None:
    collector = _collector()
    timestamp = datetime(2026, 6, 16, 9, 15, 30, 456000, tzinfo=UTC)
    ended_at = timestamp + timedelta(seconds=1)

    first = write_run_metrics(tmp_path, collector, ended_at=ended_at, timestamp=timestamp)
    second = write_run_metrics(tmp_path, collector, ended_at=ended_at, timestamp=timestamp)
    third = write_run_metrics(tmp_path, collector, ended_at=ended_at, timestamp=timestamp)

    assert first.name == "run_metrics_20260616T091530.456Z.json"
    assert second.name == "run_metrics_20260616T091530.456Z_1.json"
    assert third.name == "run_metrics_20260616T091530.456Z_2.json"
    assert first.read_text(encoding="utf-8")
    assert second.read_text(encoding="utf-8")
    assert third.read_text(encoding="utf-8")


def test_run_metrics_writer_raises_without_partial_file_on_atomic_failure(
    tmp_path, monkeypatch
) -> None:
    collector = _collector()

    def fail_replace(_source, _target):
        raise OSError("disk full")

    monkeypatch.setattr("debug_agent.observability.run_metrics.os.replace", fail_replace)

    with pytest.raises(RunMetricsWriteError):
        write_run_metrics(
            tmp_path,
            collector,
            ended_at=datetime(2026, 6, 16, 9, 15, 30, tzinfo=UTC),
            timestamp=datetime(2026, 6, 16, 9, 15, 30, tzinfo=UTC),
        )

    assert not list(tmp_path.glob("run_metrics_*.tmp"))
    assert not list(tmp_path.glob("run_metrics_*.json"))


def test_run_metrics_writer_converts_mkdir_failure(tmp_path, monkeypatch) -> None:
    collector = _collector()

    def fail_mkdir(*_args, **_kwargs):
        raise OSError("mkdir denied")

    monkeypatch.setattr("debug_agent.observability.run_metrics.Path.mkdir", fail_mkdir)

    with pytest.raises(RunMetricsWriteError, match="mkdir denied"):
        write_run_metrics(
            tmp_path,
            collector,
            ended_at=datetime(2026, 6, 16, 9, 15, 30, tzinfo=UTC),
            timestamp=datetime(2026, 6, 16, 9, 15, 30, tzinfo=UTC),
        )

    assert not list(tmp_path.glob("run_metrics_*.json"))


def test_run_metrics_observes_view_image_as_brokered_tool_call() -> None:
    collector = RunMetricsCollector(
        session_id="sess_metrics",
        run_id="run_metrics",
        invocation_kind="start",
        started_at=datetime(2026, 6, 16, 9, 10, 0, tzinfo=UTC),
    )

    collector.observe_event(
        kind="tool_call_completed",
        payload={
            "tool_name": "view_image",
            "status": "ok",
            "duration_ms": 42,
            "vision_provider": "openai",
            "vision_model": "kimi-k2.5",
        },
    )
    payload = collector.build_payload(
        ended_at=datetime(2026, 6, 16, 9, 10, 1, tzinfo=UTC)
    )

    assert payload["timing"]["tool_time_ms_observed"] == 42
    assert payload["timing"]["tool_time_coverage"] == {
        "timed_tool_calls": 1,
        "total_tool_calls": 1,
    }
    assert payload["tools"]["total_tool_calls"] == 1
    assert payload["tools"]["successful_tool_calls"] == 1
