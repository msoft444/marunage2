import base64
import logging
from pathlib import Path
from types import SimpleNamespace

from backend import MariaDBTaskBackend
from backend.phase_orchestrator import DEFAULT_REWORK_LIMIT, PhaseOrchestrator
from backend.llm_client import LLMConfigurationError, LLMRateLimitError, LLMTimeoutError
from backend.repository_workspace import CommitPushError
from backend.service_runner import WorkerEngine


def test_build_prompt_includes_phase_specific_context_for_orchestrated_tasks(db_connection_mock):
    backend = MariaDBTaskBackend(db_connection_mock)
    phase1_task = SimpleNamespace(
        phase=1,
        task_type="phase1_design",
        payload_json={"task": "README を改善"},
        target_repo="example/project",
        working_branch="mn2/10/phase0",
    )

    prompt = backend._build_prompt(phase1_task, "README を改善する")

    assert "Phase: 1" in prompt
    assert "Task Type: phase1_design" in prompt
    assert "基本設計" in prompt
    assert "README を改善する" in prompt


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
    token = "token-123"
    expected_header = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")

    def fake_git_runner(args: list[str], cwd: Path):
        commands.append((args, cwd))
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
            return None
        if args == ["git", "remote", "get-url", "origin"]:
            from subprocess import CompletedProcess

            return CompletedProcess(args, 0, stdout="https://github.com/example/project.git\n", stderr="")
        if args[:3] == ["git", "rev-parse", "--verify"] and args[-1].startswith("origin/"):
            raise RuntimeError("remote branch missing")
        return None

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "develop"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    import os

    previous_token = os.environ.get("GITHUB_TOKEN")
    os.environ["GITHUB_TOKEN"] = token

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        git_command_runner=fake_git_runner,
        workspace_root=tmp_path,
    )

    try:
        processed = engine.run_once()
    finally:
        if previous_token is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = previous_token

    workspace_root = tmp_path / "1"
    assert processed is True
    assert (workspace_root / "artifacts").is_dir()
    assert (workspace_root / "system_docs_snapshot").is_dir()
    assert (workspace_root / "patches").is_dir()
    assert commands == [
        (["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}", "clone", "https://github.com/example/project.git", str(workspace_root / "repo")], workspace_root),
        (["git", "remote", "get-url", "origin"], workspace_root / "repo"),
        (["git", "-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {expected_header}", "fetch", "origin", "--prune"], workspace_root / "repo"),
        (["git", "rev-parse", "--verify", "origin/mn2/1/phase0"], workspace_root / "repo"),
        (["git", "checkout", "develop"], workspace_root / "repo"),
        (["git", "checkout", "-B", "mn2/1/phase0", "develop"], workspace_root / "repo"),
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


def test_worker_generates_llm_response_commits_changes_and_marks_waiting_approval_for_github_clone_task(db_connection_mock, tmp_path):
    prompts: list[str] = []

    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            prompts.append(prompt)
            return "READMEを更新しました\n\n- 現在時刻を追記しました。"

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
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
    assert db_connection_mock.tasks[1]["status"] == "waiting_approval"
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


def test_worker_advances_orchestrated_phase_task_after_direct_edit_success(db_connection_mock, tmp_path):
    db_connection_mock.tasks = {
        10: {
            "id": 10,
            "parent_task_id": None,
            "root_task_id": 10,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "10"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/10/phase0",
            "payload_json": {
                "phase_flow": [0, 1, 2, 3, 4, 5],
                "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 0, "phase_attempt": 0},
            },
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        11: {
            "id": 11,
            "parent_task_id": 10,
            "root_task_id": 10,
            "task_type": "phase0_brainstorm",
            "phase": 0,
            "status": "queued",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "10"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/10/phase0",
            "payload_json": {
                "task": "README を更新",
                "instruction": "README を更新する",
                "phase_flow": [0, 1, 2, 3, 4, 5],
                "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 0, "phase_attempt": 0},
            },
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 12

    class FakeLLMClient:
        model = "gpt-5.4-mini"

        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "READMEを更新しました\n\n- phase 0 complete"

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "10" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "10"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "10" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "10" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "10" / "patches"),
            }

        def commit_and_push(self, **kwargs):
            return {
                "changed_files": ["README.md"],
                "commit_sha": "abc1234",
                "commit_message": "phase 0",
                "working_branch": kwargs["working_branch"],
            }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=FakeLLMClient(),
        workspace_root=tmp_path,
    )
    engine.task_backend.repository_workspace = FakeRepositoryWorkspace()

    processed = engine.run_once()

    next_task = db_connection_mock.tasks[12]
    assert processed is True
    assert db_connection_mock.tasks[11]["status"] == "succeeded"
    assert db_connection_mock.tasks[10]["status"] == "running"
    assert db_connection_mock.tasks[10]["payload_json"]["orchestration"]["current_phase"] == 1
    assert next_task["task_type"] == "phase1_design"
    assert next_task["phase"] == 1
    assert next_task["status"] == "queued"
    assert next_task["parent_task_id"] == 11
    assert next_task["root_task_id"] == 10
    assert next_task["workspace_path"] == str(tmp_path / "10")
    assert next_task["target_repo"] == "example/project"
    assert next_task["target_ref"] == "main"
    assert next_task["working_branch"] == "mn2/10/phase0"
    assert db_connection_mock.tasks[11]["payload_json"]["orchestration"]["llm_model"] == "gpt-5.4-mini"
    assert db_connection_mock.tasks[11]["payload_json"]["orchestration"]["phase_summary"] == "READMEを更新しました"
    assert db_connection_mock.tasks[11]["payload_json"]["orchestration"]["handoff_message"] == "READMEを更新しました"
    assert next_task["payload_json"]["orchestration"]["llm_model"] == "gpt-5.4-mini"
    assert next_task["payload_json"]["orchestration"]["phase_summary"] == "READMEを更新しました"
    assert next_task["payload_json"]["orchestration"]["handoff_message"] == "READMEを更新しました"
    assert any(log["event_type"] == "phase_task_enqueued" for log in db_connection_mock.logs)


def test_worker_advances_orchestrated_phase_task_on_no_change(db_connection_mock, tmp_path):
    db_connection_mock.tasks = {
        20: {
            "id": 20,
            "parent_task_id": None,
            "root_task_id": 20,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "20"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/20/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 0}},
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        21: {
            "id": 21,
            "parent_task_id": 20,
            "root_task_id": 20,
            "task_type": "phase0_brainstorm",
            "phase": 0,
            "status": "queued",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "20"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/20/phase0",
            "payload_json": {"task": "README を更新", "instruction": "README を更新する", "phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 0}},
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 22

    class FakeLLMClient:
        model = "gpt-5.4-mini"

        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "READMEを確認しました"

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "20" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "20"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "20" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "20" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "20" / "patches"),
            }

        def commit_and_push(self, **kwargs):
            raise CommitPushError("phase_edit_no_changes")

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=FakeLLMClient(),
        workspace_root=tmp_path,
    )
    engine.task_backend.repository_workspace = FakeRepositoryWorkspace()

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[21]["status"] == "succeeded"
    assert db_connection_mock.tasks[20]["status"] == "running"
    assert db_connection_mock.tasks[22]["task_type"] == "phase1_design"
    assert db_connection_mock.tasks[21]["payload_json"]["orchestration"]["llm_model"] == "gpt-5.4-mini"
    assert db_connection_mock.tasks[21]["payload_json"]["orchestration"]["phase_summary"] == "READMEを確認しました"
    assert db_connection_mock.tasks[21]["payload_json"]["orchestration"]["handoff_message"] == "READMEを確認しました"
    assert db_connection_mock.tasks[22]["payload_json"]["orchestration"]["llm_model"] == "gpt-5.4-mini"
    assert any(log["event_type"] == "phase_edit_no_changes" for log in db_connection_mock.logs)


