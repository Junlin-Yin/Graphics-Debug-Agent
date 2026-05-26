from __future__ import annotations

import json

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.context_snapshots import ContextSnapshotStore
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase


def test_compression_snapshot_artifacts_payloads_over_16_kib(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    artifacts = ArtifactStore(db.connection, db.path.parent)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="yolo",
        config_snapshot={"provider": "fake"},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    store = ContextSnapshotStore(db.connection, artifacts)

    snapshot = store.save_compression_snapshot(
        session_id=session.session_id,
        run_id=run.run_id,
        trigger="compression",
        source_checkpoint_id=None,
        active_skill_records=[],
        summary='{"task_goal":"x"}',
        retained_messages=[
            {
                "seq": index,
                "role": "assistant",
                "kind": "assistant_output",
                "content": "large retained message " * 80,
            }
            for index in range(80)
        ],
        omitted_tool_result_count=0,
        evicted_message_count=12,
        evicted_model_call_group_count=3,
        artifact_refs=["art_1"],
        token_estimate={"before": {"total_tokens": 100}, "after": {"total_tokens": 20}},
    )

    row = db.connection.execute(
        """
        SELECT retained_messages_json, payload_artifact_id
        FROM context_snapshots
        WHERE context_snapshot_id = ?
        """,
        (snapshot.context_snapshot_id,),
    ).fetchone()
    assert row[0] == "[]"
    assert row[1] == snapshot.payload_artifact_id
    assert snapshot.payload_artifact_id is not None
    payload = json.loads(
        artifacts.resolve_path(snapshot.payload_artifact_id).read_text(encoding="utf-8")
    )
    assert payload["trigger"] == "compression"
    assert len(payload["retained_messages"]) == 80
    assert payload["evicted_model_call_group_count"] == 3
    db.close()
