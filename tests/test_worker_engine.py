import logging
from pathlib import Path

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
