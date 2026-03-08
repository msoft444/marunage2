import logging
from pathlib import Path

from backend import MariaDBTaskBackend
from backend.llm_client import LLMConfigurationError, LLMRateLimitError, LLMTimeoutError
from backend.repository_workspace import CommitPushError
from backend.service_runner import WorkerEngine


def test_worker_cycle_leases_and_starts_queued_task(db_connection_mock):
    engine = WorkerEngine(db_connection_mock, service_name="brain", worker_name="worker-brain-1")

    processed = engine.run_once()

    task = db_connection_mock.tasks[1]
    assert processed is True
    assert task["status"] in {"leased", "running"}
    assert task["lease_expires_at"] is not None
    assert task["started_at"] is not None
    assert any("INSERT INTO logs" in statement for statement, _ in db_connection_mock.statements)


def test_worker_recovers_expired_task_and_logs(db_connection_mock, caplog):
    db_connection_mock.tasks[2]["status"] = "running"
    db_connection_mock.tasks[2]["lease_owner"] = "worker-old"
    db_connection_mock.tasks[2]["lease_expires_at"] = "expired"
    db_connection_mock.tasks[2]["started_at"] = "earlier"
    engine = WorkerEngine(db_connection_mock, service_name="brain", worker_name="worker-brain-1")

    with caplog.at_level(logging.INFO, logger="marunage2.worker_engine"):
        recovered_task_ids = engine.recover_expired_tasks()

    task = db_connection_mock.tasks[2]
    assert recovered_task_ids == [2]
    assert task["status"] == "queued"
    assert task["lease_owner"] is None
    assert task["lease_expires_at"] is None
    assert task["started_at"] is None
    assert "brain recovered expired tasks: 2" in caplog.text


def test_worker_reconnects_database_connection(db_connection_mock, caplog):
    replacement_connection = type(db_connection_mock)()
    db_connection_mock.ping.side_effect = RuntimeError("connection lost")
    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        connection_factory=lambda: replacement_connection,
    )

    with caplog.at_level(logging.INFO, logger="marunage2.worker_engine"):
        reconnected = engine.ensure_connection()

    assert reconnected is True
    assert engine.connection is replacement_connection
    db_connection_mock.close.assert_called_once()
    assert "brain database reconnection succeeded" in caplog.text


def test_worker_resolves_task_working_directory(db_connection_mock):
    db_connection_mock.tasks[1]["workspace_path"] = "/workspace/repo-a"
    engine = WorkerEngine(db_connection_mock, service_name="brain", worker_name="worker-brain-1")

    working_directory = engine.task_working_directory(1)

    assert working_directory == "/workspace/repo-a"


def test_worker_clones_github_repository_and_creates_branch(db_connection_mock, tmp_path):
    commands: list[tuple[list[str], Path]] = []

    def fake_git_runner(args: list[str], cwd: Path) -> None:
        commands.append((args, cwd))
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "develop"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=fake_git_runner,
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    workspace_root = tmp_path / "1"
    assert processed is True
    assert (workspace_root / "artifacts").is_dir()
    assert (workspace_root / "system_docs_snapshot").is_dir()
    assert (workspace_root / "patches").is_dir()
    assert commands == [
        (["git", "clone", "https://github.com/example/project.git", str(workspace_root / "repo")], workspace_root),
        (["git", "checkout", "develop"], workspace_root / "repo"),
        (["git", "checkout", "-B", "mn2/1/phase0"], workspace_root / "repo"),
    ]


def test_worker_blocks_task_when_repository_prepare_fails(db_connection_mock, tmp_path):
    def failing_git_runner(args: list[str], cwd: Path) -> None:
        raise RuntimeError("clone failed")

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=failing_git_runner,
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "repository_prepare_failed" for log in db_connection_mock.logs)


def test_worker_blocks_task_when_workspace_path_is_invalid(db_connection_mock):
    db_connection_mock.tasks[1]["workspace_path"] = "/workspace/../etc"
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "invalid_workspace_path" for log in db_connection_mock.logs)