def test_worker_phase5_audit_parses_rejected_review_and_requeues_phase4(db_connection_mock, tmp_path):
    db_connection_mock.tasks = {
        70: {
            "id": 70,
            "parent_task_id": None,
            "root_task_id": 70,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "70"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/70/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0}},
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        71: {
            "id": 71,
            "parent_task_id": 70,
            "root_task_id": 70,
            "task_type": "phase5_audit",
            "phase": 5,
            "status": "queued",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "70"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/70/phase0",
            "payload_json": {
                "task": "監査",
                "instruction": "レビュー結果を返す",
                "phase_flow": [0, 1, 2, 3, 4, 5],
                "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0},
            },
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 72

    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "REVIEW RESULT: REJECTED\nfix failing assertions"

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "70" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "70"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "70" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "70" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "70" / "patches"),
            }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=FakeLLMClient(),
        workspace_root=tmp_path,
    )
    engine.task_backend.repository_workspace = FakeRepositoryWorkspace()

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[71]["status"] == "succeeded"
    assert db_connection_mock.tasks[70]["status"] == "running"
    assert db_connection_mock.tasks[70]["payload_json"]["orchestration"]["current_phase"] == 4
    assert db_connection_mock.tasks[72]["task_type"] == "phase4_impl"
    assert db_connection_mock.tasks[72]["phase"] == 4
    assert db_connection_mock.tasks[72]["status"] == "queued"
    assert any(log["event_type"] == "phase_rework_enqueued" for log in db_connection_mock.logs)


