from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


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


class MariaDBAccessor:
    def __init__(self, connection: Any):
        self.connection = connection
        self._transaction_depth = 0

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

    def atomic_lease(self, task_id: int, lease_owner: str) -> str:
        query = (
            "UPDATE tasks SET status = 'leased', lease_owner = %s "
            "WHERE id = %s AND status = 'queued'"
        )
        cursor = self._execute(query, (lease_owner, task_id))
        if cursor.rowcount == 0:
            raise LeaseConflictError(f"task {task_id} is already leased")
        return lease_owner

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