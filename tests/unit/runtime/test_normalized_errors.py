import ast
from pathlib import Path

import pytest

from debug_agent.cli.exit_codes import (
    ERROR_STARTUP_PERSISTENCE,
    ERROR_TOOL_CALL,
    map_error_to_exit_code,
)
from debug_agent.runtime.errors import NormalizedError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LEGACY_ERROR_CLASS_VALUES = frozenset(
    {
        "timeout",
        "internal_error",
        "policy_denied",
        "compression_failed",
        "context_limit_exceeded",
    }
)
LEGACY_ERROR_CLASS_SCAN_ROOTS = (
    PROJECT_ROOT / "src" / "debug_agent",
    PROJECT_ROOT / "tests" / "unit",
)
LEGACY_ERROR_CLASSES_CONTRACT_PATH = Path(
    "src/debug_agent/runtime/contracts.py"
)


class _LegacyErrorClassProducerVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.scope_stack: list[str] = []
        self.violations: list[tuple[Path, int, str, str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scoped(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scoped(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scoped(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if (
                _string_literal_value(key) == "error_class"
                and (legacy_value := _string_literal_value(value))
                in LEGACY_ERROR_CLASS_VALUES
            ):
                self._record_if_not_allowed(node.lineno, legacy_value, "dict")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if (
                keyword.arg == "error_class"
                and (legacy_value := _string_literal_value(keyword.value))
                in LEGACY_ERROR_CLASS_VALUES
            ):
                self._record_if_not_allowed(
                    keyword.value.lineno,
                    legacy_value,
                    "keyword",
                )
        self.generic_visit(node)

    def _visit_scoped(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    ) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def _record_if_not_allowed(self, line: int, value: str, kind: str) -> None:
        relative_path = self.path.relative_to(PROJECT_ROOT)
        scope = ".".join(self.scope_stack)
        if _is_allowed_legacy_error_class_location(relative_path, scope):
            return
        self.violations.append((relative_path, line, value, kind))


def _string_literal_value(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_allowed_legacy_error_class_location(relative_path: Path, scope: str) -> bool:
    if relative_path == LEGACY_ERROR_CLASSES_CONTRACT_PATH:
        return True
    normalized_scope = scope.lower()
    return "legacy" in normalized_scope or "compatibility" in normalized_scope


def test_legacy_error_classes_are_not_new_runtime_truth_producers() -> None:
    violations: list[tuple[Path, int, str, str]] = []
    for root in LEGACY_ERROR_CLASS_SCAN_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            visitor = _LegacyErrorClassProducerVisitor(path)
            visitor.visit(tree)
            violations.extend(visitor.violations)

    assert violations == [], "\n".join(
        f"{path}:{line}: legacy error_class {value!r} in {kind} literal"
        for path, line, value, kind in violations
    )


def test_normalized_error_rejects_unknown_class_and_reason() -> None:
    with pytest.raises(ValueError, match="Unknown error_class"):
        NormalizedError.create(
            "bad_class",
            "tool_schema_invalid",
            message="bad",
            scope="tool",
        )

    with pytest.raises(ValueError, match="Unknown reason"):
        NormalizedError.create(
            "tool_error",
            "bad_reason",
            message="bad",
            scope="tool",
        )


def test_tool_schema_invalid_default_and_model_projection() -> None:
    error = NormalizedError.create(
        "tool_error",
        "tool_schema_invalid",
        message="Tool arguments are invalid.",
        scope="tool",
        metadata={"tool_name": "todo", "field": "items"},
    )

    assert error.to_dict()["recoverability"] == "turn_recoverable"
    assert error.to_model_visible() == {
        "error_class": "tool_error",
        "reason": "tool_schema_invalid",
        "message": "Tool arguments are invalid.",
        "artifact_ids": [],
    }


def test_semantic_exit_code_mapping_uses_normalized_reason() -> None:
    schema = NormalizedError.create(
        "config_error",
        "legacy_schema_version",
        message="legacy",
        scope="persistence",
    )
    tool = NormalizedError.create(
        "tool_error",
        "tool_schema_invalid",
        message="bad tool call",
        scope="tool",
    )

    assert map_error_to_exit_code(schema) == ERROR_STARTUP_PERSISTENCE
    assert map_error_to_exit_code(tool) == ERROR_TOOL_CALL


def test_ui_error_registry_matches_trace_and_prompt_input_reasons() -> None:
    prompt = NormalizedError.create(
        "ui_error",
        "prompt_input_failed",
        message="Prompt input timed out.",
        scope="ui",
    )
    trace = NormalizedError.create(
        "ui_error",
        "trace_render_failed",
        message="Trace render failed.",
        scope="ui",
    )

    assert prompt.to_model_visible()["reason"] == "prompt_input_failed"
    assert trace.to_model_visible()["reason"] == "trace_render_failed"