def test_worker_phase5_audit_treats_not_approved_and_requeues_phase4(db_connection_mock, tmp_path):
    db_connection_mock.tasks = {
        73: {
            "id": 73,
            "parent_task_id": None,
            "root_task_id": 73,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "73"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/73/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0}},
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        74: {
            "id": 74,
            "parent_task_id": 73,
            "root_task_id": 73,
            "task_type": "phase5_audit",
            "phase": 5,
            "status": "queued",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "73"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/73/phase0",
            "payload_json": {
                "task": "監査",
                "instruction": "レビュー結果を返す",
                "phase_flow": [0, 1, 2, 3, 4, 5],
                "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0},
            },
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 75

    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "NOT APPROVED. REJECTED\nneeds corrections"

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "73" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "73"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "73" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "73" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "73" / "patches"),
            }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=FakeLLMClient(),
        workspace_root=tmp_path,
    )
    engine.task_backend.repository_workspace = FakeRepositoryWorkspace()

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[74]["status"] == "succeeded"
    assert db_connection_mock.tasks[73]["status"] == "running"
    assert db_connection_mock.tasks[73]["payload_json"]["orchestration"]["current_phase"] == 4
    assert db_connection_mock.tasks[75]["task_type"] == "phase4_impl"
    assert db_connection_mock.tasks[75]["status"] == "queued"
    assert any(log["event_type"] == "phase_rework_enqueued" for log in db_connection_mock.logs)


def test_worker_phase5_audit_parses_approved_and_promotes_root_to_waiting_approval(db_connection_mock, tmp_path):
    db_connection_mock.tasks = {
        76: {
            "id": 76,
            "parent_task_id": None,
            "root_task_id": 76,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "76"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/76/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0}},
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        77: {
            "id": 77,
            "parent_task_id": 76,
            "root_task_id": 76,
            "task_type": "phase5_audit",
            "phase": 5,
            "status": "queued",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": str(tmp_path / "76"),
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/76/phase0",
            "payload_json": {
                "task": "監査",
                "instruction": "レビュー結果を返す",
                "phase_flow": [0, 1, 2, 3, 4, 5],
                "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0},
            },
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 78

    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "NOT REJECTED. APPROVED\nlooks good"

    class FakeRepositoryWorkspace:
        def prepare_repository(self, **kwargs):
            repo_path = tmp_path / "76" / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            return {
                "workspace_path": str(tmp_path / "76"),
                "repo_path": str(repo_path),
                "artifacts_path": str(tmp_path / "76" / "artifacts"),
                "docs_snapshot_path": str(tmp_path / "76" / "system_docs_snapshot"),
                "patches_path": str(tmp_path / "76" / "patches"),
            }

    engine = WorkerEngine(
        db_connection_mock,
        service_name="brain",
        worker_name="worker-brain-1",
        llm_client=FakeLLMClient(),
        workspace_root=tmp_path,
    )
    engine.task_backend.repository_workspace = FakeRepositoryWorkspace()

    processed = engine.run_once()

    assert processed is True
    assert db_connection_mock.tasks[77]["status"] == "succeeded"
    assert db_connection_mock.tasks[76]["status"] == "waiting_approval"
    assert db_connection_mock.tasks[76]["payload_json"]["orchestration"]["final_review_state"] == "approved"
    assert 78 not in db_connection_mock.tasks
    assert any(log["event_type"] == "root_task_promoted_waiting_approval" for log in db_connection_mock.logs)


def test_review_state_parsers_treat_not_approved_as_rejected(db_connection_mock):
    backend = MariaDBTaskBackend(db_connection_mock)

    assert backend._parse_review_state("NOT APPROVED") == "rejected"
    assert PhaseOrchestrator._normalize_review_state(None, "NOT APPROVED") == "rejected"
    assert PhaseOrchestrator._normalize_review_state(None, "NOT APPROVED. REJECTED") == "rejected"
    assert backend._parse_review_state("NOT REJECTED. APPROVED") == "approved"
    assert PhaseOrchestrator._normalize_review_state(None, "NOT REJECTED. APPROVED") == "approved"


def test_phase_orchestrator_deduplicates_when_next_phase_task_is_already_active(db_connection_mock):
    db_connection_mock.tasks = {
        80: {
            "id": 80,
            "parent_task_id": None,
            "root_task_id": 80,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/80",
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/80/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 0}},
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        81: {
            "id": 81,
            "parent_task_id": 80,
            "root_task_id": 80,
            "task_type": "phase0_brainstorm",
            "phase": 0,
            "status": "succeeded",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/80",
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/80/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 0}},
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
        82: {
            "id": 82,
            "parent_task_id": 81,
            "root_task_id": 80,
            "task_type": "phase1_design",
            "phase": 1,
            "status": "queued",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/80",
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/80/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 1}},
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 83
    orchestrator = PhaseOrchestrator(MariaDBTaskBackend(db_connection_mock).db)

    handled = orchestrator.handle_phase_completion(
        orchestrator.db.select_orchestration_task_for_update(81),
        service_name="brain",
        worker_name="worker-brain-1",
        result_summary="phase0 done",
    )

    assert handled is True
    assert 83 not in db_connection_mock.tasks
    assert db_connection_mock.tasks[80]["payload_json"]["orchestration"]["current_phase"] == 0
    assert any(log["event_type"] == "phase_task_deduplicated" for log in db_connection_mock.logs)


