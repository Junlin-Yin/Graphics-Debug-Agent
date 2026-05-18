from __future__ import annotations

import importlib.metadata
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol


ViewEventKind = Literal[
    "model_text_delta",
    "model_markdown_final",
    "tool_block",
    "system_message",
    "error_message",
]
SessionCloseStatus = Literal["closed", "cancelled", "failed"]


class ReplView(Protocol):
    def run(self, controller: object) -> int: ...

    def show_welcome(self, snapshot: WelcomeSnapshot) -> None: ...

    def set_input_enabled(self, enabled: bool) -> None: ...

    def append_user_message(self, message: str) -> None: ...

    def append_view_event(self, event: ReplViewEvent) -> None: ...

    def set_turn_status(
        self, turn_id: int, status: str, elapsed_seconds: int
    ) -> None: ...

    def update_status_bar(self, snapshot: StatusBarSnapshot) -> None: ...

    def show_session_closed(self, summary: SessionCloseSummary) -> None: ...

    def show_error(self, message: str) -> None: ...


@dataclass(frozen=True)
class ReplViewEvent:
    kind: ViewEventKind
    payload: dict[str, Any]


@dataclass(frozen=True)
class WelcomeSnapshot:
    tool_name: str
    version: str
    model: str
    workspace_root: str
    approval_mode: str
    session_id_short: str


@dataclass(frozen=True)
class StatusBarSnapshot:
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    approval_mode: str
    model: str


@dataclass(frozen=True)
class SessionCloseSummary:
    session_id: str
    status: SessionCloseStatus
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_type: str | None


@dataclass(frozen=True)
class ToolResultPreview:
    text: str
    truncated: bool
    shown_lines: int
    total_lines: int | None
    artifact_ids: list[str]


def build_welcome_snapshot(
    *,
    config_snapshot: dict[str, Any],
    workspace_root: str,
    approval_mode: str,
    session_id: str,
) -> WelcomeSnapshot:
    return WelcomeSnapshot(
        tool_name="debug-agent",
        version=_package_version(),
        model=str(config_snapshot.get("model") or "unknown"),
        workspace_root=workspace_root,
        approval_mode=approval_mode,
        session_id_short=_welcome_session_label(session_id),
    )


def build_session_close_summary(
    *,
    session_id: str,
    status: SessionCloseStatus,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    error_type: str | None,
) -> SessionCloseSummary:
    return SessionCloseSummary(
        session_id=session_id,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        error_type=error_type,
    )


def format_token_count(value: int | None) -> str:
    if value is None:
        return "unavailable"
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k"


class PromptHistory:
    def __init__(self) -> None:
        self._entries: list[str] = []
        self._navigation_index: int | None = None

    def add(self, entry: str) -> None:
        if not entry.strip():
            return
        self._entries.append(entry)
        self.reset_navigation()

    def previous(self) -> str | None:
        if not self._entries:
            return None
        if self._navigation_index is None:
            self._navigation_index = len(self._entries) - 1
        else:
            self._navigation_index = max(0, self._navigation_index - 1)
        return self._entries[self._navigation_index]

    def next(self) -> str | None:
        if self._navigation_index is None:
            return None
        self._navigation_index += 1
        if self._navigation_index >= len(self._entries):
            self.reset_navigation()
            return None
        return self._entries[self._navigation_index]

    def reset_navigation(self) -> None:
        self._navigation_index = None


class ToolResultPreviewFormatter:
    def format(
        self,
        *,
        output: str | dict[str, Any] | None,
        redacted_output: str | None,
        artifact_ids: list[str],
        max_lines: int = 10,
        max_chars: int = 1000,
    ) -> ToolResultPreview:
        source = _preview_source(output, redacted_output)
        lines = source.splitlines() or [""]
        total_lines = len(lines)
        shown_lines = lines[:max_lines]
        truncated_by_lines = total_lines > len(shown_lines)
        text = "\n".join(shown_lines)
        truncated_by_chars = len(text) > max_chars
        if truncated_by_chars:
            text = text[:max_chars]
            shown_lines = text.splitlines() or [""]
        truncated = truncated_by_lines or truncated_by_chars
        quoted_lines = [f"> {line}" for line in shown_lines]
        if truncated:
            quoted_lines.append("> ...")
            quoted_lines.append(
                f"> [{_truncation_message(len(shown_lines), total_lines, artifact_ids)}]"
            )
        return ToolResultPreview(
            text="\n".join(quoted_lines),
            truncated=truncated,
            shown_lines=len(shown_lines),
            total_lines=total_lines,
            artifact_ids=list(artifact_ids),
        )


def _package_version() -> str:
    try:
        return importlib.metadata.version("debug-agent")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _welcome_session_label(session_id: str) -> str:
    candidate = session_id.removeprefix("sess_")
    unique_segment = candidate.rsplit("-", 1)[-1] if "-" in candidate else candidate
    return f"sess-{unique_segment[:4]}"


def _preview_source(
    output: str | dict[str, Any] | None, redacted_output: str | None
) -> str:
    if redacted_output is not None:
        return redacted_output
    if isinstance(output, dict):
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    if output is None:
        return ""
    return output


def _truncation_message(
    shown_lines: int, total_lines: int, artifact_ids: list[str]
) -> str:
    message = f"truncated: showing {shown_lines} of {total_lines} lines"
    if artifact_ids:
        artifacts = ", ".join(artifact_ids)
        message = f"{message}, full output saved as artifact {artifacts}"
    return message
