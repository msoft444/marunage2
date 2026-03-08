import subprocess
from pathlib import Path

import pytest

from backend.repository_workspace import ArtifactApplyError, RepositoryWorkspaceManager


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_workspace_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init"], seed)
    _git(["config", "user.name", "Seed User"], seed)
    _git(["config", "user.email", "seed@example.com"], seed)
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], seed)
    _git(["commit", "-m", "initial commit"], seed)
    _git(["branch", "-M", "main"], seed)
    _git(["remote", "add", "origin", str(origin)], seed)
    _git(["push", "-u", "origin", "main"], seed)

    workspace = tmp_path / "1"
    artifacts = workspace / "artifacts"
    patches = workspace / "patches"
    repo = workspace / "repo"
    artifacts.mkdir(parents=True)
    patches.mkdir(parents=True)
    _git(["clone", str(origin), str(repo)], tmp_path)
    _git(["checkout", "-B", "mn2/1/phase0", "origin/main"], repo)
    return workspace, repo, origin


def test_repository_workspace_manager_applies_diff_commits_and_pushes(tmp_path, monkeypatch):
    workspace, repo, origin = _init_workspace_repo(tmp_path)
    token = "ghp_secret_token_123"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    artifact = workspace / "artifacts" / "llm_response.md"
    artifact.write_text(
        "Proposal\n\n```diff\n--- a/README.md\n+++ b/README.md\n@@ -1 +1,2 @@\n hello\n+updated\n```\n",
        encoding="utf-8",
    )

    manager = RepositoryWorkspaceManager()

    result = manager.apply_artifact(
        workspace_path=str(workspace),
        working_branch="mn2/1/phase0",
        task_title=f"Update README {token}",
        result_summary_md="README update summary",
    )

    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\nupdated\n"
    assert result["changed_files"] == ["README.md"]
    assert token not in result["commit_message"]
    assert "[MASKED_GITHUB_TOKEN]" in result["commit_message"]
    head_subject = _git(["log", "-1", "--pretty=%s"], repo).stdout.strip()
    remote_subject = _git(["--git-dir", str(origin), "log", "mn2/1/phase0", "-1", "--pretty=%s"], tmp_path).stdout.strip()
    assert head_subject == result["commit_message"]
    assert remote_subject == result["commit_message"]


def test_repository_workspace_manager_blocks_missing_artifact(tmp_path):
    workspace, _repo, _origin = _init_workspace_repo(tmp_path)
    manager = RepositoryWorkspaceManager()

    with pytest.raises(ArtifactApplyError, match="artifact_not_found"):
        manager.apply_artifact(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
        )


def test_repository_workspace_manager_blocks_artifact_without_diff(tmp_path):
    workspace, _repo, _origin = _init_workspace_repo(tmp_path)
    artifact = workspace / "artifacts" / "llm_response.md"
    artifact.write_text("説明だけで diff がありません\n", encoding="utf-8")
    manager = RepositoryWorkspaceManager()

    with pytest.raises(ArtifactApplyError, match="no_diff_section"):
        manager.apply_artifact(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
        )


def test_repository_workspace_manager_blocks_path_traversal_in_diff(tmp_path):
    workspace, _repo, _origin = _init_workspace_repo(tmp_path)
    artifact = workspace / "artifacts" / "llm_response.md"
    artifact.write_text(
        "--- a/../evil.txt\n+++ b/../evil.txt\n@@ -1 +1 @@\n-x\n+y\n",
        encoding="utf-8",
    )
    manager = RepositoryWorkspaceManager()

    with pytest.raises(ArtifactApplyError, match="path_traversal"):
        manager.apply_artifact(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
        )