from __future__ import annotations

import os

import pytest

from backend.database import LeaseConflictError, MariaDBAccessor


mariadb = pytest.importorskip("mariadb")

pytestmark = pytest.mark.integration


def _integration_enabled() -> bool:
    return os.getenv("MARIADB_INTEGRATION") == "1"


@pytest.fixture
def mariadb_connection():
    if not _integration_enabled():
        pytest.skip("set MARIADB_INTEGRATION=1 to run live MariaDB integration tests")

    connection = mariadb.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        autocommit=False,
    )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def clean_tasks(mariadb_connection):
    cursor = mariadb_connection.cursor()
    cursor.execute("DELETE FROM messages")
    cursor.execute("DELETE FROM logs")
    cursor.execute("DELETE FROM tasks")
    mariadb_connection.commit()
    cursor.close()


def _insert_task(mariadb_connection, task_id: int, status: str) -> None:
    cursor = mariadb_connection.cursor()
    cursor.execute(
        """
        INSERT INTO tasks (
            id, root_task_id, task_type, phase, status,
            requested_by_role, assigned_role, assigned_service, retry_count, max_retry
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (task_id, task_id, "integration-check", 4, status, "guardian", "brain", "brain", 0, 3),
    )
    mariadb_connection.commit()
    cursor.close()


def test_mariadb_accessor_leases_once(mariadb_connection):
    _insert_task(mariadb_connection, 1001, "queued")
    accessor = MariaDBAccessor(mariadb_connection)

    with accessor.transaction():
        owner = accessor.atomic_lease(1001, "worker-a")

    assert owner == "worker-a"
    cursor = mariadb_connection.cursor(dictionary=True)
    cursor.execute("SELECT lease_expires_at FROM tasks WHERE id = %s", (1001,))
    row = cursor.fetchone()
    cursor.close()
    assert row["lease_expires_at"] is not None

    with pytest.raises(LeaseConflictError):
        with accessor.transaction():
            accessor.atomic_lease(1001, "worker-b")


def test_mariadb_accessor_transitions_real_row(mariadb_connection):
    _insert_task(mariadb_connection, 1002, "queued")
    accessor = MariaDBAccessor(mariadb_connection)

    with accessor.transaction():
        row = accessor.select_task_for_update(1002)
        updated = accessor.update_task_status(1002, row.status, "leased")

    assert row.status == "queued"
    assert updated is True


def test_port_allocator_row_is_loaded_from_real_mariadb(mariadb_connection):
    accessor = MariaDBAccessor(mariadb_connection)

    with accessor.transaction():
        row = accessor.select_port_allocator_for_update("dashboard")

    assert row["service_name"] == "dashboard"
