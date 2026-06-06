import pytest

from debug_agent.cli.exit_codes import (
    ERROR_STARTUP_PERSISTENCE,
    ERROR_TOOL_CALL,
    map_error_to_exit_code,
)
from debug_agent.runtime.errors import NormalizedError


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
