from __future__ import annotations

import pytest

from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.persistence.todo_plans import TodoPlanStore


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    second_run = runs.create_prompt_run(session.session_id, run_id="run_2")
    events = EventWriter(db.connection, db.path.parent)
    store = TodoPlanStore(db.connection)
    return db, session, run, second_run, events, store


def test_missing_current_plan_returns_explicit_empty_state(tmp_path) -> None:
    db, _session, run, _second_run, _events, store = _runtime(tmp_path)

    current = store.get_current(run.run_id)

    assert current.run_id == run.run_id
    assert current.version == 0
    assert current.items == []
    assert current.is_empty is True
    assert current.updated_at is None
    db.close()


def test_replace_plan_versions_orders_items_and_writes_event(tmp_path) -> None:
    db, session, run, _second_run, events, store = _runtime(tmp_path)

    replaced = store.replace_plan(
        session.session_id,
        run.run_id,
        [
            {"content": "First", "status": "completed"},
            {
                "content": "Second",
                "status": "in_progress",
                "activeForm": "Working second",
            },
        ],
        events,
    )

    assert replaced.previous_plan_version == 0
    assert replaced.plan.version == 1
    assert replaced.plan.items == [
        {
            "index": 1,
            "content": "First",
            "status": "completed",
            "metadata": {},
        },
        {
            "index": 2,
            "content": "Second",
            "status": "in_progress",
            "activeForm": "Working second",
            "metadata": {},
        },
    ]
    assert replaced.event.kind == "todo_updated"
    assert replaced.event.payload == {
        "previous_plan_version": 0,
        "plan_version": 1,
        "item_count": 2,
        "counts": {"pending": 0, "in_progress": 1, "completed": 1},
        "items": [
            {"index": 1, "content": "First", "status": "completed"},
            {
                "index": 2,
                "content": "Second",
                "status": "in_progress",
                "activeForm": "Working second",
            },
        ],
    }
    assert [event.kind for event in events.list_for_run(run.run_id)] == ["todo_updated"]
    db.close()


def test_replace_plan_is_run_scoped_and_can_clear_plan(tmp_path) -> None:
    db, session, run, second_run, events, store = _runtime(tmp_path)
    store.replace_plan(
        session.session_id,
        run.run_id,
        [{"content": "Only run one", "status": "pending"}],
        events,
    )

    cleared = store.replace_plan(session.session_id, run.run_id, [], events)

    assert cleared.previous_plan_version == 1
    assert cleared.plan.version == 2
    assert cleared.plan.items == []
    assert cleared.plan.is_empty is True
    assert store.get_current(second_run.run_id).version == 0
    assert store.get_current(second_run.run_id).items == []
    db.close()


class _FailingEventWriter:
    def append_in_transaction(self, _event):
        raise RuntimeError("event insert failed")


def test_failed_event_insert_rolls_back_plan_replacement(tmp_path) -> None:
    db, session, run, _second_run, events, store = _runtime(tmp_path)
    store.replace_plan(
        session.session_id,
        run.run_id,
        [{"content": "Original", "status": "pending"}],
        events,
    )

    with pytest.raises(RuntimeError, match="event insert failed"):
        store.replace_plan(
            session.session_id,
            run.run_id,
            [{"content": "Mutated", "status": "completed"}],
            _FailingEventWriter(),
        )

    assert store.get_current(run.run_id).version == 1
    assert store.get_current(run.run_id).items[0]["content"] == "Original"
    assert [event.kind for event in events.list_for_run(run.run_id)] == ["todo_updated"]
    db.close()
