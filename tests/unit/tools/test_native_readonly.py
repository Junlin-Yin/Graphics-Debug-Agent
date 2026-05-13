from __future__ import annotations

import subprocess

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.tools.broker import ToolBroker
from debug_agent.tools.native_readonly import tool_definitions


def _broker(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    session = SessionStore(db.connection).create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    run = RunStore(db.connection).create_prompt_run(session.session_id, run_id="run_1")
    broker = ToolBroker(
        event_writer=EventWriter(db.connection, db.path.parent),
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    )
    return workspace, db, broker, session, run


def _invoke(workspace, broker, session, run, tool_name, arguments):
    return broker.invoke(
        session_id=session.session_id,
        run_id=run.run_id,
        tool_name=tool_name,
        arguments=arguments,
        context={"workspace_root": str(workspace)},
    )


def test_tool_definitions_are_framework_neutral_schema() -> None:
    definitions = {definition.name: definition for definition in tool_definitions()}

    assert set(definitions) == {"read_file", "list_dir", "search_text", "git_status"}
    assert definitions["read_file"].input_schema == {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path",
            }
        },
        "required": ["path"],
    }


def test_list_dir_lists_immediate_entries_sorted(tmp_path) -> None:
    workspace, db, broker, session, run = _broker(tmp_path)
    (workspace / "b.txt").write_text("b", encoding="utf-8")
    (workspace / "a").mkdir()

    result = _invoke(workspace, broker, session, run, "list_dir", {"path": "."})

    assert result.status == "ok"
    assert result.output == {
        "entries": [
            {"name": ".sessions", "type": "directory"},
            {"name": "a", "type": "directory"},
            {"name": "b.txt", "type": "file"},
        ]
    }
    db.close()


def test_search_text_returns_matches_and_no_match(tmp_path) -> None:
    workspace, db, broker, session, run = _broker(tmp_path)
    (workspace / "one.txt").write_text("needle\nother", encoding="utf-8")
    (workspace / "two.txt").write_text("nothing", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"\xff\xfe needle")

    match = _invoke(
        workspace,
        broker,
        session,
        run,
        "search_text",
        {"query": "needle", "path": "."},
    )
    no_match = _invoke(
        workspace,
        broker,
        session,
        run,
        "search_text",
        {"query": "absent", "path": "."},
    )

    assert match.status == "ok"
    assert match.output == {
        "matches": [{"path": "one.txt", "line": 1, "text": "needle"}]
    }
    assert no_match.status == "ok"
    assert no_match.output == {"matches": []}
    db.close()


def test_search_text_skips_large_directories_unless_explicitly_requested(
    tmp_path,
) -> None:
    workspace, db, broker, session, run = _broker(tmp_path)
    (workspace / "src").mkdir()
    (workspace / "src" / "app.txt").write_text("needle app", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("needle git", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "pkg.txt").write_text(
        "needle dependency",
        encoding="utf-8",
    )
    (workspace / "build").mkdir()
    (workspace / "build" / "output.txt").write_text("needle build", encoding="utf-8")

    default_search = _invoke(
        workspace,
        broker,
        session,
        run,
        "search_text",
        {"query": "needle", "path": "."},
    )
    explicit_search = _invoke(
        workspace,
        broker,
        session,
        run,
        "search_text",
        {"query": "needle", "path": "node_modules"},
    )

    assert default_search.status == "ok"
    assert default_search.output == {
        "matches": [{"path": "src/app.txt", "line": 1, "text": "needle app"}]
    }
    assert explicit_search.status == "ok"
    assert explicit_search.output == {
        "matches": [
            {
                "path": "node_modules/pkg.txt",
                "line": 1,
                "text": "needle dependency",
            }
        ]
    }
    db.close()


def test_git_status_returns_status_for_workspace(tmp_path) -> None:
    workspace, db, broker, session, run = _broker(tmp_path)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    (workspace / "tracked.txt").write_text("new", encoding="utf-8")

    result = _invoke(workspace, broker, session, run, "git_status", {})

    assert result.status == "ok"
    assert "?? tracked.txt" in result.output
    db.close()
