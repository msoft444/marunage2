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
    _git(["config", "user.name", "Test User"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
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


def test_prepare_repository_restores_existing_remote_working_branch(tmp_path):
    workspace, repo, origin = _init_workspace_repo(tmp_path)

    # Seed a remote working branch that is ahead of main.
    _git(["checkout", "mn2/1/phase0"], repo)
    (repo / "README.md").write_text("hello\nremote-head\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "advance working branch"], repo)
    _git(["push", "origin", "mn2/1/phase0"], repo)
    remote_head = _git(["--git-dir", str(origin), "rev-parse", "mn2/1/phase0"], tmp_path).stdout.strip()

    # Move local main forward separately so recreating from target_ref would diverge.
    _git(["checkout", "main"], repo)
    (repo / "README.md").write_text("hello\nmain-head\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "advance main"], repo)
    _git(["push", "origin", "main"], repo)

    manager = RepositoryWorkspaceManager()
    manager.prepare_repository(
        workspace_path=str(workspace),
        target_repo="example/project",
        target_ref="main",
        working_branch="mn2/1/phase0",
    )

    prepared_head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    assert prepared_head == remote_head


def test_prepare_repository_uses_github_token_header_for_github_fetch(tmp_path, monkeypatch):
    workspace = tmp_path / "1"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    commands: list[tuple[list[str], Path]] = []
    token = "token-123"
    expected_header = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def fake_git_runner(args: list[str], cwd: Path):
        commands.append((args, cwd))
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/example/project.git\n", stderr="")
        if args[:3] == ["git", "rev-parse", "--verify"]:
            raise subprocess.CalledProcessError(1, args, stderr="missing")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setenv("GITHUB_TOKEN", token)
    manager = RepositoryWorkspaceManager(git_command_runner=fake_git_runner)

    manager.prepare_repository(
        workspace_path=str(workspace),
        target_repo="example/project",
        target_ref="main",
        working_branch="mn2/1/phase0",
    )

    fetch_command = commands[1][0]
    assert fetch_command[:3] == ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}"]
    assert fetch_command[3:] == ["fetch", "origin", "--prune"]


def test_repository_workspace_manager_uses_github_token_header_for_working_branch_fetch(tmp_path, monkeypatch):
    workspace = tmp_path / "1"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    commands: list[tuple[list[str], Path]] = []
    token = "token-123"
    expected_header = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def fake_git_runner(args: list[str], cwd: Path):
        commands.append((args, cwd))
        if args == ["git", "fetch", "origin", "--prune"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/example/project.git\n", stderr="")
        if args == ["git", "rev-parse", "--verify", "mn2/1/phase0"]:
            raise subprocess.CalledProcessError(1, args, stderr="missing local")
        if args[:3] == ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ["git", "rev-parse", "--verify", "origin/mn2/1/phase0"]:
            return subprocess.CompletedProcess(args, 0, stdout="origin/mn2/1/phase0\n", stderr="")
        if args == ["git", "diff", "--no-ext-diff", "origin/main...origin/mn2/1/phase0"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ["git", "fetch", "origin", "main"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ["git", "rev-parse", "--verify", "origin/main"]:
            return subprocess.CompletedProcess(args, 0, stdout="origin/main\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setenv("GITHUB_TOKEN", token)
    manager = RepositoryWorkspaceManager(git_command_runner=fake_git_runner)

    manager.get_diff(
        workspace_path=str(workspace),
        working_branch="mn2/1/phase0",
        merge_target="main",
    )

    auth_fetch_commands = [args for args, _cwd in commands if args[:3] == ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}"]]
    assert [command[3:] for command in auth_fetch_commands] == [
        ["fetch", "origin", "main"],
        ["fetch", "origin", "mn2/1/phase0"],
    ]


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


def test_repository_workspace_manager_lists_allowlisted_merge_targets(monkeypatch, tmp_path):
    workspace = tmp_path / "1"
    repo = workspace / "repo"
    repo.mkdir(parents=True)
    token = "token-123"
    expected_header = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def fake_git_runner(args: list[str], cwd: Path):
        if args == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(args, 0, stdout="https://github.com/example/project.git\n", stderr="")
        if args[:3] == ["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}"] and args[3:] == ["fetch", "origin", "--prune"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ["git", "rev-parse", "--verify", "mn2/1/phase0"]:
            return subprocess.CompletedProcess(args, 0, stdout="mn2/1/phase0\n", stderr="")
        if args == ["git", "branch", "-r", "--format=%(refname:short)"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="origin/HEAD -> origin/main\norigin/main\norigin/develop\norigin/release/v1\n",
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setenv("GITHUB_TOKEN", token)
    monkeypatch.setenv("MERGE_TARGET_ALLOWLIST", "main,develop")
    manager = RepositoryWorkspaceManager(git_command_runner=fake_git_runner)

    result = manager.list_merge_targets(workspace_path=str(workspace), working_branch="mn2/1/phase0")

    assert result == ["main", "develop"]


def test_repository_workspace_manager_returns_diff_against_merge_target(tmp_path):
    workspace, repo, _origin = _init_workspace_repo(tmp_path)
    _git(["checkout", "mn2/1/phase0"], repo)
    (repo / "README.md").write_text("hello\nupdated\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "update readme"], repo)

    manager = RepositoryWorkspaceManager()

    diff_text = manager.get_diff(
        workspace_path=str(workspace),
        working_branch="mn2/1/phase0",
        merge_target="main",
    )

    assert "README.md" in diff_text
    assert "+updated" in diff_text


def test_repository_workspace_manager_merges_into_target_and_cleans_up_branches(tmp_path):
    workspace, repo, origin = _init_workspace_repo(tmp_path)
    _git(["checkout", "mn2/1/phase0"], repo)
    (repo / "README.md").write_text("hello\nupdated\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "update readme"], repo)
    _git(["push", "origin", "mn2/1/phase0"], repo)

    manager = RepositoryWorkspaceManager()

    result = manager.merge_and_cleanup(
        workspace_path=str(workspace),
        working_branch="mn2/1/phase0",
        merge_target="main",
    )

    _git(["checkout", "main"], repo)
    assert "updated" in (repo / "README.md").read_text(encoding="utf-8")
    assert _git(["branch", "--list", "mn2/1/phase0"], repo).stdout.strip() == ""
    assert _git(["--git-dir", str(origin), "branch", "--list", "mn2/1/phase0"], tmp_path).stdout.strip() == ""
    assert result["merge_target"] == "main"
    assert result["deleted_local_branch"] is True
    assert result["deleted_remote_branch"] is True


def test_repository_workspace_manager_rejects_non_allowlisted_merge_target(tmp_path):
    workspace, _repo, _origin = _init_workspace_repo(tmp_path)
    manager = RepositoryWorkspaceManager()

    with pytest.raises(CommitPushError, match="merge_target_not_allowed"):
        manager.get_diff(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
            merge_target="release/v1",
        )


def test_repository_workspace_manager_reports_missing_working_branch_for_diff(tmp_path):
    workspace, repo, _origin = _init_workspace_repo(tmp_path)
    _git(["checkout", "main"], repo)
    _git(["branch", "-D", "mn2/1/phase0"], repo)

    manager = RepositoryWorkspaceManager()

    with pytest.raises(CommitPushError, match="working_branch_not_found"):
        manager.get_diff(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
            merge_target="main",
        )


def test_repository_workspace_manager_reports_missing_working_branch_for_merge_targets(tmp_path):
    workspace, repo, _origin = _init_workspace_repo(tmp_path)
    _git(["checkout", "main"], repo)
    _git(["branch", "-D", "mn2/1/phase0"], repo)

    manager = RepositoryWorkspaceManager()

    with pytest.raises(CommitPushError, match="working_branch_not_found"):
        manager.list_merge_targets(
            workspace_path=str(workspace),
            working_branch="mn2/1/phase0",
        )