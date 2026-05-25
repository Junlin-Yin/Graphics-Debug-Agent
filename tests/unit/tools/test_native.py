from __future__ import annotations

from debug_agent.tools.native import tool_definitions
from debug_agent.tools.shell import tool_definitions as shell_tool_definitions


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
    assert definitions["search_text"].input_schema["required"] == ["path", "query"]
    assert definitions["write_file"].input_schema["required"] == ["path", "content"]
    assert definitions["edit_file"].input_schema["required"] == [
        "path",
        "old_text",
        "new_text",
    ]
    for name in ("read_file", "list_dir", "search_text"):
        assert definitions[name].input_schema["properties"]["limit"] == {
            "type": "integer",
            "minimum": 1,
        }


def test_shell_exec_definition_is_not_a_native_tool() -> None:
    native_definitions = {definition.name for definition in tool_definitions()}
    shell_definitions = {definition.name: definition for definition in shell_tool_definitions()}

    assert "shell_exec" not in native_definitions
    assert set(shell_definitions) == {"shell_exec"}
    assert shell_definitions["shell_exec"].category == "shell"
