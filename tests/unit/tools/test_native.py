from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

from debug_agent.tools import native
from debug_agent.tools.native import tool_definitions
from debug_agent.tools.shell import tool_definitions as shell_tool_definitions


class _TrackingWriteLockContext:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._registry_lock = threading.Lock()

    @contextmanager
    def write_lock_for_path(self, path):
        canonical = str(Path(path).resolve())
        with self._registry_lock:
            lock = self._locks.setdefault(canonical, threading.RLock())
        with lock:
            yield


def test_native_tool_definitions_are_phase1_metadata() -> None:
    definitions = {definition.name: definition for definition in tool_definitions()}

    assert set(definitions) == {
        "read_file",
        "list_dir",
        "search_text",
        "write_file",
        "edit_file",
    }
    assert "git_status" not in definitions
    for definition in definitions.values():
        assert definition.category == "native"
        assert definition.risk_level in {"read", "write"}
        assert definition.access in (["read"], ["write"])
        assert definition.input_schema["additionalProperties"] is False


def test_native_tool_schemas_require_fields_and_positive_limits() -> None:
    definitions = {definition.name: definition for definition in tool_definitions()}

    assert definitions["read_file"].input_schema["required"] == ["path"]
    assert definitions["list_dir"].input_schema["required"] == ["path"]
    assert definitions["search_text"].input_schema["required"] == ["pattern"]
    assert definitions["write_file"].input_schema["required"] == ["path", "content"]
    assert definitions["edit_file"].input_schema["required"] == [
        "path",
        "old_text",
        "new_text",
    ]
    for name in ("read_file", "list_dir"):
        assert definitions[name].input_schema["properties"]["limit"] == {
            "type": "integer",
            "minimum": 1,
        }
    search_schema = definitions["search_text"].input_schema["properties"]
    assert "query" not in search_schema
    assert search_schema["pattern"] == {"type": "string"}
    assert search_schema["path"] == {"type": "string"}
    assert search_schema["output_mode"] == {
        "type": "string",
        "enum": ["content", "files_with_matches", "count"],
        "default": "content",
    }
    assert search_schema["maxResults"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000,
        "default": 100,
    }
    assert search_schema["offset"] == {"type": "integer", "minimum": 0, "default": 0}
    assert search_schema["case_sensitive"] == {"type": "boolean", "default": True}
    assert search_schema["fixed_strings"] == {"type": "boolean", "default": False}
    assert search_schema["include_hidden"] == {"type": "boolean", "default": False}
    assert search_schema["before_context"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 10,
        "default": 0,
    }
    assert search_schema["after_context"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 10,
        "default": 0,
    }
    assert search_schema["context"] == {"type": "integer", "minimum": 0, "maximum": 10}


def test_shell_exec_definition_is_not_a_native_tool() -> None:
    native_definitions = {definition.name for definition in tool_definitions()}
    shell_definitions = {definition.name: definition for definition in shell_tool_definitions()}

    assert "shell_exec" not in native_definitions
    assert set(shell_definitions) == {"shell_exec"}
    assert shell_definitions["shell_exec"].category == "shell"


def test_write_file_uses_context_write_lock_for_same_canonical_path(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "locked.txt"
    context = _TrackingWriteLockContext()
    active = 0
    max_active = 0
    calls = 0
    first_entered = threading.Event()
    release_first = threading.Event()
    state_lock = threading.Lock()
    original_write_text = Path.write_text

    def tracking_write_text(self, *args, **kwargs):
        nonlocal active, calls, max_active
        if self.resolve() != target.resolve():
            return original_write_text(self, *args, **kwargs)
        with state_lock:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        if call_number == 1:
            first_entered.set()
            release_first.wait(timeout=1)
        try:
            time.sleep(0.01)
            return original_write_text(self, *args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(Path, "write_text", tracking_write_text)

    first = threading.Thread(
        target=native.write_file,
        args=(context, {"path": str(target), "content": "one"}),
    )
    second = threading.Thread(
        target=native.write_file,
        args=(context, {"path": str(target), "content": "two"}),
    )
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    time.sleep(0.02)
    assert max_active == 1
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert calls == 2
    assert max_active == 1


def test_edit_file_uses_context_write_lock_for_same_canonical_path(
    tmp_path, monkeypatch
) -> None:
    target = tmp_path / "locked.txt"
    target.write_text("old old", encoding="utf-8")
    context = _TrackingWriteLockContext()
    active = 0
    max_active = 0
    calls = 0
    first_entered = threading.Event()
    release_first = threading.Event()
    state_lock = threading.Lock()
    original_write_text = Path.write_text

    def tracking_write_text(self, *args, **kwargs):
        nonlocal active, calls, max_active
        if self.resolve() != target.resolve():
            return original_write_text(self, *args, **kwargs)
        with state_lock:
            calls += 1
            call_number = calls
            active += 1
            max_active = max(max_active, active)
        if call_number == 1:
            first_entered.set()
            release_first.wait(timeout=1)
        try:
            time.sleep(0.01)
            return original_write_text(self, *args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(Path, "write_text", tracking_write_text)

    first = threading.Thread(
        target=native.edit_file,
        args=(context, {"path": str(target), "old_text": "old", "new_text": "new"}),
    )
    second = threading.Thread(
        target=native.edit_file,
        args=(context, {"path": str(target), "old_text": "old", "new_text": "new"}),
    )
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    time.sleep(0.02)
    assert max_active == 1
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert calls == 2
    assert max_active == 1
