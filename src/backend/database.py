from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from security.sandbox import WorkspaceSandbox


class LeaseConflictError(RuntimeError):
    pass


class TaskConsistencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskRow:
    id: int
    status: str
    lease_owner: str | None
    lease_expires_at: str | None


@dataclass(frozen=True)
class QueueTaskRow:
    id: int
    parent_task_id: int | None
    root_task_id: int
    task_type: str
    phase: int
    status: str
    assigned_service: str
    priority: int
    payload_json: dict[str, Any] | None = None
    workspace_path: str | None = None
    target_repo: str | None = None
    target_ref: str | None = None
    working_branch: str | None = None
    approval_required: bool = False
    result_summary_md: str | None = None


@dataclass(frozen=True)
class RecoverableTaskRow:
    id: int
    root_task_id: int


@dataclass(frozen=True)
class ArtifactApplyTaskRow:
    id: int
    root_task_id: int
    status: str
    payload_json: dict[str, Any] | None = None
    workspace_path: str | None = None
    target_repo: str | None = None
    target_ref: str | None = None
    working_branch: str | None = None
    result_summary_md: str | None = None


class MariaDBAccessor:
    def __init__(self, connection: Any, workspace_root: str | Path = "/workspace"):
        self.connection = connection
        self._transaction_depth = 0
        self.workspace_sandbox = WorkspaceSandbox(str(workspace_root))

    @contextmanager
    def transaction(self):
        begin = getattr(self.connection, "begin", None)
        start_transaction = getattr(self.connection, "start_transaction", None)
        commit = getattr(self.connection, "commit", None)
        rollback = getattr(self.connection, "rollback", None)
        started_here = self._transaction_depth == 0
        if started_here:
            if callable(begin):
                begin()
            elif callable(start_transaction):
                start_transaction()
        self._transaction_depth += 1
        try:
            yield self.connection
        except Exception:
            self._transaction_depth -= 1
            if started_here and callable(rollback):
                rollback()
            raise
        else:
            self._transaction_depth -= 1
            if started_here and callable(commit):
                commit()

    def select_task_for_update(self, task_id: int) -> TaskRow:
        query = (
            "SELECT id, status, lease_owner, lease_expires_at "
            "FROM tasks WHERE id = %s FOR UPDATE"
        )
        cursor = self._execute(query, (task_id,))
        row = self._fetchone_dict(cursor)
        if row is None:
            raise TaskConsistencyError(f"task {task_id} does not exist")
        return TaskRow(**row)

    def select_port_allocator_for_update(self, service_name: str) -> dict[str, Any]:
        query = (
            "SELECT id, service_name, last_allocated_port, reservation_state_json "
            "FROM port_allocator WHERE service_name = %s FOR UPDATE"
        )
        cursor = self._execute(query, (service_name,))
        row = self._fetchone_dict(cursor)
        if row is None:
            raise TaskConsistencyError(f"port allocator {service_name} does not exist")
        return row

    def update_task_status(self, task_id: int, current_status: str, new_status: str) -> bool:
        query = (
            "UPDATE tasks SET status = %s "
            "WHERE id = %s AND status = %s"
        )
        cursor = self._execute(query, (new_status, task_id, current_status))
        return bool(cursor.rowcount)

    def update_task_result(self, task_id: int, current_status: str, new_status: str, result_summary_md: str) -> bool:
        query = (
            "UPDATE tasks SET status = %s, result_summary_md = %s, finished_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND status = %s"
        )
        cursor = self._execute(query, (new_status, result_summary_md, task_id, current_status))
        return bool(cursor.rowcount)

    def select_next_queued_task(self, service_name: str) -> QueueTaskRow | None:
        query = (
            "SELECT id, parent_task_id, root_task_id, task_type, phase, status, assigned_service, priority, payload_json, workspace_path, target_repo, target_ref, working_branch, approval_required, result_summary_md "
            "FROM tasks WHERE assigned_service = %s AND status = 'queued' "
            "ORDER BY priority DESC, created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        cursor = self._execute(query, (service_name,))
        row = self._fetchone_dict(cursor)
        if row is None:
            return None
        payload_json = row.get("payload_json")
        if isinstance(payload_json, str):
            row["payload_json"] = json.loads(payload_json)
        return QueueTaskRow(**row)

    def select_orchestration_task_for_update(self, task_id: int) -> QueueTaskRow:
        query = (
            "SELECT id, parent_task_id, root_task_id, task_type, phase, status, assigned_service, priority, payload_json, workspace_path, target_repo, target_ref, working_branch, approval_required, result_summary_md "
            "FROM tasks WHERE id = %s FOR UPDATE"
        )
        cursor = self._execute(query, (task_id,))
        row = self._fetchone_dict(cursor)
        if row is None:
            raise TaskConsistencyError(f"task {task_id} does not exist")
        payload_json = row.get("payload_json")
        if isinstance(payload_json, str):
            row["payload_json"] = json.loads(payload_json)
        return QueueTaskRow(**row)

    def select_active_phase_task(self, root_task_id: int, phase: int) -> QueueTaskRow | None:
        query = (
            "SELECT id, parent_task_id, root_task_id, task_type, phase, status, assigned_service, priority, payload_json, workspace_path, target_repo, target_ref, working_branch, approval_required, result_summary_md "
            "FROM tasks WHERE root_task_id = %s AND phase = %s "
            "AND status IN ('queued', 'leased', 'running', 'waiting_approval') "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE"
        )
        cursor = self._execute(query, (root_task_id, phase))
        row = self._fetchone_dict(cursor)
        if row is None:
            return None
        payload_json = row.get("payload_json")
        if isinstance(payload_json, str):
            row["payload_json"] = json.loads(payload_json)
        return QueueTaskRow(**row)

    def insert_task(
        self,
        *,
        parent_task_id: int | None,
        root_task_id: int,
        task_type: str,
        phase: int,
        status: str,
        requested_by_role: str,
        assigned_role: str,
        assigned_service: str,
        priority: int,
        workspace_path: str | None,
        target_repo: str | None,
        target_ref: str | None,
        working_branch: str | None,
        payload_json: dict[str, Any] | None,
        retry_count: int,
        max_retry: int,
        approval_required: bool,
    ) -> int:
        query = (
            "INSERT INTO tasks ("
            "parent_task_id, root_task_id, task_type, phase, status, requested_by_role, assigned_role, assigned_service, "
            "priority, workspace_path, target_repo, target_ref, working_branch, payload_json, retry_count, max_retry, approval_required"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        cursor = self._execute(
            query,
            (
                parent_task_id,
                root_task_id,
                task_type,
                phase,
                status,
                requested_by_role,
                assigned_role,
                assigned_service,
                priority,
                workspace_path,
                target_repo,
                target_ref,
                working_branch,
                json.dumps(payload_json, ensure_ascii=False) if payload_json is not None else None,
                retry_count,
                max_retry,
                approval_required,
            ),
        )
        lastrowid = getattr(cursor, "lastrowid", None)
        if lastrowid is None:
            raise TaskConsistencyError("could not determine inserted task id")
        return int(lastrowid)

    def update_task_payload_json(self, task_id: int, payload_json: dict[str, Any]) -> bool:
        query = "UPDATE tasks SET payload_json = %s WHERE id = %s"
        cursor = self._execute(query, (json.dumps(payload_json, ensure_ascii=False), task_id))
        return bool(cursor.rowcount)

    def select_task_workspace_path(self, task_id: int) -> str | None:
        query = "SELECT workspace_path, target_repo FROM tasks WHERE id = %s"
        cursor = self._execute(query, (task_id,))
        row = self._fetchone_dict(cursor)
        if row is None:
            raise TaskConsistencyError(f"task {task_id} does not exist")
        return self.normalize_task_workspace_path(row.get("workspace_path"), row.get("target_repo"))

    def select_task_for_artifact_apply(self, task_id: int) -> ArtifactApplyTaskRow:
        query = (
            "SELECT id, root_task_id, status, payload_json, workspace_path, target_repo, target_ref, working_branch, result_summary_md "
            "FROM tasks WHERE id = %s FOR UPDATE"
        )
        cursor = self._execute(query, (task_id,))
        row = self._fetchone_dict(cursor)
        if row is None:
            raise TaskConsistencyError(f"task {task_id} does not exist")
        payload_json = row.get("payload_json")
        if isinstance(payload_json, str):
            row["payload_json"] = json.loads(payload_json)
        return ArtifactApplyTaskRow(**row)

    def normalize_task_workspace_path(self, workspace_path: str | None, target_repo: str | None = None) -> str | None:
        if workspace_path is None:
            return None
        if not isinstance(workspace_path, str):
            raise TaskConsistencyError("task workspace_path is invalid")
        if not self.workspace_sandbox.validate_control_chars(workspace_path):
            raise TaskConsistencyError("task workspace_path contains control characters")
        try:
            normalized_workspace = Path(workspace_path).resolve(strict=False)
        except RuntimeError as error:
            raise TaskConsistencyError("task workspace_path could not be normalized") from error
        if not self.workspace_sandbox.validate_workspace_path(str(normalized_workspace)):
            raise TaskConsistencyError("task workspace_path escapes workspace root")
        if target_repo:
            normalized_workspace = normalized_workspace / "repo"
            if not self.workspace_sandbox.validate_workspace_path(str(normalized_workspace)):
                raise TaskConsistencyError("task repository path escapes workspace root")
        return str(normalized_workspace)

    def select_expired_tasks_for_requeue(self, service_name: str) -> list[RecoverableTaskRow]:
        query = (
            "SELECT id, root_task_id FROM tasks "
            "WHERE assigned_service = %s "
            "AND status IN ('leased', 'running') "
            "AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at < CURRENT_TIMESTAMP "
            "ORDER BY created_at ASC FOR UPDATE SKIP LOCKED"
        )
        cursor = self._execute(query, (service_name,))
        return [RecoverableTaskRow(**row) for row in self._fetchall_dicts(cursor)]

    def mark_task_running(self, task_id: int, lease_owner: str) -> bool:
        query = (
            "UPDATE tasks SET status = 'running', started_at = CURRENT_TIMESTAMP "
            "WHERE id = %s AND status = 'leased' AND lease_owner = %s"
        )
        cursor = self._execute(query, (task_id, lease_owner))
        return bool(cursor.rowcount)

    def insert_log(self, task_id: int, root_task_id: int, service: str, event_type: str, message: str, trace_id: str) -> bool:
        query = (
            "INSERT INTO logs (task_id, root_task_id, service, component, level, event_type, message, details_json, trace_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )
        cursor = self._execute(
            query,
            (task_id, root_task_id, service, "worker_engine", "INFO", event_type, message, None, trace_id),
        )
        return bool(cursor.rowcount)

    def atomic_lease(self, task_id: int, lease_owner: str) -> str:
        query = (
            "UPDATE tasks SET status = 'leased', lease_owner = %s, "
            "lease_expires_at = DATE_ADD(CURRENT_TIMESTAMP, INTERVAL 1 HOUR) "
            "WHERE id = %s AND status = 'queued'"
        )
        cursor = self._execute(query, (lease_owner, task_id))
        if cursor.rowcount == 0:
            raise LeaseConflictError(f"task {task_id} is already leased")
        return lease_owner

    def requeue_expired_task(self, task_id: int) -> bool:
        query = (
            "UPDATE tasks SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL, started_at = NULL "
            "WHERE id = %s AND status IN ('leased', 'running')"
        )
        cursor = self._execute(query, (task_id,))
        return bool(cursor.rowcount)

    def requeue_blocked_tasks(self) -> list[int]:
        query = "UPDATE tasks SET status = 'queued' WHERE status = 'blocked'"
        cursor = self._execute(query, ())
        task_ids = getattr(cursor, "task_ids", None)
        return list(task_ids or [])

    def update_port_allocator_state(self, service_name: str, reservation_state_json: str) -> bool:
        query = "UPDATE port_allocator SET reservation_state_json = %s WHERE service_name = %s"
        cursor = self._execute(query, (reservation_state_json, service_name))
        return bool(cursor.rowcount)

    def _execute(self, query: str, params: tuple[Any, ...]):
        direct_execute = getattr(self.connection, "execute", None)
        if callable(direct_execute):
            return direct_execute(query, params)

        cursor_factory = getattr(self.connection, "cursor", None)
        if not callable(cursor_factory):
            raise TaskConsistencyError("connection does not provide execute or cursor")

        try:
            cursor = cursor_factory(dictionary=True)
        except TypeError:
            cursor = cursor_factory()
        cursor.execute(query, params)
        return cursor

    def _fetchone_dict(self, cursor) -> dict[str, Any] | None:
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row

        description = getattr(cursor, "description", None)
        if not description:
            raise TaskConsistencyError("cursor did not expose column metadata")
        columns = [column[0] for column in description]
        return dict(zip(columns, row, strict=True))

    def _fetchall_dicts(self, cursor) -> list[dict[str, Any]]:
        fetchall = getattr(cursor, "fetchall", None)
        if callable(fetchall):
            rows = fetchall()
        else:
            first_row = self._fetchone_dict(cursor)
            rows = [] if first_row is None else [first_row]

        if not rows:
            return []
        if isinstance(rows[0], dict):
            return list(rows)

        description = getattr(cursor, "description", None)
        if not description:
            raise TaskConsistencyError("cursor did not expose column metadata")
        columns = [column[0] for column in description]
        return [dict(zip(columns, row, strict=True)) for row in rows]