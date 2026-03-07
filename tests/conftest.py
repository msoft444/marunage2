import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backend import MariaDBTaskBackend
from librarian import LibrarianService
from security import SafeFileOps, SecretScanner, SecureCommandRunner, SecureDashboard, WorkspaceSandbox


@dataclass
class FakeCursor:
    rows: list[dict]
    rowcount: int
    task_ids: list[int] | None = None

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeMariaDBConnection:
    def __init__(self):
        self.tasks = {
            1: {
                "id": 1,
                "root_task_id": 1,
                "status": "queued",
                "lease_owner": None,
                "lease_expires_at": None,
                "assigned_service": "brain",
                "priority": 0,
                "started_at": None,
            },
            2: {
                "id": 2,
                "root_task_id": 2,
                "status": "blocked",
                "lease_owner": None,
                "lease_expires_at": None,
                "assigned_service": "brain",
                "priority": 0,
                "started_at": None,
            },
            3: {
                "id": 3,
                "root_task_id": 3,
                "status": "blocked",
                "lease_owner": None,
                "lease_expires_at": None,
                "assigned_service": "brain",
                "priority": 0,
                "started_at": None,
            },
        }
        self.port_allocator = {
            "dashboard": {
                "id": 1,
                "service_name": "dashboard",
                "last_allocated_port": None,
                "reservation_state_json": "{}",
            }
        }
        self.statements: list[tuple[str, tuple]] = []
        self.execute = Mock(side_effect=self._execute)
        self.begin = Mock()
        self.commit = Mock()
        self.rollback = Mock()
        self.close = Mock()
        self.ping = Mock()

    def _execute(self, query, params=()):
        normalized = " ".join(str(query).split())
        self.statements.append((normalized, tuple(params)))
        if normalized == "SELECT 1":
            return FakeCursor([{"value": 1}], 1)
        if "FROM tasks WHERE id = %s FOR UPDATE" in normalized:
            task = None
            selected = self.tasks.get(params[0])
            if selected is not None:
                task = {
                    "id": selected["id"],
                    "status": selected["status"],
                    "lease_owner": selected["lease_owner"],
                    "lease_expires_at": selected["lease_expires_at"],
                }
            return FakeCursor([task] if task else [], 1 if task else 0)
        if "FROM tasks WHERE assigned_service = %s AND status = 'queued'" in normalized:
            service_name = params[0]
            candidates = [
                task for task in self.tasks.values() if task["assigned_service"] == service_name and task["status"] == "queued"
            ]
            candidates.sort(key=lambda task: (-task["priority"], task["id"]))
            task = None
            if candidates:
                selected = candidates[0]
                task = {
                    "id": selected["id"],
                    "root_task_id": selected["root_task_id"],
                    "status": selected["status"],
                    "assigned_service": selected["assigned_service"],
                    "priority": selected["priority"],
                }
            return FakeCursor([task] if task else [], 1 if task else 0)
        if "WHERE assigned_service = %s AND status IN ('leased', 'running')" in normalized:
            service_name = params[0]
            rows = [
                {
                    "id": task["id"],
                    "root_task_id": task["root_task_id"],
                }
                for task in self.tasks.values()
                if task["assigned_service"] == service_name
                and task["status"] in {"leased", "running"}
                and task["lease_expires_at"] == "expired"
            ]
            rows.sort(key=lambda task: task["id"])
            return FakeCursor(rows, len(rows))
        if "FROM port_allocator WHERE service_name = %s FOR UPDATE" in normalized:
            row = self.port_allocator.get(params[0])
            return FakeCursor([row] if row else [], 1 if row else 0)
        if normalized.startswith("UPDATE tasks SET status = 'leased', lease_owner = %s, lease_expires_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 1 HOUR)"):
            lease_owner, task_id = params
            task = self.tasks[task_id]
            if task["status"] != "queued":
                return FakeCursor([], 0)
            task["status"] = "leased"
            task["lease_owner"] = lease_owner
            task["lease_expires_at"] = "leased-until-later"
            return FakeCursor([], 1)
        if normalized.startswith("UPDATE tasks SET status = %s WHERE id = %s AND status = %s"):
            new_status, task_id, current_status = params
            task = self.tasks[task_id]
            if task["status"] != current_status:
                return FakeCursor([], 0)
            task["status"] = new_status
            return FakeCursor([], 1)
        if normalized.startswith("UPDATE tasks SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = %s AND status = 'leased' AND lease_owner = %s"):
            task_id, lease_owner = params
            task = self.tasks[task_id]
            if task["status"] != "leased" or task["lease_owner"] != lease_owner:
                return FakeCursor([], 0)
            task["status"] = "running"
            task["started_at"] = "now"
            return FakeCursor([], 1)
        if normalized.startswith("UPDATE tasks SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, started_at = NULL WHERE id = %s AND status IN ('leased', 'running')"):
            task_id = params[0]
            task = self.tasks[task_id]
            if task["status"] not in {"leased", "running"}:
                return FakeCursor([], 0)
            task["status"] = "queued"
            task["lease_owner"] = None
            task["lease_expires_at"] = None
            task["started_at"] = None
            return FakeCursor([], 1)
        if normalized == "UPDATE tasks SET status = 'queued' WHERE status = 'blocked'":
            task_ids = [task_id for task_id, task in self.tasks.items() if task["status"] == "blocked"]
            for task_id in task_ids:
                self.tasks[task_id]["status"] = "queued"
            return FakeCursor([], len(task_ids), task_ids=task_ids)
        if normalized.startswith("UPDATE port_allocator SET reservation_state_json = %s WHERE service_name = %s"):
            reservation_state, service_name = params
            self.port_allocator[service_name]["reservation_state_json"] = reservation_state
            return FakeCursor([], 1)
        if normalized.startswith("INSERT INTO logs"):
            return FakeCursor([], 1)
        return FakeCursor([], 1)


