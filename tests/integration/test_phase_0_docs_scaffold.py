from pathlib import Path


def test_phase_0_contract_docs_exist() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    assert (repo_root / "docs/phase-0/scope.md").is_file()
    assert (repo_root / "docs/phase-0/implementation-plan.md").is_file()
    assert (repo_root / "docs/phase-0/operations.md").is_file()
