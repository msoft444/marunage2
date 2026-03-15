from __future__ import annotations

import json
import posixpath
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import nh3

from backend.database import MariaDBAccessor, TaskConsistencyError
from backend.repository_workspace import CommitPushError, RepositoryWorkspaceManager

from .secret_scanner import SecretScanner
from .sandbox import WorkspaceSandbox


class SecureDashboard:
    _ALLOWED_TAGS = {"a", "p", "br", "code", "pre", "strong", "em", "ul", "ol", "li"}
    _ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}
    _TEXT_CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }

    def __init__(
        self,
        asset_root: Path | None = None,
        db_connection: Any | None = None,
        connection_factory: Callable[[], Any] | None = None,
        secret_scanner: SecretScanner | None = None,
        workspace_root: Path | str | None = None,
        repository_workspace_manager: RepositoryWorkspaceManager | None = None,
    ):
        self.asset_root = asset_root or Path(__file__).resolve().parent / "static"
        self.db_connection = db_connection
        self.connection_factory = connection_factory
        self.secret_scanner = secret_scanner or SecretScanner()
        self.workspace_root = Path(workspace_root).resolve(strict=False) if workspace_root else Path.cwd().resolve(strict=False)
        self.sandbox = WorkspaceSandbox(str(self.workspace_root))
        self.repository_workspace = repository_workspace_manager or RepositoryWorkspaceManager()
        self._banned_repository_roots = tuple(
            Path(path).resolve(strict=False) for path in ("/", "/etc", "/bin", "/sbin", "/usr", "/var", "/private", "/dev", "/System")
        )

    def render_markdown(self, content_md: str) -> str:
        return nh3.clean(
            content_md,
            tags=self._ALLOWED_TAGS,
            attributes=self._ALLOWED_ATTRIBUTES,
            url_schemes={"http", "https", "mailto"},
        )

    def fetch_timeline(self, message_count: int) -> dict:
        return {"initial_load": min(message_count, 100), "paginated": True}

    def approve_release(self) -> dict:
        return {"promote_release_tasks": 1}

    def serve_path(self, request_path: str) -> dict:
        return self.serve_request("GET", request_path, body=None)

    def serve_request(
        self,
        method: str,
        request_path: str,
        body: str | None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        parsed = urlparse(request_path or "/")
        path = posixpath.normpath(parsed.path or "/")
        query = parse_qs(parsed.query, keep_blank_values=True)
        method = method.upper()

        # Static UI must win over the generic API handler for `/` and `/index.html`.
        if path in {"/", "/index.html"}:
            return self._serve_asset("index.html")

        if path.startswith("/static/"):
            relative_path = path.removeprefix("/static/")
            return self._serve_asset(relative_path)

        if path.startswith("/api/v1/"):
            return self._handle_api(method, path, body, query, headers or {})

        return self._json_response(404, {"error": "not_found", "path": path})

    def _handle_api(
        self,
        method: str,
        path: str,
        body: str | None,
        query: dict[str, list[str]],
        headers: dict[str, str],
    ) -> dict:
        if method == "GET" and path == "/api/v1/health":
            payload = {
                "service": "dashboard",
                "status": "ok",
                "path": path,
            }
            return self._json_response(200, payload)

        if path == "/api/v1/tasks":
            if method == "GET":
                return self._json_response(200, {"tasks": self._list_tasks()})
            if method == "POST":
                return self._create_task(body)
            return self._json_response(405, {"error": "method_not_allowed", "path": path, "method": method})

        if path == "/api/v1/repositories/branches":
            if method != "GET":
                return self._json_response(405, {"error": "method_not_allowed", "path": path, "method": method})
            repository_url = (query.get("repository_url") or [""])[0]
            return self._get_repository_branches(repository_url)

        if path.startswith("/api/v1/tasks/"):
            route = self._parse_task_route(path)
            if route is None:
                return self._json_response(404, {"error": "task_not_found", "path": path})
            task_id, action = route
            if action is None:
                if method != "GET":
                    return self._json_response(405, {"error": "method_not_allowed", "path": path, "method": method})
                return self._get_task_detail(task_id)
            if action == "diff":
                if method != "GET":
                    return self._json_response(405, {"error": "method_not_allowed", "path": path, "method": method})
                return self._get_task_diff(task_id)
            if action == "approve":
                if method != "POST":
                    return self._json_response(405, {"error": "method_not_allowed", "path": path, "method": method})
                return self._approve_task(task_id, body, headers)
            if action == "reject":
                if method != "POST":
                    return self._json_response(405, {"error": "method_not_allowed", "path": path, "method": method})
                return self._reject_task(task_id, body, headers)
            return self._json_response(404, {"error": "task_not_found", "path": path})

        return self._json_response(404, {"error": "not_found", "path": path})

    def _create_task(self, body: str | None) -> dict:
        try:
            request_payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return self._json_response(400, {"error": "invalid_json"})

        if not isinstance(request_payload, dict):
            return self._json_response(400, {"error": "invalid_payload"})

        scan_result = {
            "blocked": False,
            "decode_depth": 0,
            "disabled": True,
            "reason": "development_task_requests_bypass_secret_scanner",
        }
        task_status = "queued"
        repository_context = self._resolve_repository_context(
            request_payload.get("repository_path"),
            request_payload.get("target_ref"),
        )
        if request_payload.get("repository_path") and repository_context is None:
            if self._normalize_github_repository_url(request_payload.get("repository_path")) is not None:
                return self._json_response(400, {"error": "invalid_target_ref"})
            return self._json_response(400, {"error": "invalid_repository_path"})
        default_phase = 0 if repository_context and repository_context["requires_clone"] else 4
        default_task_type = "requirement_session" if repository_context and repository_context["requires_clone"] else "documentation"
        task_type = str(request_payload.get("task_type") or default_task_type)
        assigned_service = str(request_payload.get("assigned_service") or "brain")
        assigned_role = str(request_payload.get("assigned_role") or assigned_service)
        try:
            phase = self._coerce_int_field(request_payload, "phase", default_phase)
            priority = self._coerce_int_field(request_payload, "priority", 0)
        except ValueError as error:
            return self._json_response(400, {"error": "invalid_payload", "field": str(error)})
        payload_json = dict(request_payload)
        if repository_context is not None:
            payload_json["repository_path"] = repository_context["repository_path"]
            if repository_context["requires_clone"]:
                payload_json["phase_flow"] = [0, 1, 2, 3, 4, 5]
                payload_json["orchestration"] = {
                    "phase_flow": [0, 1, 2, 3, 4, 5],
                    "current_phase": 0,
                    "last_completed_phase": None,
                    "phase_attempt": 0,
                    "final_review_state": "pending",
                }
                payload_json["repository_source"] = "github_url"
        payload_json["security_scan"] = {
            "blocked": scan_result["blocked"],
            "decode_depth": scan_result["decode_depth"],
            "disabled": scan_result["disabled"],
            "reason": scan_result["reason"],
        }

        with self._database() as connection:
            cursor = self._cursor(connection)
            begin = getattr(connection, "begin", None)
            if callable(begin):
                begin()
            try:
                if repository_context and repository_context["requires_clone"]:
                    cursor.execute(
                        (
                            "INSERT INTO tasks ("
                            "parent_task_id, root_task_id, task_type, phase, status, requested_by_role, assigned_role, assigned_service, "
                            "priority, workspace_path, target_repo, target_ref, working_branch, payload_json, retry_count, max_retry, approval_required"
                            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        ),
                        (
                            None,
                            0,
                            "phase_orchestration_root",
                            0,
                            "running",
                            "dashboard",
                            assigned_role,
                            assigned_service,
                            priority,
                            None,
                            repository_context["target_repo"],
                            repository_context["target_ref"],
                            None,
                            json.dumps(payload_json, ensure_ascii=False),
                            0,
                            3,
                            True,
                        ),
                    )
                    task_id = self._last_insert_id(cursor, connection)
                    repository_context = self._finalize_repository_context(task_id, repository_context)
                    payload_json["repository_path"] = repository_context["repository_path"]
                    payload_json["clone_destination"] = f"{repository_context['workspace_path']}/repo"
                    cursor.execute(
                        (
                            "UPDATE tasks SET root_task_id = %s, workspace_path = %s, target_repo = %s, target_ref = %s, working_branch = %s, payload_json = %s "
                            "WHERE id = %s"
                        ),
                        (
                            task_id,
                            repository_context["workspace_path"],
                            repository_context["target_repo"],
                            repository_context["target_ref"],
                            repository_context["working_branch"],
                            json.dumps(payload_json, ensure_ascii=False),
                            task_id,
                        ),
                    )
                    phase_payload = dict(payload_json)
                    phase_payload["orchestration"] = {
                        **payload_json["orchestration"],
                        "source_task_id": task_id,
                    }
                    cursor.execute(
                        (
                            "INSERT INTO tasks ("
                            "parent_task_id, root_task_id, task_type, phase, status, requested_by_role, assigned_role, assigned_service, "
                            "priority, workspace_path, target_repo, target_ref, working_branch, payload_json, retry_count, max_retry, approval_required"
                            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        ),
                        (
                            task_id,
                            task_id,
                            "phase0_brainstorm",
                            0,
                            task_status,
                            "dashboard",
                            assigned_role,
                            assigned_service,
                            priority,
                            repository_context["workspace_path"],
                            repository_context["target_repo"],
                            repository_context["target_ref"],
                            repository_context["working_branch"],
                            json.dumps(phase_payload, ensure_ascii=False),
                            0,
                            3,
                            False,
                        ),
                    )
                    phase_task_id = self._last_insert_id(cursor, connection)
                    cursor.execute(
                        (
                            "INSERT INTO logs (task_id, root_task_id, service, component, level, event_type, message, details_json, trace_id) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        ),
                        (
                            task_id,
                            task_id,
                            "dashboard",
                            "interactive_ui",
                            "INFO",
                            "phase_root_created",
                            "Phase orchestration root task created",
                            None,
                            f"dashboard-{task_id}",
                        ),
                    )
                    cursor.execute(
                        (
                            "INSERT INTO logs (task_id, root_task_id, service, component, level, event_type, message, details_json, trace_id) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        ),
                        (
                            phase_task_id,
                            task_id,
                            "dashboard",
                            "interactive_ui",
                            "INFO",
                            "phase_task_enqueued",
                            "Initial phase 0 task created",
                            None,
                            f"dashboard-{task_id}",
                        ),
                    )
                else:
                    cursor.execute(
                        (
                            "INSERT INTO tasks ("
                            "parent_task_id, root_task_id, task_type, phase, status, requested_by_role, assigned_role, assigned_service, "
                            "priority, workspace_path, target_repo, target_ref, working_branch, payload_json, retry_count, max_retry, approval_required"
                            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                        ),
                        (
                            None,
                            0,
                            task_type,
                            phase,
                            task_status,
                            "dashboard",
                            assigned_role,
                            assigned_service,
                            priority,
                            repository_context["workspace_path"] if repository_context else None,
                            repository_context["target_repo"] if repository_context else None,
                            repository_context["target_ref"] if repository_context else None,
                            repository_context["working_branch"] if repository_context else None,
                            json.dumps(payload_json, ensure_ascii=False),
                            0,
                            3,
                            False,
                        ),
                    )
                    task_id = self._last_insert_id(cursor, connection)
                    repository_context = self._finalize_repository_context(task_id, repository_context)
                    if repository_context is not None:
                        payload_json["repository_path"] = repository_context["repository_path"]
                        if repository_context["requires_clone"]:
                            payload_json["clone_destination"] = f"{repository_context['workspace_path']}/repo"
                    cursor.execute(
                        (
                            "UPDATE tasks SET root_task_id = %s, workspace_path = %s, target_repo = %s, target_ref = %s, working_branch = %s, payload_json = %s "
                            "WHERE id = %s"
                        ),
                        (
                            task_id,
                            repository_context["workspace_path"] if repository_context else None,
                            repository_context["target_repo"] if repository_context else None,
                            repository_context["target_ref"] if repository_context else None,
                            repository_context["working_branch"] if repository_context else None,
                            json.dumps(payload_json, ensure_ascii=False),
                            task_id,
                        ),
                    )
                cursor.execute(
                    (
                        "INSERT INTO logs (task_id, root_task_id, service, component, level, event_type, message, details_json, trace_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    ),
                    (
                        task_id,
                        task_id,
                        "dashboard",
                        "interactive_ui",
                        "INFO",
                        "task_submitted",
                        "Task submitted from dashboard",
                        None,
                        f"dashboard-{task_id}",
                    ),
                )
                commit = getattr(connection, "commit", None)
                if callable(commit):
                    commit()
            except Exception:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
                return self._json_response(500, {"error": "internal_server_error"})

        task = self._load_task(task_id)
        return self._json_response(201, {"task": task, "scan": payload_json["security_scan"]})

    def _coerce_int_field(self, payload: dict[str, Any], field_name: str, default: int) -> int:
        value = payload.get(field_name, default)
        if isinstance(value, bool):
            raise ValueError(field_name)
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(field_name) from error

    def _list_tasks(self) -> list[dict[str, Any]]:
        with self._database() as connection:
            cursor = self._cursor(connection)
            cursor.execute(
                (
                    "SELECT id, parent_task_id, root_task_id, task_type, phase, status, assigned_service, priority, payload_json, workspace_path, target_repo, target_ref, working_branch, result_summary_md, created_at "
                    "FROM tasks WHERE parent_task_id IS NULL ORDER BY created_at DESC, id DESC LIMIT 50"
                ),
                (),
            )
            return [self._serialize_task_row(row) for row in self._fetchall(cursor)]

    def _get_task_detail(self, task_id: int) -> dict:
        task = self._load_task(task_id)
        if task is None:
            return self._json_response(404, {"error": "task_not_found", "task_id": task_id})

        is_root_task = task.get("parent_task_id") is None and task.get("root_task_id") == task_id
        with self._database() as connection:
            cursor = self._cursor(connection)
            if is_root_task:
                cursor.execute(
                    (
                        "SELECT task_id, root_task_id, service, event_type, message, details_json, created_at "
                        "FROM logs WHERE root_task_id = %s ORDER BY id ASC"
                    ),
                    (task_id,),
                )
            else:
                cursor.execute(
                    (
                        "SELECT task_id, root_task_id, service, event_type, message, details_json, created_at "
                        "FROM logs WHERE task_id = %s ORDER BY id ASC"
                    ),
                    (task_id,),
                )
            logs = self._serialize_log_rows(self._fetchall(cursor))
            subtasks: list[dict[str, Any]] = []
            if is_root_task:
                cursor.execute(
                    (
                        "SELECT id, parent_task_id, root_task_id, task_type, phase, status, assigned_service, priority, payload_json, workspace_path, target_repo, target_ref, working_branch, result_summary_md, created_at "
                        "FROM tasks WHERE root_task_id = %s AND id != %s ORDER BY phase ASC, id ASC"
                    ),
                    (task_id, task_id),
                )
                subtasks = [self._serialize_task_row(row) for row in self._fetchall(cursor)]
                logs_by_task_id: dict[Any, list[dict[str, Any]]] = {}
                for log in logs:
                    logs_by_task_id.setdefault(log.get("task_id"), []).append(log)
                for subtask in subtasks:
                    subtask["logs"] = logs_by_task_id.get(subtask.get("id"), [])

        payload = {
            "task": task,
            "subtasks": subtasks,
            "logs": logs,
            "result_html": self.render_markdown(task.get("result_summary_md") or ""),
        }
        return self._json_response(200, payload)

    def _load_task(self, task_id: int) -> dict[str, Any] | None:
        with self._database() as connection:
            cursor = self._cursor(connection)
            cursor.execute(
                (
                    "SELECT id, parent_task_id, root_task_id, task_type, phase, status, requested_by_role, assigned_role, assigned_service, "
                    "priority, workspace_path, target_repo, target_ref, working_branch, payload_json, result_summary_md, lease_owner, lease_expires_at, started_at, finished_at, created_at "
                    "FROM tasks WHERE id = %s"
                ),
                (task_id,),
            )
            row = self._fetchone(cursor)
            if row is None:
                return None
            return self._serialize_task_row(row)

    def _parse_task_route(self, path: str) -> tuple[int, str | None] | None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 4 or parts[:3] != ["api", "v1", "tasks"]:
            return None
        try:
            task_id = int(parts[3])
        except (TypeError, ValueError):
            return None
        action = parts[4] if len(parts) > 4 else None
        return task_id, action

    def _get_repository_branches(self, repository_url: str) -> dict:
        normalized_url = self._normalize_github_repository_url(repository_url)
        if normalized_url is None:
            return self._json_response(400, {"error": "invalid_repository_path"})
        try:
            branches = self.repository_workspace.list_repository_branches(normalized_url)
        except CommitPushError as error:
            return self._json_response(502, {"error": str(error)})
        default_branch = "main" if "main" in branches else (branches[0] if branches else None)
        return self._json_response(200, {"branches": branches, "default_branch": default_branch})

    def _get_task_diff(self, task_id: int) -> dict:
        try:
            task = self._get_approval_task(task_id)
        except TaskConsistencyError:
            return self._json_response(404, {"error": "task_not_found", "task_id": task_id})
        if not task.target_repo or not task.workspace_path or not task.working_branch:
            return self._json_response(400, {"error": "approval_not_supported", "task_id": task_id})
        if not task.target_ref:
            return self._json_response(400, {"error": "invalid_target_ref", "task_id": task_id})
        try:
            diff_text = self.repository_workspace.get_diff(
                workspace_path=task.workspace_path,
                working_branch=task.working_branch,
                merge_target=task.target_ref,
            )
        except CommitPushError as error:
            message = str(error)
            if message == "working_branch_not_found":
                return self._json_response(404, {"error": "working_branch_not_found", "task_id": task_id})
            if message.startswith("merge_target_not_found:"):
                return self._json_response(404, {"error": "merge_target_not_found", "task_id": task_id})
            if message == "merge_target_not_allowed":
                return self._json_response(400, {"error": "merge_target_not_allowed", "task_id": task_id})
            return self._json_response(502, {"error": message, "task_id": task_id})
        return self._json_response(200, {"task_id": task_id, "merge_target": task.target_ref, "diff": diff_text})

    def _approve_task(self, task_id: int, body: str | None, headers: dict[str, str]) -> dict:
        csrf_error = self._validate_state_changing_request(headers)
        if csrf_error is not None:
            return csrf_error
        payload = self._parse_json_dict(body)
        if payload is None:
            return self._json_response(400, {"error": "invalid_json"})

        with self._database() as connection:
            accessor = MariaDBAccessor(connection, workspace_root=self.workspace_root)
            try:
                task = accessor.select_task_for_artifact_apply(task_id)
            except TaskConsistencyError:
                return self._json_response(404, {"error": "task_not_found", "task_id": task_id})
            if task.status != "waiting_approval":
                return self._json_response(409, {"error": "invalid_task_state", "task_id": task_id, "status": task.status})
            if not task.target_repo or not task.workspace_path or not task.working_branch:
                return self._json_response(400, {"error": "approval_not_supported", "task_id": task_id})
            if not task.target_ref:
                return self._json_response(400, {"error": "invalid_target_ref", "task_id": task_id})
            try:
                merge_result = self.repository_workspace.merge_and_cleanup(
                    workspace_path=task.workspace_path,
                    working_branch=task.working_branch,
                    merge_target=task.target_ref,
                )
            except CommitPushError as error:
                message = str(error)
                if message == "working_branch_not_found":
                    return self._json_response(404, {"error": "working_branch_not_found", "task_id": task_id})
                if message == "merge_target_not_allowed":
                    return self._json_response(400, {"error": "merge_target_not_allowed", "task_id": task_id})
                if message.startswith("merge_target_not_found:"):
                    return self._json_response(404, {"error": "merge_target_not_found", "task_id": task_id})
                blocked = accessor.update_task_status(task.id, "waiting_approval", "blocked")
                if not blocked:
                    raise TaskConsistencyError(f"task {task.id} could not be blocked after approval failure")
                accessor.insert_log(
                    task.id,
                    task.root_task_id,
                    "dashboard",
                    self._map_approval_error_to_event(message),
                    f"Approval failed: {message}",
                    f"dashboard-{task_id}",
                )
                commit = getattr(connection, "commit", None)
                if callable(commit):
                    commit()
                return self._json_response(409, {"error": "approval_failed", "task_id": task_id, "details": message})
            completed = accessor.update_task_result(task.id, "waiting_approval", "succeeded", task.result_summary_md or "")
            if not completed:
                raise TaskConsistencyError(f"task {task.id} could not be updated after approval")
            accessor.insert_log(
                task.id,
                task.root_task_id,
                "dashboard",
                "task_approved",
                f"Merged {task.working_branch} into {merge_result['merge_target']}",
                f"dashboard-{task_id}",
            )
            commit = getattr(connection, "commit", None)
            if callable(commit):
                commit()
        return self._json_response(200, {"task": self._load_task(task_id), "merge": merge_result})

    def _reject_task(self, task_id: int, body: str | None, headers: dict[str, str]) -> dict:
        csrf_error = self._validate_state_changing_request(headers)
        if csrf_error is not None:
            return csrf_error
        payload = self._parse_json_dict(body)
        if payload is None:
            return self._json_response(400, {"error": "invalid_json"})
        reason = str(payload.get("reason") or "rejected by operator")

        with self._database() as connection:
            accessor = MariaDBAccessor(connection, workspace_root=self.workspace_root)
            try:
                task = accessor.select_task_for_artifact_apply(task_id)
            except TaskConsistencyError:
                return self._json_response(404, {"error": "task_not_found", "task_id": task_id})
            if task.status != "waiting_approval":
                return self._json_response(409, {"error": "invalid_task_state", "task_id": task_id, "status": task.status})
            blocked = accessor.update_task_status(task.id, "waiting_approval", "blocked")
            if not blocked:
                raise TaskConsistencyError(f"task {task.id} could not be rejected")
            accessor.insert_log(
                task.id,
                task.root_task_id,
                "dashboard",
                "task_rejected",
                f"Approval rejected: {reason}",
                f"dashboard-{task_id}",
            )
            commit = getattr(connection, "commit", None)
            if callable(commit):
                commit()
        return self._json_response(200, {"task": self._load_task(task_id), "reason": reason})

    def _get_approval_task(self, task_id: int):
        with self._database() as connection:
            accessor = MariaDBAccessor(connection, workspace_root=self.workspace_root)
            return accessor.select_task_for_artifact_apply(task_id)

    @staticmethod
    def _parse_json_dict(body: str | None) -> dict[str, Any] | None:
        try:
            payload = json.loads(body or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _validate_state_changing_request(self, headers: dict[str, str]) -> dict | None:
        origin = headers.get("Origin") or headers.get("origin")
        if origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1"}:
            return self._json_response(403, {"error": "csrf_origin_denied"})
        return None

    @staticmethod
    def _map_approval_error_to_event(message: str) -> str:
        if message.startswith("merge_conflict:"):
            return "merge_conflict"
        if message.startswith("merge_push_rejected:"):
            return "merge_push_rejected"
        if message.startswith("branch_cleanup_local_failed:"):
            return "branch_cleanup_local_failed"
        if message.startswith("branch_cleanup_remote_failed:"):
            return "branch_cleanup_remote_failed"
        if message.startswith("merge_target_not_found:"):
            return "merge_target_not_found"
        return "approval_failed"

    @contextmanager
    def _database(self):
        if self.db_connection is not None:
            yield self.db_connection
            return
        if self.connection_factory is None:
            raise RuntimeError("dashboard database unavailable")
        connection = self.connection_factory()
        try:
            yield connection
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    def _cursor(self, connection: Any):
        cursor_factory = getattr(connection, "cursor", None)
        if not callable(cursor_factory):
            raise RuntimeError("dashboard database unavailable")
        try:
            return cursor_factory(dictionary=True)
        except TypeError:
            return cursor_factory()

    def _fetchone(self, cursor) -> dict[str, Any] | None:
        row = cursor.fetchone()
        if row is None or isinstance(row, dict):
            return row
        description = getattr(cursor, "description", None)
        columns = [column[0] for column in description]
        return dict(zip(columns, row, strict=True))

    def _fetchall(self, cursor) -> list[dict[str, Any]]:
        fetchall = getattr(cursor, "fetchall", None)
        rows = fetchall() if callable(fetchall) else []
        if not rows:
            return []
        if isinstance(rows[0], dict):
            return list(rows)
        description = getattr(cursor, "description", None)
        columns = [column[0] for column in description]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def _serialize_log_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        serialized: list[dict[str, Any]] = []
        for row in rows:
            details = row.get("details_json")
            if isinstance(details, str):
                try:
                    details = json.loads(details)
                except json.JSONDecodeError:
                    details = None
            serialized_row = dict(row)
            serialized_row["details_json"] = details
            serialized_row["details"] = details if isinstance(details, dict) else None
            serialized.append(serialized_row)
        return serialized

    def _last_insert_id(self, cursor, connection: Any) -> int:
        lastrowid = getattr(cursor, "lastrowid", None)
        if lastrowid is not None:
            return int(lastrowid)
        insert_id = getattr(connection, "insert_id", None)
        if callable(insert_id):
            return int(insert_id())
        raise RuntimeError("could not determine inserted task id")

    def _serialize_task_row(self, row: dict[str, Any]) -> dict[str, Any]:
        payload_json = row.get("payload_json")
        if isinstance(payload_json, str):
            try:
                payload_json = json.loads(payload_json)
            except json.JSONDecodeError:
                pass
        result = dict(row)
        result["payload_json"] = payload_json
        result["repository_path"] = (payload_json or {}).get("repository_path") or row.get("workspace_path")
        result["workspace_path"] = row.get("workspace_path")
        parent_task_id = row.get("parent_task_id")
        root_task_id = row.get("root_task_id")
        is_root = parent_task_id is None and row.get("id") == root_task_id
        result["is_root"] = is_root
        result["relationship_label"] = "root" if is_root else "child"
        orchestration = payload_json.get("orchestration") if isinstance(payload_json, dict) else None
        if isinstance(orchestration, dict):
            result["current_phase"] = orchestration.get("current_phase")
            result["last_completed_phase"] = orchestration.get("last_completed_phase")
            result["llm_model"] = orchestration.get("llm_model") or "N/A"
            result["handoff_message"] = orchestration.get("handoff_message") or "引き継ぎ事項なし"
            result["phase_summary"] = orchestration.get("phase_summary") or "-"
        else:
            result["llm_model"] = "N/A"
            result["handoff_message"] = "引き継ぎ事項なし"
            result["phase_summary"] = "-"
        result["instruction"] = (payload_json or {}).get("instruction") if isinstance(payload_json, dict) else None
        return result

    def _resolve_repository_context(self, repository_path: Any, target_ref: Any) -> dict[str, Any] | None:
        if repository_path in (None, ""):
            return None
        if not isinstance(repository_path, str):
            return None
        if not self.sandbox.validate_control_chars(repository_path):
            return None

        github_context = self._parse_github_repository_url(repository_path, target_ref)
        if github_context is not None:
            return github_context

        local_path = self._validate_local_repository_path(repository_path)
        if local_path is None:
            return None
        return {
            "repository_path": local_path,
            "workspace_path": local_path,
            "target_repo": None,
            "target_ref": None,
            "working_branch": None,
            "requires_clone": False,
        }

    def _validate_local_repository_path(self, repository_path: str) -> str | None:
        candidate = Path(repository_path)
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            return None
        if not resolved.is_dir():
            return None
        if resolved == Path("/").resolve(strict=False):
            return None
        if not self.sandbox.validate_workspace_path(str(resolved)):
            return None
        effective_banned_roots = tuple(
            banned_root
            for banned_root in self._banned_repository_roots
            if self.workspace_root != banned_root and not self.workspace_root.is_relative_to(banned_root)
        )
        if any(resolved == banned_root or resolved.is_relative_to(banned_root) for banned_root in effective_banned_roots):
            return None
        return str(resolved)

    def _normalize_github_repository_url(self, repository_path: Any) -> str | None:
        if not isinstance(repository_path, str):
            return None
        parsed = urlparse(repository_path)
        if parsed.scheme not in {"http", "https"}:
            return None
        if (parsed.hostname or "").lower() != "github.com":
            return None
        if parsed.params or parsed.query or parsed.fragment:
            return None
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) != 2:
            return None
        owner, repo_name = path_parts
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
        if not owner or not repo_name:
            return None
        if not self._is_safe_git_identifier(owner) or not self._is_safe_git_identifier(repo_name):
            return None
        return f"https://github.com/{owner}/{repo_name}.git"

    def _parse_github_repository_url(self, repository_path: str, target_ref: Any) -> dict[str, Any] | None:
        normalized_url = self._normalize_github_repository_url(repository_path)
        if normalized_url is None:
            return None
        normalized_ref = self._validate_target_ref(target_ref)
        if normalized_ref is None:
            return None
        target_repo = normalized_url.removeprefix("https://github.com/").removesuffix(".git")
        return {
            "repository_path": normalized_url,
            "workspace_path": None,
            "target_repo": target_repo,
            "target_ref": normalized_ref,
            "working_branch": None,
            "requires_clone": True,
        }

    def _finalize_repository_context(self, task_id: int, repository_context: dict[str, Any] | None) -> dict[str, Any] | None:
        if repository_context is None or not repository_context["requires_clone"]:
            return repository_context
        return {
            **repository_context,
            "workspace_path": f"/workspace/{task_id}",
            "working_branch": f"mn2/{task_id}/phase0",
        }

    @staticmethod
    def _is_safe_git_identifier(value: str) -> bool:
        if value in {".", ".."}:
            return False
        return all(char.isalnum() or char in {"-", "_", "."} for char in value)

    def _validate_target_ref(self, target_ref: Any) -> str | None:
        if target_ref in (None, ""):
            return None
        if not isinstance(target_ref, str):
            return None
        if not self.sandbox.validate_control_chars(target_ref):
            return None
        normalized = target_ref.strip()
        if not normalized or normalized.startswith("-"):
            return None
        if not all(char.isalnum() or char in {"-", "_", ".", "/"} for char in normalized):
            return None
        if normalized not in self.repository_workspace.allowed_merge_targets():
            return None
        return normalized

    def _serve_asset(self, relative_path: str) -> dict:
        if "\x00" in relative_path:
            return self._json_response(404, {"error": "asset_not_found", "path": relative_path})
        asset_path = (self.asset_root / relative_path).resolve()
        asset_root = self.asset_root.resolve()
        if not asset_path.is_file() or not asset_path.is_relative_to(asset_root):
            return self._json_response(404, {"error": "asset_not_found", "path": relative_path})

        content_type = self._TEXT_CONTENT_TYPES.get(asset_path.suffix, "application/octet-stream")
        body = asset_path.read_text(encoding="utf-8")
        return {
            "status": 200,
            "content_type": content_type,
            "body": body,
        }

    def _json_safe(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [self._json_safe(item) for item in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return value

    def _json_response(self, status: int, payload: dict) -> dict:
        safe_payload = self._json_safe(payload)
        return {
            "status": status,
            "content_type": "application/json",
            "body": json.dumps(safe_payload),
            "json": safe_payload,
        }