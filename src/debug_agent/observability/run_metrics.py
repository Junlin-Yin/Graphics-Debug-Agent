from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from debug_agent.runtime.contracts import TOOL_RESULT_STATUSES
from debug_agent.runtime.usage_accounting import (
    ModelCallTokenObservation,
    TokenUsage,
    summarize_model_call_window,
    token_usage_from_mapping,
)


METRICS_SCHEMA_VERSION = 1
METRICS_INVOCATION_KINDS = frozenset({"start", "resume"})
FAILED_TOOL_STATUSES = frozenset({"error", "timeout", "denied", "cancelled"})


class RunMetricsWriteError(Exception):
    pass


@dataclass(frozen=True)
class ToolCallObservation:
    status: str
    duration_ms: int | None

    def __post_init__(self) -> None:
        if self.status not in TOOL_RESULT_STATUSES:
            raise ValueError(f"Unsupported tool result status for metrics: {self.status}")


@dataclass
class RunMetricsCollector:
    session_id: str
    run_id: str
    invocation_kind: str
    started_at: datetime
    model_calls: list[ModelCallTokenObservation] = field(default_factory=list)
    model_call_durations_ms: list[int] = field(default_factory=list)
    tool_calls: list[ToolCallObservation] = field(default_factory=list)
    _pending_model_calls: dict[str, ModelCallTokenObservation] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if self.invocation_kind not in METRICS_INVOCATION_KINDS:
            raise ValueError(
                f"Unsupported metrics invocation kind: {self.invocation_kind}"
            )
        self.started_at = _utc_datetime(self.started_at)

    def record_model_call(
        self,
        *,
        purpose: str,
        duration_ms: int | None,
        provider_usage: TokenUsage | None,
        estimated_usage: TokenUsage,
    ) -> None:
        if purpose not in {"main", "compression"}:
            return
        self.model_calls.append(
            ModelCallTokenObservation(
                provider_usage=provider_usage,
                estimated_usage=estimated_usage,
            )
        )
        if isinstance(duration_ms, int):
            self.model_call_durations_ms.append(max(0, duration_ms))

    def record_model_window_usage(
        self,
        *,
        provider_usage: TokenUsage | None,
        estimated_usage: TokenUsage | None,
    ) -> None:
        if estimated_usage is None:
            estimated_usage = provider_usage
        if estimated_usage is None:
            return
        self.model_calls.append(
            ModelCallTokenObservation(
                provider_usage=provider_usage,
                estimated_usage=estimated_usage,
            )
        )

    def record_tool_call(self, *, status: str, duration_ms: int | None) -> None:
        self.tool_calls.append(ToolCallObservation(status=status, duration_ms=duration_ms))

    def observe_event(self, *, kind: str, payload: dict[str, Any]) -> None:
        if kind == "model_call_started":
            purpose = _model_purpose(payload)
            if purpose not in {"main", "compression"}:
                return
            observation_id = payload.get("model_call_observation_id")
            if not isinstance(observation_id, str) or not observation_id:
                return
            estimated_usage = token_usage_from_mapping(payload.get("estimated_usage"))
            if estimated_usage is None:
                return
            self._pending_model_calls[observation_id] = ModelCallTokenObservation(
                provider_usage=None,
                estimated_usage=estimated_usage,
            )
            return
        if kind == "model_call_completed":
            purpose = _model_purpose(payload)
            if purpose not in {"main", "compression"}:
                return
            _pop_pending_model_call(self._pending_model_calls, payload)
            duration_ms = _duration_payload_ms(payload)
            if duration_ms is not None:
                self.model_call_durations_ms.append(duration_ms)
            return
        if kind == "model_call_failed":
            purpose = _model_purpose(payload)
            if purpose not in {"main", "compression"}:
                return
            observation = _pop_pending_model_call(self._pending_model_calls, payload)
            if observation is None:
                estimated_usage = token_usage_from_mapping(payload.get("estimated_usage"))
                if estimated_usage is not None:
                    observation = ModelCallTokenObservation(
                        provider_usage=None,
                        estimated_usage=estimated_usage,
                    )
            if observation is not None:
                self.model_calls.append(observation)
            duration_ms = _duration_payload_ms(payload)
            if duration_ms is not None:
                self.model_call_durations_ms.append(duration_ms)
            return
        if kind not in {"tool_call_completed", "tool_call_failed", "tool_call_denied"}:
            return
        status = payload.get("status")
        if not isinstance(status, str) or status not in TOOL_RESULT_STATUSES:
            return
        self.record_tool_call(status=status, duration_ms=_duration_payload_ms(payload))

    def observe_agent_result(self, result: object) -> None:
        usage = token_usage_from_mapping(getattr(result, "usage", None))
        metadata = getattr(result, "metadata", None)
        estimated_usage = (
            token_usage_from_mapping(metadata.get("estimated_usage"))
            if isinstance(metadata, dict)
            else None
        )
        self.record_model_window_usage(
            provider_usage=usage,
            estimated_usage=estimated_usage,
        )

    def build_payload(self, *, ended_at: datetime) -> dict[str, Any]:
        ended_at = _utc_datetime(ended_at)
        token_summary = summarize_model_call_window(self.model_calls)
        usage = token_summary.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        return {
            "schema_version": METRICS_SCHEMA_VERSION,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "metrics_started_at": _isoformat_ms(self.started_at),
            "metrics_ended_at": _isoformat_ms(ended_at),
            "invocation_kind": self.invocation_kind,
            "timing": {
                "wall_time_ms": _duration_ms(self.started_at, ended_at),
                "llm_time_ms_observed": sum(self.model_call_durations_ms),
                "tool_time_ms_observed": sum(
                    call.duration_ms
                    for call in self.tool_calls
                    if isinstance(call.duration_ms, int)
                ),
                "tool_time_coverage": {
                    "timed_tool_calls": sum(
                        1
                        for call in self.tool_calls
                        if isinstance(call.duration_ms, int)
                    ),
                    "total_tool_calls": len(self.tool_calls),
                },
            },
            "tokens": {
                "provider_usage_available": bool(
                    token_summary.get("provider_usage_available")
                ),
                "token_source": str(token_summary.get("token_source") or "provider"),
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "total_tokens": int(usage.get("total_tokens", 0)),
                "estimator_version": token_summary.get("estimator_version"),
            },
            "tools": _tool_summary(self.tool_calls),
        }


