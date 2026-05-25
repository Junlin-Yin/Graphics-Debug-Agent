from __future__ import annotations

import sqlite3
import hashlib
from pathlib import Path

import pytest

from debug_agent.persistence.artifacts import ArtifactStore
from debug_agent.persistence.runs import RunStore
from debug_agent.persistence.sessions import SessionStore
from debug_agent.persistence.skills import SkillSnapshotStore
from debug_agent.persistence.sqlite import RuntimeDatabase
from debug_agent.skills.registry import SkillRegistry, SkillRegistryError


def _skill_md(name: str, description: str = "Useful skill", body: str = "Do it.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\n{body}\n"


def _runtime(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    db = RuntimeDatabase.bootstrap(workspace)
    sessions = SessionStore(db.connection)
    runs = RunStore(db.connection)
    session = sessions.create(
        workspace_root=workspace,
        approval_mode="normal",
        config_snapshot={},
        session_id="sess_1",
    )
    run = runs.create_prompt_run(session.session_id, run_id="run_1")
    sessions.set_active_run(session.session_id, run.run_id)
    return workspace, home, db, session, run


def test_discovers_only_global_and_project_direct_children_with_project_precedence(
    tmp_path,
) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    global_root = home / ".debug-agent" / "skills"
    project_root = workspace / ".debug-agent" / "skills"
    (global_root / "alpha").mkdir(parents=True)
    (global_root / "alpha" / "SKILL.md").write_text(
        _skill_md("shared", description="global"), encoding="utf-8"
    )
    (project_root / "alpha").mkdir(parents=True)
    (project_root / "alpha" / "SKILL.md").write_text(
        _skill_md("shared", description="project"), encoding="utf-8"
    )
    (project_root / "nested" / "child").mkdir(parents=True)
    (project_root / "nested" / "SKILL.md").write_text(
        _skill_md("parent"), encoding="utf-8"
    )
    (project_root / "nested" / "child" / "SKILL.md").write_text(
        _skill_md("nested"), encoding="utf-8"
    )
    (project_root / "root_skill.md").write_text(_skill_md("ignored"), encoding="utf-8")
    (project_root / "link").symlink_to(project_root / "alpha", target_is_directory=True)

    snapshots = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    ).snapshot(session_id=session.session_id, run_id=run.run_id)

    assert [snapshot.name for snapshot in snapshots] == ["parent", "shared"]
    assert snapshots[1].source_scope == "project"
    assert snapshots[1].manifest["description"] == "project"
    db.close()


def test_duplicate_names_within_same_scope_fail_startup_config_error(tmp_path) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    root = workspace / ".debug-agent" / "skills"
    (root / "first").mkdir(parents=True)
    (root / "second").mkdir(parents=True)
    (root / "first" / "SKILL.md").write_text(_skill_md("dup"), encoding="utf-8")
    (root / "second" / "SKILL.md").write_text(_skill_md("dup"), encoding="utf-8")

    with pytest.raises(SkillRegistryError) as raised:
        SkillRegistry(
            workspace_root=workspace,
            home_dir=home,
            artifact_store=ArtifactStore(db.connection, db.path.parent),
        ).snapshot(session_id=session.session_id, run_id=run.run_id)

    assert raised.value.error_class == "config_error"
    assert "Duplicate skill name in project scope: dup" in str(raised.value)
    db.close()


@pytest.mark.parametrize(
    ("front_matter", "message"),
    [
        ("name: bad\nextra: value\ndescription: x\n", "Unknown skill manifest field"),
        ("name: bad name\ndescription: x\n", "Invalid skill name"),
        ("name: wf\ndescription: x\nexecution_mode: workflow\n", "Only prompt skills"),
        ("name: t\ndescription: 1\n", "description must be a string"),
        ("name: t\ndescription: x\ntriggers: run\n", "triggers must be a list"),
    ],
)
def test_invalid_manifests_fail_config_error(tmp_path, front_matter, message) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    root = workspace / ".debug-agent" / "skills" / "bad"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(f"---\n{front_matter}---\nbody\n", encoding="utf-8")

    with pytest.raises(SkillRegistryError) as raised:
        SkillRegistry(
            workspace_root=workspace,
            home_dir=home,
            artifact_store=ArtifactStore(db.connection, db.path.parent),
        ).snapshot(session_id=session.session_id, run_id=run.run_id)

    assert raised.value.error_class == "config_error"
    assert message in str(raised.value)
    db.close()


def test_hash_is_stable_and_ignores_files_outside_references(tmp_path) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("alpha"), encoding="utf-8")
    (skill_dir / "notes.txt").write_text("ignored", encoding="utf-8")
    (skill_dir / "references" / "guide.txt").write_text("guide\r\n", encoding="utf-8")
    registry = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    )

    first = registry.snapshot(session_id=session.session_id, run_id=run.run_id)[0]
    (skill_dir / "notes.txt").write_text("changed", encoding="utf-8")
    second = registry.snapshot(session_id="sess_2", run_id="run_2")[0]

    assert first.overall_content_hash == second.overall_content_hash
    assert first.skill_md_content_hash.startswith("sha256:")
    assert first.references[0].content_hash.startswith("sha256:")
    db.close()