@pytest.fixture
def db_settings(monkeypatch):
    monkeypatch.setenv("DB_HOST", os.getenv("DB_HOST", "127.0.0.1"))
    monkeypatch.setenv("DB_PORT", os.getenv("DB_PORT", "3306"))
    monkeypatch.setenv("DB_NAME", os.getenv("DB_NAME", "marunage2"))
    monkeypatch.setenv("DB_USER", os.getenv("DB_USER", "marunage"))
    monkeypatch.setenv("DB_PASSWORD", os.getenv("DB_PASSWORD", "dummy-password"))
    return {
        "host": os.environ["DB_HOST"],
        "port": os.environ["DB_PORT"],
        "name": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


@pytest.fixture
def db_connection_mock(db_settings):
    connection = FakeMariaDBConnection()
    connection.settings = db_settings
    return connection


@pytest.fixture
def docker_socket_path(monkeypatch):
    monkeypatch.setenv("DOCKER_SOCK", os.getenv("DOCKER_SOCK", "/var/run/docker.sock"))
    return os.environ["DOCKER_SOCK"]


@pytest.fixture
def docker_client_mock(docker_socket_path):
    client = Mock(name="docker_client")
    client.socket_path = docker_socket_path
    return client


@pytest.fixture
def task_backend(db_connection_mock):
    return MariaDBTaskBackend(db_connection_mock)


@pytest.fixture
def secret_scanner():
    return SecretScanner()


@pytest.fixture
def sandbox(tmp_path):
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    return WorkspaceSandbox(str(workspace_root))


@pytest.fixture
def dashboard():
    return SecureDashboard()


@pytest.fixture
def file_ops(tmp_path):
    return SafeFileOps(workspace_root=tmp_path / "file-ops")


@pytest.fixture
def command_runner():
    return SecureCommandRunner()


@pytest.fixture
def librarian(db_connection_mock, docker_client_mock, tmp_path):
    return LibrarianService(
        db_connection=db_connection_mock,
        docker_client=docker_client_mock,
        wal_path=tmp_path / "librarian.wal",
    )