def test_phase_orchestrator_blocks_root_when_rework_limit_exceeded(db_connection_mock):
    db_connection_mock.tasks = {
        90: {
            "id": 90,
            "parent_task_id": None,
            "root_task_id": 90,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/90",
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/90/phase0",
            "payload_json": {
                "phase_flow": [0, 1, 2, 3, 4, 5],
                "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": DEFAULT_REWORK_LIMIT},
            },
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        91: {
            "id": 91,
            "parent_task_id": 90,
            "root_task_id": 90,
            "task_type": "phase5_audit",
            "phase": 5,
            "status": "succeeded",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/90",
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/90/phase0",
            "payload_json": {
                "phase_flow": [0, 1, 2, 3, 4, 5],
                "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": DEFAULT_REWORK_LIMIT},
            },
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 92
    orchestrator = PhaseOrchestrator(MariaDBTaskBackend(db_connection_mock).db)

    handled = orchestrator.handle_phase_completion(
        orchestrator.db.select_orchestration_task_for_update(91),
        service_name="brain",
        worker_name="worker-brain-1",
        review_state="rejected",
        audit_feedback="still failing",
    )

    assert handled is True
    assert db_connection_mock.tasks[90]["status"] == "blocked"
    assert db_connection_mock.tasks[90]["payload_json"]["orchestration"]["failure_reason"] == "phase_rework_limit_exceeded"
    assert 92 not in db_connection_mock.tasks
    assert any(log["event_type"] == "root_task_blocked" for log in db_connection_mock.logs)


def test_phase_orchestrator_requeues_phase4_when_phase5_rejected(db_connection_mock):
    db_connection_mock.tasks = {
        30: {
            "id": 30,
            "parent_task_id": None,
            "root_task_id": 30,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/30",
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/30/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0}},
            "approval_required": True,
            "result_summary_md": None,
            "started_at": None,
        },
        31: {
            "id": 31,
            "parent_task_id": 30,
            "root_task_id": 30,
            "task_type": "phase5_audit",
            "phase": 5,
            "status": "succeeded",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/30",
            "target_repo": "example/project",
            "target_ref": "main",
            "working_branch": "mn2/30/phase0",
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5, "phase_attempt": 0}},
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    db_connection_mock._next_task_id = 32
    orchestrator = PhaseOrchestrator(MariaDBTaskBackend(db_connection_mock).db)

    handled = orchestrator.handle_phase_completion(
        orchestrator.db.select_orchestration_task_for_update(31),
        service_name="brain",
        worker_name="worker-brain-1",
        review_state="rejected",
        audit_feedback="fix failing assertions",
    )

    rework_task = db_connection_mock.tasks[32]
    assert handled is True
    assert db_connection_mock.tasks[30]["status"] == "running"
    assert db_connection_mock.tasks[30]["payload_json"]["orchestration"]["current_phase"] == 4
    assert db_connection_mock.tasks[30]["payload_json"]["orchestration"]["final_review_state"] == "rejected"
    assert rework_task["task_type"] == "phase4_impl"
    assert rework_task["phase"] == 4
    assert rework_task["status"] == "queued"
    assert rework_task["parent_task_id"] == 31
    assert rework_task["payload_json"]["orchestration"]["audit_feedback"] == "fix failing assertions"
    assert any(log["event_type"] == "phase_rework_enqueued" for log in db_connection_mock.logs)