def test_text_reference_hashes_normalized_utf8_content(tmp_path) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    crlf_dir = workspace / ".debug-agent" / "skills" / "crlf"
    lf_dir = home / ".debug-agent" / "skills" / "lf"
    (crlf_dir / "references").mkdir(parents=True)
    (lf_dir / "references").mkdir(parents=True)
    (crlf_dir / "SKILL.md").write_text(_skill_md("same"), encoding="utf-8")
    (lf_dir / "SKILL.md").write_text(_skill_md("same"), encoding="utf-8")
    (crlf_dir / "references" / "guide.txt").write_bytes(b"a\r\nb\r\n")
    (lf_dir / "references" / "guide.txt").write_bytes(b"a\nb\n")
    registry = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    )

    project_snapshot = registry.snapshot(session_id=session.session_id, run_id=run.run_id)[0]
    global_snapshot = SkillRegistry(
        workspace_root=tmp_path / "other-workspace",
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    ).snapshot(session_id="sess_2", run_id="run_2")[0]

    expected_hash = "sha256:" + hashlib.sha256(b"a\nb\n").hexdigest()
    assert project_snapshot.references[0].content_hash == expected_hash
    assert global_snapshot.references[0].content_hash == expected_hash
    assert project_snapshot.references[0].size_bytes == len(b"a\r\nb\r\n")
    assert global_snapshot.references[0].size_bytes == len(b"a\nb\n")
    db.close()


def test_direct_child_skill_directory_missing_root_skill_md_fails_config_error(
    tmp_path,
) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    child = workspace / ".debug-agent" / "skills" / "candidate"
    (child / "nested").mkdir(parents=True)
    (child / "nested" / "SKILL.md").write_text(_skill_md("ignored"), encoding="utf-8")

    with pytest.raises(SkillRegistryError) as raised:
        SkillRegistry(
            workspace_root=workspace,
            home_dir=home,
            artifact_store=ArtifactStore(db.connection, db.path.parent),
        ).snapshot(session_id=session.session_id, run_id=run.run_id)

    assert raised.value.error_class == "config_error"
    assert "Missing root SKILL.md" in str(raised.value)
    db.close()


