from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.checkpoints import CheckpointStore
from debug_agent.persistence.events import EventWriter
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.sqlite import RuntimeDatabase


def test_empty_phase_0_stores_can_be_constructed(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)

    stores = [
        SessionStore(db.connection),
        RunStore(db.connection),
        EventWriter(db.connection, db.path.parent),
        CheckpointStore(db.connection),
        ArtifactStore(db.connection, db.path.parent),
    ]

    assert all(store.connection is db.connection for store in stores)
    db.close()
