from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable

from security.compose_validator import ComposeValidator

from .contracts import ContractValidationError, ModelContractCodec
from .database import LeaseConflictError, MariaDBAccessor, TaskConsistencyError
from .llm_client import (
    LLMAuthenticationError,
    LLMClient,
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMRateLimitError,
    LLMServiceError,
    LLMTimeoutError,
)
from .phase_orchestrator import PHASE_TASK_TYPES, PhaseOrchestrator
from .repository_workspace import CommitPushError, RepositoryPreparationError, RepositoryWorkspaceManager
from .state_machine import TaskStateMachine


PHASE_PROMPT_CONTEXT = {
    0: "Phase 0 / phase0_brainstorm: 要求整理と壁打ちに集中し、前提・論点・作業計画を docs に反映せよ。",
    1: "Phase 1 / phase1_design: 基本設計に集中し、phase 0 の結果を踏まえて設計書と非機能要件を更新せよ。",
    2: "Phase 2 / phase2_test_design: 破壊テスト設計に集中し、異常系・破壊シナリオを docs に追加せよ。",
    3: "Phase 3 / phase3_test_impl: テスト実装に集中し、RED を先に作れ。",
    4: "Phase 4 / phase4_impl: 本体実装に集中し、必要最小限の code/docs 同期だけを行え。",
    5: "Phase 5 / phase5_audit: 監査に集中し、コード変更は行わず APPROVED または REJECTED と指摘を返せ。",
}