def test_reference_artifacting_and_snapshot_payload_artifacting_persist_rows(
    tmp_path,
) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    (skill_dir / "references").mkdir(parents=True)
    large_body = "x" * (17 * 1024)
    (skill_dir / "SKILL.md").write_text(
        _skill_md("alpha", body=large_body), encoding="utf-8"
    )
    (skill_dir / "references" / "large.txt").write_text(
        "r" * (17 * 1024), encoding="utf-8"
    )
    (skill_dir / "references" / "binary.bin").write_bytes(b"\xff\x00")

    snapshots = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    ).snapshot(session_id=session.session_id, run_id=run.run_id)
    SkillSnapshotStore(db.connection).save_many(snapshots)

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        skill_payload_artifact = conn.execute(
            "SELECT payload_artifact_id FROM skill_snapshots WHERE skill_name = 'alpha'"
        ).fetchone()[0]
        reference_rows = conn.execute(
            """
            SELECT reference_path, media_kind, inline_text_payload, payload_artifact_id
            FROM skill_reference_snapshots
            ORDER BY reference_path
            """
        ).fetchall()
        artifacts = conn.execute("SELECT artifact_id FROM artifacts").fetchall()

    assert skill_payload_artifact is not None
    assert reference_rows == [
        ("references/binary.bin", "binary", None, reference_rows[0][3]),
        ("references/large.txt", "text", None, reference_rows[1][3]),
    ]
    assert reference_rows[0][3] is not None
    assert reference_rows[1][3] is not None
    assert len(artifacts) == 3
    db.close()


def test_available_skill_headers_list_candidates_without_bodies_or_references(
    tmp_path,
) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        _skill_md("alpha", description="Alpha skill", body="SECRET BODY"),
        encoding="utf-8",
    )
    (skill_dir / "references" / "guide.txt").write_text(
        "SECRET REF", encoding="utf-8"
    )
    snapshots = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    ).snapshot(session_id=session.session_id, run_id=run.run_id)
    store = SkillSnapshotStore(db.connection)
    store.save_many(snapshots)

    headers = store.available_skill_headers(
        session_id=session.session_id,
        run_id=run.run_id,
    )

    assert "alpha: Alpha skill" in headers
    assert "SECRET BODY" not in headers
    assert "SECRET REF" not in headers
    assert "references/guide.txt" not in headers
    db.close()


def test_persisted_snapshot_is_not_changed_by_source_mutation(tmp_path) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(_skill_md("alpha", body="original"), encoding="utf-8")
    snapshots = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    ).snapshot(session_id=session.session_id, run_id=run.run_id)
    SkillSnapshotStore(db.connection).save_many(snapshots)

    skill_file.write_text(_skill_md("alpha", body="mutated"), encoding="utf-8")

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        content, content_hash = conn.execute(
            "SELECT skill_md_content, overall_content_hash FROM skill_snapshots"
        ).fetchone()

    assert content == _skill_md("alpha", body="original")
    assert content_hash == snapshots[0].overall_content_hash
    db.close()


def test_persisted_skill_md_content_verifies_against_content_hash(tmp_path) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_bytes(
        b"---\r\nname: alpha\r\ndescription: Alpha\r\n---\r\nbody\r\n"
    )
    snapshots = SkillRegistry(
        workspace_root=workspace,
        home_dir=home,
        artifact_store=ArtifactStore(db.connection, db.path.parent),
    ).snapshot(session_id=session.session_id, run_id=run.run_id)
    SkillSnapshotStore(db.connection).save_many(snapshots)

    with sqlite3.connect(workspace / ".sessions" / "runtime.db") as conn:
        content, content_hash = conn.execute(
            "SELECT skill_md_content, skill_md_content_hash FROM skill_snapshots"
        ).fetchone()

    assert content == "---\nname: alpha\ndescription: Alpha\n---\nbody\n"
    assert "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest() == content_hash
    db.close()


def test_unreadable_reference_fails_startup_config_error(tmp_path, monkeypatch) -> None:
    workspace, home, db, session, run = _runtime(tmp_path)
    skill_dir = workspace / ".debug-agent" / "skills" / "alpha"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("alpha"), encoding="utf-8")
    blocked = skill_dir / "references" / "blocked.txt"
    blocked.write_text("blocked", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def read_bytes(self):
        if self == blocked:
            raise OSError("permission denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(SkillRegistryError) as raised:
        SkillRegistry(
            workspace_root=workspace,
            home_dir=home,
            artifact_store=ArtifactStore(db.connection, db.path.parent),
        ).snapshot(session_id=session.session_id, run_id=run.run_id)

    assert raised.value.error_class == "config_error"
    assert "Unreadable reference file" in str(raised.value)
    db.close()
