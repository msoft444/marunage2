import logging

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
