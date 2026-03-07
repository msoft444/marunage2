from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable

from .database import TaskConsistencyError
from .task_backend import MariaDBTaskBackend


LOGGER = logging.getLogger("marunage2.worker_engine")


@dataclass
class WorkerEngine:
    connection: object
    service_name: str
    worker_name: str
    connection_factory: Callable[[], object] | None = None

    def __post_init__(self) -> None:
        self._bind_connection(self.connection)

    def _bind_connection(self, connection: object) -> None:
        self.connection = connection
        self.task_backend = MariaDBTaskBackend(connection)

    def ensure_connection(self) -> bool:
        ping = getattr(self.connection, "ping", None)
        ping_error: Exception | None = None
        if callable(ping):
            try:
                ping(reconnect=True)
                return False
            except Exception as exc:
                ping_error = exc

        if self.connection_factory is None:
            if ping_error is not None:
                raise ping_error
            return False

        close = getattr(self.connection, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                LOGGER.debug("failed closing stale database connection", exc_info=True)

        self._bind_connection(self.connection_factory())
        LOGGER.info("%s database reconnection succeeded", self.service_name)
        return True

    def recover_expired_tasks(self) -> list[int]:
        recovered_task_ids = self.task_backend.recover_expired_tasks(self.service_name, self.worker_name)
        if recovered_task_ids:
            LOGGER.info(
                "%s recovered expired tasks: %s",
                self.service_name,
                ", ".join(str(task_id) for task_id in recovered_task_ids),
            )
        return recovered_task_ids

    def run_once(self) -> bool:
        return self.task_backend.process_next_queued_task(self.service_name, self.worker_name)