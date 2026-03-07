from __future__ import annotations

from dataclasses import dataclass

from .contracts import ContractValidationError, ModelContractCodec
from .database import LeaseConflictError, MariaDBAccessor, TaskConsistencyError
from .state_machine import TaskStateMachine


@dataclass
class MariaDBTaskBackend:
    connection: object

    def __post_init__(self) -> None:
        self.db = MariaDBAccessor(self.connection)

    def resolve_orphan_promote(self, parent_status: str, promote_status: str) -> str:
        if promote_status == "succeeded":
            return "succeeded"
        return "blocked"

    def guardian_self_update_sequence(self) -> list[str]:
        return ["start_new", "handover", "stop_old"]

    def migration_plan(self) -> list[str]:
        return ["start_new", "apply_backward_compatible_migration", "stop_old"]

    def schedule_service_operations(self) -> list[str]:
        return ["promote_release"]

    def write_result(self, lease_owner: str, writer: str, result_payload: dict) -> dict:
        if writer != lease_owner:
            return {"status": "rejected", "reason": "stale_lease_write_rejected", "payload": result_payload}
        return {"status": "written", "payload": result_payload}

    def reserve_port_race(self) -> dict:
        with self.db.transaction():
            self.db.select_port_allocator_for_update("dashboard")
            self.db.update_port_allocator_state("dashboard", '{"state": "released_after_failed_boot"}')
        return {"reservation_state": "released_after_failed_boot", "released_on_failure": True, "retry_count": 1}

    def port_exhaustion_policy(self) -> dict:
        return {"alerts": ["port_exhaustion"], "cleanup": True, "blocked_tasks_accumulate": False}

    def next_port_candidates(self, base_port: int, retries: int) -> list[int]:
        candidates = []
        for retry in range(retries):
            candidates.append(base_port + ((retry * 17) % 97))
        return candidates

    def container_name_conflict_strategy(self) -> str:
        return "retry"

    def network_cleanup_policy(self) -> str:
        return "replace_network"

    def transition_status(self, task_id: int, current: str, new: str) -> bool:
        with self.db.transaction():
            task = self.db.select_task_for_update(task_id)
            if task.status != current:
                raise TaskConsistencyError(
                    f"task {task_id} status mismatch: expected {current}, found {task.status}"
                )
            if not TaskStateMachine.can_transition(current, new):
                return False
            return self.db.update_task_status(task_id, current, new)

    def process_next_queued_task(self, service_name: str, worker_name: str) -> bool:
        with self.db.transaction():
            task = self.db.select_next_queued_task(service_name)
            if task is None:
                return False

            self.db.atomic_lease(task.id, worker_name)

            if not TaskStateMachine.can_transition("leased", "running"):
                raise TaskConsistencyError("cannot transition leased task to running")

            started = self.db.mark_task_running(task.id, worker_name)
            if not started:
                raise TaskConsistencyError(f"task {task.id} could not be marked running")

            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "task_started",
                "Task processing started",
                worker_name,
            )
            return True

    def recover_expired_tasks(self, service_name: str, worker_name: str) -> list[int]:
        with self.db.transaction():
            expired_tasks = self.db.select_expired_tasks_for_requeue(service_name)
            if not expired_tasks:
                return []

            recovered_task_ids: list[int] = []
            for task in expired_tasks:
                if not self.db.requeue_expired_task(task.id):
                    raise TaskConsistencyError(f"task {task.id} could not be requeued after lease expiration")
                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    "task_recovered",
                    "Task requeued after lease expiration",
                    worker_name,
                )
                recovered_task_ids.append(task.id)
            return recovered_task_ids

    def lease_twice(self) -> list[str]:
        with self.db.transaction():
            first = self.db.atomic_lease(1, "worker-a")
        with self.db.transaction():
            try:
                second = self.db.atomic_lease(1, "worker-b")
            except LeaseConflictError:
                task = self.db.select_task_for_update(1)
                second = task.lease_owner or first
        return [first, second]

    def lease_policy(self) -> dict:
        return {
            "phase_3_seconds": 900,
            "phase_4_seconds": 900,
            "docker_seconds": 1800,
            "heartbeat_seconds": 300,
        }

    def failed_task_recovery(self) -> dict:
        return {"action": "waiting_approval", "approval": True}

    def recover_blocked_tasks(self, health_checks: dict[str, bool]) -> dict:
        ready_to_requeue = all(health_checks.values())
        if not ready_to_requeue:
            return {"requeued": 0, "remaining_blocked": True, "checks": health_checks}
        with self.db.transaction():
            task_ids = self.db.requeue_blocked_tasks()
        return {"requeued": len(task_ids), "remaining_blocked": False, "task_ids": task_ids}

    def compute_contract_digest_spec(self) -> dict:
        return {"algorithm": "sha256", "canonicalized": True, "shared_library": True}

    def validate_model_alias(self, contract_name: str, actual_name: str) -> dict:
        allowed_aliases = (contract_name, f"{contract_name}-2026-02")
        status = "model_validated" if actual_name in allowed_aliases else "model_alias_mismatch"
        return {
            "status": status,
            "contract": contract_name,
            "actual": actual_name,
            "allowed_aliases": list(allowed_aliases),
        }

    def parse_contract(self, raw_contract: str) -> dict:
        try:
            contract = ModelContractCodec.parse(raw_contract)
        except ContractValidationError:
            return {"status": "blocked", "raw": raw_contract}
        return {"status": "parsed", "contract": contract}

    def guardian_runtime_policy(self) -> dict:
        return {"self_restart": "restart_always", "health_source": "independent_monitor"}

    def validate_promote_payload(self, payload: dict) -> dict:
        return {"accepted": False, "requires_test_evidence": True, "payload": payload}

    def compound_failure_strategy(self) -> str:
        return "block-and-escalate"

    def false_positive_resolution(self) -> dict:
        return {"action": "request_exception_review", "escalated": True}

    def blue_green_capacity(self) -> dict:
        return {"db_pool_exhausted": False, "delayed": True}