def test_phase_orchestrator_promotes_root_to_succeeded_and_blocks_on_failure(db_connection_mock):
    db_connection_mock.tasks = {
        40: {
            "id": 40,
            "parent_task_id": None,
            "root_task_id": 40,
            "task_type": "phase_orchestration_root",
            "phase": 0,
            "status": "running",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/40",
            "target_repo": None,
            "target_ref": None,
            "working_branch": None,
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5}},
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
        41: {
            "id": 41,
            "parent_task_id": 40,
            "root_task_id": 40,
            "task_type": "phase5_audit",
            "phase": 5,
            "status": "succeeded",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/40",
            "target_repo": None,
            "target_ref": None,
            "working_branch": None,
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 5}},
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
        42: {
            "id": 42,
            "parent_task_id": 40,
            "root_task_id": 40,
            "task_type": "phase2_test_design",
            "phase": 2,
            "status": "blocked",
            "lease_owner": None,
            "lease_expires_at": None,
            "assigned_service": "brain",
            "priority": 0,
            "workspace_path": "/workspace/40",
            "target_repo": None,
            "target_ref": None,
            "working_branch": None,
            "payload_json": {"phase_flow": [0, 1, 2, 3, 4, 5], "orchestration": {"phase_flow": [0, 1, 2, 3, 4, 5], "current_phase": 2}},
            "approval_required": False,
            "result_summary_md": None,
            "started_at": None,
        },
    }
    orchestrator = PhaseOrchestrator(MariaDBTaskBackend(db_connection_mock).db)

    approved = orchestrator.handle_phase_completion(
        orchestrator.db.select_orchestration_task_for_update(41),
        service_name="brain",
        worker_name="worker-brain-1",
        review_state="approved",
    )

    assert approved is True
    assert db_connection_mock.tasks[40]["status"] == "succeeded"
    assert db_connection_mock.tasks[40]["payload_json"]["orchestration"]["final_review_state"] == "approved"
    assert any(log["event_type"] == "root_task_promoted_succeeded" for log in db_connection_mock.logs)

    db_connection_mock.tasks[40]["status"] = "running"
    blocked = orchestrator.handle_phase_blocked(
        orchestrator.db.select_orchestration_task_for_update(42),
        service_name="brain",
        worker_name="worker-brain-1",
        reason="repository_prepare_failed",
    )

    assert blocked is True
    assert db_connection_mock.tasks[40]["status"] == "blocked"
    assert any(log["event_type"] == "root_task_blocked" for log in db_connection_mock.logs)


def test_worker_generates_llm_response_and_marks_local_task_succeeded_without_approval(db_connection_mock, tmp_path):
    class FakeLLMClient:
        def generate(self, prompt: str, metadata: dict | None = None) -> str:
            return "READMEを更新しました\n\n- ローカル repo を更新しました。"

    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "local-repo")
    db_connection_mock.tasks[1]["target_repo"] = None
    db_connection_mock.tasks[1]["target_ref"] = None
    db_connection_mock.tasks[1]["working_branch"] = None
    db_connection_mock.tasks[1]["approval_required"] = False
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

    artifact_path = tmp_path / "local-repo" / "artifacts" / "llm_response.md"
    assert processed is True
    assert db_connection_mock.tasks[1]["status"] == "succeeded"
    assert db_connection_mock.tasks[1]["result_summary_md"] == "READMEを更新しました"
    assert artifact_path.exists()
    assert any(log["event_type"] == "llm_generation_succeeded" for log in db_connection_mock.logs)
    assert not any(log["event_type"] == "git_commit_succeeded" for log in db_connection_mock.logs)
    assert not any(log["event_type"] == "git_push_succeeded" for log in db_connection_mock.logs)


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


def test_backend_marks_waiting_approval_when_direct_edit_produces_no_changes_for_approval_task(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "queued"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
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
    assert db_connection_mock.tasks[1]["status"] == "waiting_approval"
    assert any(log["event_type"] == "phase_edit_no_changes" for log in db_connection_mock.logs)
    assert any(log["event_type"] == "llm_generation_succeeded" for log in db_connection_mock.logs)
    assert not any(log["event_type"] == "git_commit_succeeded" for log in db_connection_mock.logs)


def test_backend_marks_succeeded_when_direct_edit_produces_no_changes_for_non_approval_task(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "queued"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = False
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
    assert db_connection_mock.tasks[1]["status"] == "succeeded"
    assert any(log["event_type"] == "phase_edit_no_changes" for log in db_connection_mock.logs)
    assert any(log["event_type"] == "llm_generation_succeeded" for log in db_connection_mock.logs)
    assert not any(log["event_type"] == "git_commit_succeeded" for log in db_connection_mock.logs)


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