def write_run_metrics(
    logs_dir: Path,
    collector: RunMetricsCollector,
    *,
    ended_at: datetime,
    timestamp: datetime | None = None,
) -> Path:
    temp_path: Path | None = None
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _utc_datetime(timestamp or ended_at)
        target = _next_metrics_path(logs_dir, timestamp=timestamp)
        payload = collector.build_payload(ended_at=ended_at)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=logs_dir,
            prefix=f".{target.stem}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temp_path, target)
        return target
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise RunMetricsWriteError(str(exc)) from exc


def _next_metrics_path(logs_dir: Path, *, timestamp: datetime) -> Path:
    stamp = _filename_timestamp(timestamp)
    first = logs_dir / f"run_metrics_{stamp}.json"
    if not first.exists():
        return first
    suffix = 1
    while True:
        candidate = logs_dir / f"run_metrics_{stamp}_{suffix}.json"
        if not candidate.exists():
            return candidate
        suffix += 1


def _tool_summary(tool_calls: list[ToolCallObservation]) -> dict[str, Any]:
    total = len(tool_calls)
    successful = sum(1 for call in tool_calls if call.status == "ok")
    failures = {status: 0 for status in sorted(FAILED_TOOL_STATUSES)}
    for call in tool_calls:
        if call.status in failures:
            failures[call.status] += 1
    failed = sum(failures.values())
    return {
        "total_tool_calls": total,
        "successful_tool_calls": successful,
        "failed_tool_calls": failed,
        "tool_success_rate": 0.0 if total == 0 else round(successful / total, 4),
        "tool_failure_rate": 0.0 if total == 0 else round(failed / total, 4),
        "failure_breakdown": {
            "error": failures["error"],
            "timeout": failures["timeout"],
            "denied": failures["denied"],
            "cancelled": failures["cancelled"],
        },
    }


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _duration_ms(started_at: datetime, ended_at: datetime) -> int:
    return max(0, round((ended_at - started_at).total_seconds() * 1000))


def _isoformat_ms(value: datetime) -> str:
    value = _utc_datetime(value)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _filename_timestamp(value: datetime) -> str:
    value = _utc_datetime(value)
    return value.strftime("%Y%m%dT%H%M%S.") + f"{value.microsecond // 1000:03d}Z"


def _model_purpose(payload: dict[str, Any]) -> str:
    purpose = payload.get("purpose")
    if isinstance(purpose, str) and purpose:
        return purpose
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("purpose"), str):
        return metadata["purpose"]
    return "main"


def _duration_payload_ms(payload: dict[str, Any]) -> int | None:
    for key in ("execution_duration_ms", "duration_ms"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
    duration = payload.get("duration")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool):
        return max(0, round(float(duration) * 1000))
    return None


def _pop_pending_model_call(
    pending: dict[str, ModelCallTokenObservation],
    payload: dict[str, Any],
) -> ModelCallTokenObservation | None:
    observation_id = payload.get("model_call_observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        return None
    return pending.pop(observation_id, None)