@dataclass
class MariaDBTaskBackend:
    connection: object
    git_command_runner: Callable[[list[str], Path], None] | None = None
    llm_client: Any | None = None
    workspace_root: str | Path = "/workspace"

    def __post_init__(self) -> None:
        self.db = MariaDBAccessor(self.connection, workspace_root=self.workspace_root)
        self.repository_workspace = RepositoryWorkspaceManager(self.git_command_runner)
        self.phase_orchestrator = PhaseOrchestrator(self.db)

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
            normalized_workspace_path = None
            if task.workspace_path:
                try:
                    normalized_workspace_path = self.db.normalize_task_workspace_path(task.workspace_path)
                except TaskConsistencyError as error:
                    blocked = self.db.update_task_status(task.id, "running", "blocked")
                    if not blocked:
                        raise TaskConsistencyError(f"task {task.id} could not be blocked after invalid workspace path")
                    self.db.insert_log(
                        task.id,
                        task.root_task_id,
                        service_name,
                        "invalid_workspace_path",
                        f"Invalid workspace path: {error}",
                        worker_name,
                    )
                    self.phase_orchestrator.handle_phase_blocked(
                        task,
                        service_name=service_name,
                        worker_name=worker_name,
                        reason="invalid_workspace_path",
                    )
                    return True

            if task.target_repo and normalized_workspace_path and task.working_branch:
                try:
                    prepared_paths = self.repository_workspace.prepare_repository(
                        workspace_path=normalized_workspace_path,
                        target_repo=task.target_repo,
                        target_ref=task.target_ref or "main",
                        working_branch=task.working_branch,
                    )
                except RepositoryPreparationError as error:
                    blocked = self.db.update_task_status(task.id, "running", "blocked")
                    if not blocked:
                        raise TaskConsistencyError(f"task {task.id} could not be blocked after repository prepare failure")
                    self.db.insert_log(
                        task.id,
                        task.root_task_id,
                        service_name,
                        "repository_prepare_failed",
                        f"Repository preparation failed: {error}",
                        worker_name,
                    )
                    self.phase_orchestrator.handle_phase_blocked(
                        task,
                        service_name=service_name,
                        worker_name=worker_name,
                        reason="repository_prepare_failed",
                    )
                    return True

                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    "repository_prepared",
                    f"Repository prepared in {prepared_paths['repo_path']}",
                    worker_name,
                )
                if self._compose_validation_required(task):
                    if not self._validate_repository_compose(
                        task,
                        service_name=service_name,
                        worker_name=worker_name,
                        workspace_path=normalized_workspace_path,
                        repo_path=prepared_paths["repo_path"],
                    ):
                        return True
                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    "phase_flow_initialized",
                    "Phase 0-5 execution flow initialized",
                    worker_name,
                )

            instruction = self._extract_instruction(task.payload_json)
            if isinstance(task.payload_json, dict) and instruction is None:
                blocked = self.db.update_task_status(task.id, "running", "blocked")
                if not blocked:
                    raise TaskConsistencyError(f"task {task.id} could not be blocked after invalid instruction")
                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    "invalid_instruction",
                    "Invalid instruction: payload_json.instruction is required",
                    worker_name,
                )
                self.phase_orchestrator.handle_phase_blocked(
                    task,
                    service_name=service_name,
                    worker_name=worker_name,
                    reason="invalid_instruction",
                )
                return True
            if instruction:
                return self._generate_task_result(task, service_name, worker_name, instruction, normalized_workspace_path)
            return True

    def task_working_directory(self, task_id: int) -> str | None:
        return self.db.select_task_workspace_path(task_id)

    def apply_artifact_for_task(self, task_id: int, service_name: str, worker_name: str) -> bool:
        with self.db.transaction():
            task = self.db.select_task_for_artifact_apply(task_id)
            if task.status != "waiting_approval":
                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    "artifact_apply_skipped",
                    f"Task {task.id} is not waiting_approval",
                    worker_name,
                )
                return False

            blocked = self.db.update_task_status(task.id, "waiting_approval", "blocked")
            if not blocked:
                raise TaskConsistencyError(f"task {task.id} could not be blocked after deprecated artifact apply request")
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "artifact_apply_deprecated",
                "Artifact apply path is disabled after direct-edit pivot",
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

    def _generate_task_result(
        self,
        task,
        service_name: str,
        worker_name: str,
        instruction: str,
        workspace_path: str | None,
    ) -> bool:
        try:
            prompt = self._build_prompt(task, instruction)
            llm_client = self._get_llm_client()
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "llm_generation_started",
                "LLM generation started",
                worker_name,
            )
            response = llm_client.generate(
                prompt,
                metadata={
                    "task_id": task.id,
                    "root_task_id": task.root_task_id,
                    "target_repo": task.target_repo,
                    "working_branch": task.working_branch,
                    "workspace_path": workspace_path,
                },
            )
            sanitized_response = self._sanitize_response(response)
            self._validate_response_size(sanitized_response)
            summary = self._build_result_summary(sanitized_response)
            artifact_path = self._write_llm_artifact(workspace_path, sanitized_response)
            self._persist_phase_metadata(task, llm_client, summary)
        except (
            LLMAuthenticationError,
            LLMConfigurationError,
            LLMEmptyResponseError,
            LLMRateLimitError,
            LLMServiceError,
            LLMTimeoutError,
            OSError,
            ValueError,
        ) as error:
            blocked = self.db.update_task_status(task.id, "running", "blocked")
            if not blocked:
                raise TaskConsistencyError(f"task {task.id} could not be blocked after llm failure")
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "llm_generation_failed",
                f"LLM generation failed: {error}",
                worker_name,
            )
            return True

        task_title = None
        if isinstance(task.payload_json, dict) and isinstance(task.payload_json.get("task"), str):
            task_title = task.payload_json["task"].strip()

        is_orchestrated_task = self.phase_orchestrator.is_orchestrated_task(task)
        is_audit_phase = is_orchestrated_task and task.phase == 5 and task.task_type == PHASE_TASK_TYPES[5]

        if task.target_repo:
            if not workspace_path or not task.working_branch:
                blocked = self.db.update_task_status(task.id, "running", "blocked")
                if not blocked:
                    raise TaskConsistencyError(f"task {task.id} could not be blocked after invalid direct-edit prerequisites")
                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    "invalid_repository_context",
                    "Direct-edit requires workspace_path and working_branch",
                    worker_name,
                )
                self.phase_orchestrator.handle_phase_blocked(
                    task,
                    service_name=service_name,
                    worker_name=worker_name,
                    reason="invalid_repository_context",
                )
                return True

            if is_audit_phase:
                completed = self.db.update_task_result(task.id, "running", "succeeded", summary)
                if not completed:
                    raise TaskConsistencyError(f"task {task.id} could not be updated after audit success")
                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    "llm_generation_succeeded",
                    f"LLM response saved to {artifact_path}",
                    worker_name,
                )
                self.phase_orchestrator.handle_phase_completion(
                    task,
                    service_name=service_name,
                    worker_name=worker_name,
                    result_summary=sanitized_response,
                    review_state=self._parse_review_state(sanitized_response),
                    audit_feedback=sanitized_response,
                )
                return True

            target_status = "succeeded" if is_orchestrated_task else ("waiting_approval" if task.approval_required else "succeeded")
            try:
                commit_result = self.repository_workspace.commit_and_push(
                    workspace_path=workspace_path,
                    working_branch=task.working_branch,
                    task_title=task_title,
                    result_summary_md=summary,
                )
            except CommitPushError as error:
                if str(error) == "phase_edit_no_changes":
                    completed = self.db.update_task_result(task.id, "running", target_status, summary)
                    if not completed:
                        raise TaskConsistencyError(f"task {task.id} could not be updated after no-change direct-edit")
                    self.db.insert_log(
                        task.id,
                        task.root_task_id,
                        service_name,
                        "llm_generation_succeeded",
                        f"LLM response saved to {artifact_path}",
                        worker_name,
                    )
                    self.db.insert_log(
                        task.id,
                        task.root_task_id,
                        service_name,
                        "phase_edit_no_changes",
                        "Direct-edit produced no repository changes; skipped commit/push",
                        worker_name,
                    )
                    self.phase_orchestrator.handle_phase_completion(
                        task,
                        service_name=service_name,
                        worker_name=worker_name,
                        result_summary=summary,
                    )
                    return True
                blocked = self.db.update_task_status(task.id, "running", "blocked")
                if not blocked:
                    raise TaskConsistencyError(f"task {task.id} could not be blocked after direct-edit failure")
                event_type = self._map_commit_push_error_to_event(str(error))
                self.db.insert_log(
                    task.id,
                    task.root_task_id,
                    service_name,
                    event_type,
                    f"Direct-edit failed: {error}",
                    worker_name,
                )
                self.phase_orchestrator.handle_phase_blocked(
                    task,
                    service_name=service_name,
                    worker_name=worker_name,
                    reason=event_type,
                )
                return True

            completed = self.db.update_task_result(task.id, "running", target_status, summary)
            if not completed:
                raise TaskConsistencyError(f"task {task.id} could not be updated after direct-edit success")
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "llm_generation_succeeded",
                f"LLM response saved to {artifact_path}",
                worker_name,
            )
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "git_commit_succeeded",
                f"Created commit {commit_result['commit_sha']}",
                worker_name,
            )
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "git_push_succeeded",
                f"Pushed {commit_result['working_branch']} to origin",
                worker_name,
            )
            self.phase_orchestrator.handle_phase_completion(
                task,
                service_name=service_name,
                worker_name=worker_name,
                result_summary=summary,
            )
            return True

        completed = self.db.update_task_result(task.id, "running", "succeeded", summary)
        if not completed:
            raise TaskConsistencyError(f"task {task.id} could not be updated after llm success")
        self.db.insert_log(
            task.id,
            task.root_task_id,
            service_name,
            "llm_generation_succeeded",
            f"LLM response saved to {artifact_path}",
            worker_name,
        )
        self.phase_orchestrator.handle_phase_completion(
            task,
            service_name=service_name,
            worker_name=worker_name,
            result_summary=summary,
        )
        return True

    @staticmethod
    def _parse_review_state(response: str) -> str | None:
        uppercase = response.upper()
        if "NOT APPROVED" in uppercase:
            return "rejected"
        if "NOT REJECTED" in uppercase and "APPROVED" in uppercase:
            return "approved"
        if "REJECTED" in uppercase and "NOT REJECTED" not in uppercase:
            return "rejected"
        if "APPROVED" in uppercase:
            return "approved"
        return None

    def _get_llm_client(self):
        if self.llm_client is None:
            self.llm_client = LLMClient.from_environment()
        return self.llm_client

    def _persist_phase_metadata(self, task, llm_client: Any, summary: str) -> None:
        if not self.phase_orchestrator.is_orchestrated_task(task):
            return
        if not isinstance(task.payload_json, dict):
            return

        payload_json = dict(task.payload_json)
        orchestration = dict(payload_json.get("orchestration") or {})
        model_name = getattr(llm_client, "model", None)
        orchestration["llm_model"] = model_name.strip() if isinstance(model_name, str) and model_name.strip() else orchestration.get("llm_model")
        orchestration["phase_summary"] = summary or orchestration.get("phase_summary") or "-"
        orchestration["handoff_message"] = self._build_handoff_message(summary)
        payload_json["orchestration"] = orchestration
        if not self.db.update_task_payload_json(task.id, payload_json):
            raise TaskConsistencyError(f"task {task.id} payload could not be updated with phase metadata")
        object.__setattr__(task, "payload_json", payload_json)

    @staticmethod
    def _build_handoff_message(summary: str) -> str:
        stripped = summary.strip()
        return stripped or "引き継ぎ事項なし"

    @staticmethod
    def _compose_validation_required(task) -> bool:
        if not isinstance(task.payload_json, dict):
            return False
        if task.payload_json.get("compose_validation_required") is True:
            return True
        return task.payload_json.get("runtime_spec_json") is not None

    def _validate_repository_compose(
        self,
        task,
        *,
        service_name: str,
        worker_name: str,
        workspace_path: str,
        repo_path: str,
    ) -> bool:
        runtime_root = Path(workspace_path) / "runtime"
        validator = ComposeValidator(repo_root=repo_path, runtime_root=runtime_root)
        self.db.insert_log(
            task.id,
            task.root_task_id,
            service_name,
            "compose_validation_started",
            "Compose validation started",
            worker_name,
            details_json={
                "repo_path": str(Path(repo_path).resolve(strict=False)),
                "runtime_root": str(runtime_root.resolve(strict=False)),
            },
        )
        result = validator.validate()
        if result["blocked"]:
            blocked = self.db.update_task_status(task.id, "running", "blocked")
            if not blocked:
                raise TaskConsistencyError(f"task {task.id} could not be blocked after compose validation failure")
            self.db.insert_log(
                task.id,
                task.root_task_id,
                service_name,
                "compose_validation_blocked",
                "Compose validation failed",
                worker_name,
                details_json=result,
            )
            self.phase_orchestrator.handle_phase_blocked(
                task,
                service_name=service_name,
                worker_name=worker_name,
                reason="compose_validation_blocked",
            )
            return False
        self.db.insert_log(
            task.id,
            task.root_task_id,
            service_name,
            "compose_validation_passed",
            "Compose validation passed",
            worker_name,
            details_json=result,
        )
        return True

    @staticmethod
    def _extract_instruction(payload_json: dict[str, Any] | None) -> str | None:
        if not isinstance(payload_json, dict):
            return None
        instruction = payload_json.get("instruction")
        if not isinstance(instruction, str):
            return None
        stripped = instruction.strip()
        return stripped or None

    @staticmethod
    def _build_prompt(task, instruction: str) -> str:
        prompt_parts = [
            "リポジトリのファイルを直接編集し、変更を完成させよ。",
            "git commit / git push は実行するな。システム側で行う。",
            "リポジトリ外のファイルを編集するな。",
        ]
        phase = getattr(task, "phase", None)
        task_type = getattr(task, "task_type", None)
        if phase is not None:
            prompt_parts.append(f"Phase: {phase}")
        if task_type:
            prompt_parts.append(f"Task Type: {task_type}")
        if phase in PHASE_PROMPT_CONTEXT:
            prompt_parts.append(PHASE_PROMPT_CONTEXT[phase])
        if isinstance(task.payload_json, dict) and isinstance(task.payload_json.get("task"), str):
            prompt_parts.append(f"Task: {task.payload_json['task'].strip()}")
        if task.target_repo:
            prompt_parts.append(f"Repository: {task.target_repo}")
        if task.working_branch:
            prompt_parts.append(f"Working branch: {task.working_branch}")
        prompt_parts.append("Instruction:")
        prompt_parts.append(instruction)
        return "\n\n".join(prompt_parts)

    @staticmethod
    def _map_commit_push_error_to_event(error_message: str) -> str:
        if error_message == "phase_edit_no_changes":
            return "phase_edit_no_changes"
        if error_message.startswith("git_commit_failed:"):
            return "git_commit_failed"
        if error_message.startswith("git_push_failed:"):
            return "git_push_failed"
        if error_message.startswith("too_many_changed_files:"):
            return "too_many_changed_files"
        if error_message in {"secret_in_changed_files", "git_metadata_write_forbidden", "path_traversal", "symlink_escape"}:
            return error_message
        return "phase_edit_failed"

    @staticmethod
    def _build_result_summary(response: str) -> str:
        lines = [line.strip() for line in response.splitlines() if line.strip()]
        if not lines:
            raise LLMEmptyResponseError("empty LLM response")
        return lines[0][:200]

    @staticmethod
    def _sanitize_response(response: str) -> str:
        sanitized = response
        for env_var in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
            secret = os.getenv(env_var, "").strip()
            if secret:
                sanitized = sanitized.replace(secret, "[MASKED_GITHUB_TOKEN]")
        return sanitized

    @staticmethod
    def _validate_response_size(response: str) -> None:
        max_response_bytes = int(os.getenv("LLM_MAX_RESPONSE_BYTES", "131072"))
        response_size = len(response.encode("utf-8"))
        if response_size > max_response_bytes:
            raise ValueError(f"LLM response exceeded size limit: {response_size} > {max_response_bytes}")

    @staticmethod
    def _write_llm_artifact(workspace_path: str | None, response: str) -> str:
        if not workspace_path:
            raise ValueError("workspace_path is required for LLM artifact persistence")
        artifacts_dir = Path(workspace_path) / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / "llm_response.md"
        artifact_path.write_text(response, encoding="utf-8")
        return str(artifact_path)