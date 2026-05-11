import subprocess

from debug_agent.runtime.workspace import resolve_workspace_root


def test_resolve_workspace_root_uses_git_worktree_root(tmp_path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)

    assert resolve_workspace_root(nested) == repo.resolve()


def test_resolve_workspace_root_falls_back_to_current_directory(tmp_path) -> None:
    workspace = tmp_path / "not-git"
    workspace.mkdir()

    assert resolve_workspace_root(workspace) == workspace.resolve()
