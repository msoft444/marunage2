import base64
import subprocess
from pathlib import Path

import pytest

from backend.repository_workspace import CommitPushError, RepositoryWorkspaceManager


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


def test_repository_workspace_manager_commits_changed_files_and_pushes(tmp_path, monkeypatch):
    workspace, repo, origin = _init_workspace_repo(tmp_path)
    token = "ghp_secret_token_123"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    (repo / "README.md").write_text("hello\nupdated\n", encoding="utf-8")

    manager = RepositoryWorkspaceManager()

    result = manager.commit_and_push(
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


def test_repository_workspace_manager_blocks_when_no_changes_exist(tmp_path):
    workspace, _repo, _origin = _init_workspace_repo(tmp_path)
    manager = RepositoryWorkspaceManager()

    with pytest.raises(CommitPushError, match="phase_edit_no_changes"):
        manager.commit_and_push(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
        )


def test_repository_workspace_manager_blocks_secret_in_changed_files(tmp_path, monkeypatch):
    workspace, repo, _origin = _init_workspace_repo(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret_token_123")
    (repo / "README.md").write_text("hello\nsecret=ghp_secret_token_123\n", encoding="utf-8")
    manager = RepositoryWorkspaceManager()

    with pytest.raises(CommitPushError, match="secret_in_changed_files"):
        manager.commit_and_push(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
        )


def test_repository_workspace_manager_rejects_git_metadata_paths(tmp_path, monkeypatch):
    _workspace, repo, _origin = _init_workspace_repo(tmp_path)
    manager = RepositoryWorkspaceManager()

    monkeypatch.setattr(manager, "_changed_files", lambda _repo_path: [".git/config"])

    with pytest.raises(CommitPushError, match="git_metadata_write_forbidden"):
        manager.validate_changed_files(repo)


def test_repository_workspace_manager_uses_github_token_header_for_clone(tmp_path, monkeypatch):
    commands: list[tuple[list[str], Path]] = []
    token = "token-123"
    expected_header = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def fake_git_runner(args: list[str], cwd: Path):
        commands.append((args, cwd))
        if args[-2] == "clone":
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setenv("GITHUB_TOKEN", token)
    manager = RepositoryWorkspaceManager(git_command_runner=fake_git_runner)

    manager.prepare_repository(
        workspace_path=str(tmp_path / "1"),
        target_repo="example/project",
        target_ref="main",
        working_branch="mn2/1/phase0",
    )

    clone_command = commands[0][0]
    assert clone_command[:3] == ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}"]
    assert clone_command[3:] == ["clone", "https://github.com/example/project.git", str(tmp_path / "1" / "repo")]


def test_repository_workspace_manager_uses_github_token_header_for_github_push(tmp_path, monkeypatch):
    workspace = tmp_path / "1"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("hello\nupdated\n", encoding="utf-8")
    commands: list[tuple[list[str], Path]] = []
    token = "token-123"
    expected_header = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def fake_git_runner(args: list[str], cwd: Path):
        commands.append((args, cwd))
        if args == ["git", "status", "--short"]:
            return subprocess.CompletedProcess(args, 0, stdout=" M README.md\n", stderr="")
        if args == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="abc123\n", stderr="")
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/example/project.git\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setenv("GITHUB_TOKEN", token)
    manager = RepositoryWorkspaceManager(git_command_runner=fake_git_runner)

    result = manager.commit_and_push(
        workspace_path=str(workspace),
        working_branch="mn2/1/phase0",
        task_title="Update README",
    )

    push_command = commands[-1][0]
    assert push_command[:3] == ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}"]
    assert push_command[3:] == ["push", "origin", "mn2/1/phase0"]
    assert result["commit_sha"] == "abc123"