def test_worker_blocks_llm_task_when_workspace_path_escapes_workspace_root(db_connection_mock, tmp_path):
    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "READMEを更新しました"

    escaped_root = tmp_path.parent / "escaped-task-1"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / ".." / "escaped-task-1")
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=FakeLLMClient(),
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert not (escaped_root / "artifacts" / "llm_response.md").exists()
    assert any(log["event_type"] == "invalid_workspace_path" for log in db_connection_mock.logs)


def test_worker_blocks_task_when_instruction_is_missing(db_connection_mock):
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "invalid_instruction" for log in db_connection_mock.logs)


def test_worker_generates_llm_response_commits_changes_and_marks_succeeded(db_connection_mock, tmp_path):
    prompts: list[str] = []

    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            prompts.append(prompt)
            return "READMEを更新しました\n\n- 現在時刻を追記しました。"

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=FakeLLMClient(),
        workspace_root=tmp_path,
    )

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "1" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "1"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "1" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "1" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "1" / "patches"),
            }

        def commit_and_push(self, **kwargs):
            assert kwargs["working_branch"] == "mn2/1/phase0"
            return {
                "changed_files": ["README.md"],
                "commit_sha": "abc1234",
                "commit_message": "Phase 0: README を更新",
                "working_branch": "mn2/1/phase0",
            }

    engine.task_backend.repository_workspace = FakeRepositoryWorkspace()

    processed = engine.run_once()

    artifact_path = tmp_path / "1" / "artifacts" / "llm_response.md"
    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "succeeded"
    assert db_connection_mock.tasks[1]["result_summary_md"] == "READMEを更新しました"
    assert artifact_path.read_text(encoding="utf-8") == "READMEを更新しました\n\n- 現在時刻を追記しました。"
    assert "You are generating a proposal artifact only." not in prompts[0]
    assert "Do not edit files." not in prompts[0]
    assert "Do not run git push." not in prompts[0]
    assert "Return only the proposed content or patch in markdown." not in prompts[0]
    assert "リポジトリのファイルを直接編集し、変更を完成させよ" in prompts[0]
    assert "git commit / git push は実行するな" in prompts[0]
    assert "リポジトリ外のファイルを編集するな" in prompts[0]
    assert "README に現在時刻を記載する" in prompts[0]
    assert any(log["event_type"] == "llm_generation_started" for log in db_connection_mock.logs)
    assert any(log["event_type"] == "llm_generation_succeeded" for log in db_connection_mock.logs)
    assert any(log["event_type"] == "git_commit_succeeded" for log in db_connection_mock.logs)
    assert any(log["event_type"] == "git_push_succeeded" for log in db_connection_mock.logs)


def test_worker_blocks_task_when_llm_client_is_not_configured(db_connection_mock, tmp_path):
    class MissingKeyLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            raise LLMConfigurationError("copilot command is not installed")

    def fake_git_runner(args: list[str], cwd: Path) -> None:
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=fake_git_runner,
        llm_client=MissingKeyLLMClient(),
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "llm_generation_failed" for log in db_connection_mock.logs)


def test_worker_blocks_task_when_llm_times_out(db_connection_mock, tmp_path):
    class TimeoutLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            raise LLMTimeoutError("LLM request timed out")

    def fake_git_runner(args: list[str], cwd: Path) -> None:
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=fake_git_runner,
        llm_client=TimeoutLLMClient(),
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "llm_generation_failed" for log in db_connection_mock.logs)


def test_worker_blocks_task_when_llm_rate_limit_is_exhausted(db_connection_mock, tmp_path):
    class RateLimitedLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            raise LLMRateLimitError("LLM rate limit exceeded")

    def fake_git_runner(args: list[str], cwd: Path) -> None:
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=fake_git_runner,
        llm_client=RateLimitedLLMClient(),
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "llm_generation_failed" for log in db_connection_mock.logs)


