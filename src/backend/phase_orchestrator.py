from __future__ import annotations

from typing import Any

from .database import MariaDBAccessor, QueueTaskRow, TaskConsistencyError


PHASE_TASK_TYPES = {
    0: "phase0_brainstorm",
    1: "phase1_design",
    2: "phase2_test_design",
    3: "phase3_test_impl",
    4: "phase4_impl",
    5: "phase5_audit",
}

TERMINAL_ROOT_STATUSES = {"succeeded", "failed", "cancelled", "blocked"}
DEFAULT_REWORK_LIMIT = 3


class PhaseOrchestrator:
    def __init__(self, db: MariaDBAccessor, rework_limit: int = DEFAULT_REWORK_LIMIT):
        self.db = db
        self.rework_limit = rework_limit

    def is_orchestrated_task(self, task: QueueTaskRow) -> bool:
        if task.task_type == "phase_orchestration_root":
            return False
        return isinstance(self._extract_phase_flow(task.payload_json), list)

    def handle_phase_completion(
        self,
        task: QueueTaskRow,
        *,
        service_name: str,
        worker_name: str,
        result_summary: str | None = None,
        review_state: str | None = None,
        audit_feedback: str | None = None,
    ) -> bool:
        if not self.is_orchestrated_task(task):
            return False

        root_task = self.db.select_orchestration_task_for_update(task.root_task_id)
        if root_task.status in TERMINAL_ROOT_STATUSES:
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "root_task_already_terminal",
                f"Ignored phase completion because root task is already {root_task.status}",
                worker_name,
            )
            return True

        phase_flow = self._validated_phase_flow(task, root_task, service_name, worker_name)
        if phase_flow is None:
            return True
        if task.phase not in phase_flow:
            return self.block_root_task(root_task, task, service_name, worker_name, "phase_not_in_phase_flow")

        root_payload = self._with_orchestration(task=root_task, base_payload=root_task.payload_json, phase_flow=phase_flow)
        root_payload["orchestration"]["last_completed_phase"] = task.phase
        self.db.insert_log(
            task.id,
            task.root_task_id,
            service_name,
            "phase_task_succeeded",
            f"Phase {task.phase} task completed",
            worker_name,
        )

        if task.phase == 5:
            return self._handle_phase5_completion(
                task,
                root_task,
                root_payload,
                service_name=service_name,
                worker_name=worker_name,
                result_summary=result_summary,
                review_state=review_state,
                audit_feedback=audit_feedback,
            )

        current_index = phase_flow.index(task.phase)
        if current_index + 1 >= len(phase_flow):
            return self.block_root_task(root_task, task, service_name, worker_name, "missing_terminal_phase")
        next_phase = phase_flow[current_index + 1]

        if self.db.select_active_phase_task(task.root_task_id, next_phase) is not None:
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "phase_task_deduplicated",
                f"Phase {next_phase} task already exists",
                worker_name,
            )
            return True

        child_payload = self._build_child_payload(task, next_phase, phase_flow=phase_flow)
        child_task_id = self.db.insert_task(
            parent_task_id=task.id,
            root_task_id=task.root_task_id,
            task_type=PHASE_TASK_TYPES[next_phase],
            phase=next_phase,
            status="queued",
            requested_by_role="dashboard",
            assigned_role=task.assigned_service,
            assigned_service="brain",
            priority=task.priority,
            workspace_path=task.workspace_path,
            target_repo=task.target_repo,
            target_ref=task.target_ref,
            working_branch=task.working_branch,
            payload_json=child_payload,
            retry_count=0,
            max_retry=3,
            approval_required=False,
        )
        root_payload["orchestration"]["current_phase"] = next_phase
        if not self.db.update_task_payload_json(root_task.id, root_payload):
            raise TaskConsistencyError(f"root task {root_task.id} payload could not be updated")
        self.db.insert_log(
            child_task_id,
            task.root_task_id,
            service_name,
            "phase_task_enqueued",
            f"Enqueued phase {next_phase} task from phase {task.phase}",
            worker_name,
        )
        return True

    def handle_phase_blocked(
        self,
        task: QueueTaskRow,
        *,
        service_name: str,
        worker_name: str,
        reason: str,
    ) -> bool:
        if not self.is_orchestrated_task(task):
            return False
        root_task = self.db.select_orchestration_task_for_update(task.root_task_id)
        return self.block_root_task(root_task, task, service_name, worker_name, reason)

    def block_root_task(
        self,
        root_task: QueueTaskRow,
        task: QueueTaskRow,
        service_name: str,
        worker_name: str,
        reason: str,
    ) -> bool:
        if root_task.status in TERMINAL_ROOT_STATUSES:
            return True
        if not self.db.update_task_status(root_task.id, root_task.status, "blocked"):
            raise TaskConsistencyError(f"root task {root_task.id} could not be blocked")
        root_payload = self._with_orchestration(
            task=root_task,
            base_payload=root_task.payload_json,
            phase_flow=self._extract_phase_flow(task.payload_json) or [task.phase],
        )
        root_payload["orchestration"]["current_phase"] = task.phase
        root_payload["orchestration"]["failure_reason"] = reason
        if not self.db.update_task_payload_json(root_task.id, root_payload):
            raise TaskConsistencyError(f"root task {root_task.id} payload could not be updated after block")
        self.db.insert_log(
            task.id,
            task.root_task_id,
            service_name,
            "root_task_blocked",
            f"Root task blocked after phase {task.phase}: {reason}",
            worker_name,
        )
        return True

    def _handle_phase5_completion(
        self,
        task: QueueTaskRow,
        root_task: QueueTaskRow,
        root_payload: dict[str, Any],
        *,
        service_name: str,
        worker_name: str,
        result_summary: str | None,
        review_state: str | None,
        audit_feedback: str | None,
    ) -> bool:
        normalized_review_state = self._normalize_review_state(review_state, result_summary or "")
        if normalized_review_state == "approved":
            next_status = "waiting_approval" if root_task.target_repo else "succeeded"
            if not self.db.update_task_status(root_task.id, root_task.status, next_status):
                raise TaskConsistencyError(f"root task {root_task.id} could not be promoted to {next_status}")
            root_payload["orchestration"]["current_phase"] = 5
            root_payload["orchestration"]["final_review_state"] = "approved"
            if not self.db.update_task_payload_json(root_task.id, root_payload):
                raise TaskConsistencyError(f"root task {root_task.id} payload could not be updated after approval")
            event_type = "root_task_promoted_waiting_approval" if next_status == "waiting_approval" else "root_task_promoted_succeeded"
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                event_type,
                f"Root task promoted to {next_status} after phase 5 approval",
                worker_name,
            )
            return True

        if normalized_review_state == "rejected":
            existing_attempt = int(root_payload["orchestration"].get("phase_attempt", 0))
            next_attempt = existing_attempt + 1
            if next_attempt > self.rework_limit:
                return self.block_root_task(root_task, task, service_name, worker_name, "phase_rework_limit_exceeded")
            feedback = (audit_feedback or result_summary or "").strip() or "監査で不合格（詳細なし）"
            rework_payload = self._build_child_payload(task, 4, phase_flow=self._extract_phase_flow(task.payload_json) or [0, 1, 2, 3, 4, 5])
            rework_payload["orchestration"]["audit_feedback"] = feedback
            rework_payload["orchestration"]["phase_attempt"] = next_attempt
            child_task_id = self.db.insert_task(
                parent_task_id=task.id,
                root_task_id=task.root_task_id,
                task_type=PHASE_TASK_TYPES[4],
                phase=4,
                status="queued",
                requested_by_role="dashboard",
                assigned_role=task.assigned_service,
                assigned_service="brain",
                priority=task.priority,
                workspace_path=task.workspace_path,
                target_repo=task.target_repo,
                target_ref=task.target_ref,
                working_branch=task.working_branch,
                payload_json=rework_payload,
                retry_count=0,
                max_retry=3,
                approval_required=False,
            )
            root_payload["orchestration"]["current_phase"] = 4
            root_payload["orchestration"]["final_review_state"] = "rejected"
            root_payload["orchestration"]["phase_attempt"] = next_attempt
            root_payload["orchestration"]["audit_feedback"] = feedback
            if not self.db.update_task_payload_json(root_task.id, root_payload):
                raise TaskConsistencyError(f"root task {root_task.id} payload could not be updated after rejection")
            self.db.insert_log(
                child_task_id,
                task.root_task_id,
                service_name,
                "phase_rework_enqueued",
                f"Re-enqueued phase 4 task after phase 5 rejection: {feedback}",
                worker_name,
            )
            return True

        return self.block_root_task(root_task, task, service_name, worker_name, "phase5_result_unparseable")

    @staticmethod
    def _extract_phase_flow(payload_json: dict[str, Any] | None) -> list[int] | None:
        if not isinstance(payload_json, dict):
            return None
        orchestration = payload_json.get("orchestration")
        if isinstance(orchestration, dict) and isinstance(orchestration.get("phase_flow"), list):
            return [int(phase) for phase in orchestration["phase_flow"]]
        phase_flow = payload_json.get("phase_flow")
        if isinstance(phase_flow, list):
            return [int(phase) for phase in phase_flow]
        return None

    def _validated_phase_flow(
        self,
        task: QueueTaskRow,
        root_task: QueueTaskRow,
        service_name: str,
        worker_name: str,
    ) -> list[int] | None:
        phase_flow = self._extract_phase_flow(task.payload_json)
        if not phase_flow:
            self.block_root_task(root_task, task, service_name, worker_name, "orchestration_payload_invalid")
            return None
        if len(set(phase_flow)) != len(phase_flow):
            self.block_root_task(root_task, task, service_name, worker_name, "duplicate_phase_flow")
            return None
        return phase_flow

    def _with_orchestration(
        self,
        *,
        task: QueueTaskRow,
        base_payload: dict[str, Any] | None,
        phase_flow: list[int],
    ) -> dict[str, Any]:
        payload = dict(base_payload or {})
        orchestration = dict(payload.get("orchestration") or {})
        orchestration.setdefault("phase_flow", phase_flow)
        orchestration.setdefault("current_phase", task.phase)
        orchestration.setdefault("last_completed_phase", None)
        orchestration.setdefault("phase_attempt", 0)
        orchestration.setdefault("final_review_state", "pending")
        payload["orchestration"] = orchestration
        payload.setdefault("phase_flow", phase_flow)
        return payload

    def _build_child_payload(self, task: QueueTaskRow, next_phase: int, *, phase_flow: list[int]) -> dict[str, Any]:
        payload = self._with_orchestration(task=task, base_payload=task.payload_json, phase_flow=phase_flow)
        payload["orchestration"] = {
            **payload["orchestration"],
            "current_phase": next_phase,
            "source_task_id": task.id,
        }
        payload["phase_flow"] = phase_flow
        return payload

    @staticmethod
    def _normalize_review_state(review_state: str | None, result_summary: str) -> str | None:
        if review_state:
            normalized = review_state.strip().lower()
            if normalized in {"approved", "rejected"}:
                return normalized
        uppercase = result_summary.upper()
        if "NOT APPROVED" in uppercase:
            return "rejected"
        if "NOT REJECTED" in uppercase and "APPROVED" in uppercase:
            return "approved"
        if "REJECTED" in uppercase and "NOT REJECTED" not in uppercase:
            return "rejected"
        if "APPROVED" in uppercase:
            return "approved"
        return None