def test_backend_blocks_task_when_commit_push_fails(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "queued"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["result_summary_md"] = "README を更新しました"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "README を更新しました"

    backend = MariaDBTaskBackend(db_connection_mock, llm_client=FakeLLMClient(), workspace_root=tmp_path)

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "1" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "1"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "1" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "1" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "1" / "patches"),
            }

        def commit_and_push(self, **kwargs):
            raise CommitPushError("git_push_failed: denied")

    backend.repository_workspace = FakeRepositoryWorkspace()

    processed = backend.process_next_queued_task("brain", "worker-brain-1")

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "git_push_failed" and "denied" in log["message"] for log in db_connection_mock.logs)


def test_backend_blocks_task_when_direct_edit_produces_no_changes(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "queued"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["result_summary_md"] = "README を更新しました"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "README を更新しました"

    backend = MariaDBTaskBackend(db_connection_mock, llm_client=FakeLLMClient(), workspace_root=tmp_path)

    class NoChangesRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "1" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "1"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "1" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "1" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "1" / "patches"),
            }

        def commit_and_push(self, **kwargs):
            raise CommitPushError("phase_edit_no_changes")

    backend.repository_workspace = NoChangesRepositoryWorkspace()

    processed = backend.process_next_queued_task("brain", "worker-brain-1")

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "phase_edit_no_changes" for log in db_connection_mock.logs)


def test_backend_blocks_deprecated_artifact_apply_path(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["result_summary_md"] = "README を更新しました"
    db_connection_mock.tasks[1]["payload_json"] = {"task": "README を更新"}

    backend = MariaDBTaskBackend(db_connection_mock, workspace_root=tmp_path)

    processed = backend.apply_artifact_for_task(1, "brain", "worker-brain-1")

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "artifact_apply_deprecated" for log in db_connection_mock.logs)


def test_backend_skips_artifact_apply_when_task_is_not_waiting_approval(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "queued"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"

    backend = MariaDBTaskBackend(db_connection_mock, workspace_root=tmp_path)

    processed = backend.apply_artifact_for_task(1, "brain", "worker-brain-1")

    assert processed is False
    assert db_connection_mock.tasks[1]["status"] == "queued"
    assert any(log["event_type"] == "artifact_apply_skipped" for log in db_connection_mock.logs)


def test_worker_blocks_task_when_llm_response_is_empty(db_connection_mock, tmp_path):
    class EmptyResponseLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "   \n"

    def fake_git_runner(args: list[str], cwd: Path) -> None:
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=fake_git_runner,
        llm_client=EmptyResponseLLMClient(),
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "llm_generation_failed" for log in db_connection_mock.logs)


def test_worker_blocks_task_when_llm_response_exceeds_size_limit(db_connection_mock, tmp_path, monkeypatch):
    class HugeResponseLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "x" * 80

    def fake_git_runner(args: list[str], cwd: Path) -> None:
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LLM_MAX_RESPONSE_BYTES", "32")
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=fake_git_runner,
        llm_client=HugeResponseLLMClient(),
        workspace_root=tmp_path,
    )

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    assert any(log["event_type"] == "llm_generation_failed" for log in db_connection_mock.logs)


def test_worker_masks_github_token_from_saved_artifact(db_connection_mock, tmp_path, monkeypatch):
    class TokenEchoLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "token=ghs_secret_token_value"

    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret_token_value")
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["payload_json"] = {
        "task": "README を更新",
        "instruction": "README に現在時刻を記載する",
    }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=TokenEchoLLMClient(),
        workspace_root=tmp_path,
    )

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "1" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "1"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "1" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "1" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "1" / "patches"),
            }

        def commit_and_push(self, **kwargs):
            return {
                "changed_files": ["README.md"],
                "commit_sha": "abc1234",
                "commit_message": "Phase 0: README を更新",
                "working_branch": "mn2/1/phase0",
            }

    engine.task_backend.repository_workspace = FakeRepositoryWorkspace()

    processed = engine.run_once()

    artifact_path = tmp_path / "1" / "artifacts" / "llm_response.md"
    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "succeeded"
    assert "ghs_secret_token_value" not in artifact_path.read_text(encoding="utf-8")

