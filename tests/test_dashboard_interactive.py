import json
from datetime import datetime
from pathlib import Path

from backend.repository_workspace import CommitPushError
from security import SecretScanner, SecureDashboard


def test_dashboard_task_detail_returns_404_for_unknown_id(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request("GET", "/api/v1/tasks/999", body=None)

    assert response["status"] == 404
    assert response["content_type"] == "application/json"
    assert response["json"]["error"] == "task_not_found"


def test_dashboard_task_submission_bypasses_secret_scanner_for_development_requests(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({"task": "deploy", "instruction": "use token ghp_secret123456789"}),
    )

    created_task_id = response["json"]["task"]["id"]
    created_task = db_connection_mock.tasks[created_task_id]
    assert response["status"] == 201
    assert response["json"]["task"]["status"] == "queued"
    assert created_task["status"] == "queued"
    assert created_task["payload_json"]["security_scan"]["blocked"] is False
    assert created_task["payload_json"]["security_scan"]["disabled"] is True
    assert created_task["payload_json"]["security_scan"]["reason"] == "development_task_requests_bypass_secret_scanner"
    assert db_connection_mock.logs[-1]["event_type"] == "task_submitted"


def test_dashboard_lists_newly_submitted_task(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    create_response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({"task": "README作成", "instruction": "全ソースを解析して README を整備"}),
    )
    list_response = dashboard.serve_request("GET", "/api/v1/tasks", body=None)

    assert create_response["status"] == 201
    created_task_id = create_response["json"]["task"]["id"]
    assert any(task["id"] == created_task_id for task in list_response["json"]["tasks"])


def test_dashboard_rejects_nonexistent_repository_path(db_connection_mock, tmp_path):
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": str(tmp_path / "missing-repo"),
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_repository_path"}


def test_dashboard_rejects_system_repository_path(db_connection_mock, tmp_path):
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": "/etc",
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_repository_path"}


def test_dashboard_rejects_system_repository_subdirectory(db_connection_mock, tmp_path):
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": "/etc/nginx",
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_repository_path"}


def test_dashboard_rejects_repository_path_outside_workspace_root(db_connection_mock, tmp_path):
    repo_path = tmp_path.parent / "external-repo"
    repo_path.mkdir(exist_ok=True)
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": str(repo_path),
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_repository_path"}


def test_dashboard_rejects_non_github_repository_url(db_connection_mock, tmp_path):
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": "https://example.com/org/repo.git",
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_repository_path"}


def test_dashboard_rejects_github_repository_url_with_dot_segments(db_connection_mock, tmp_path):
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": "https://github.com/../repo",
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_repository_path"}


def test_dashboard_rejects_github_repository_url_without_target_ref(db_connection_mock, tmp_path):
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": "https://github.com/example/project",
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_target_ref"}


def test_dashboard_rejects_subdirectory_of_first_effective_banned_root(db_connection_mock, tmp_path):
    workspace_root = tmp_path / "workspace"
    banned_root = workspace_root / "restricted"
    repo_path = banned_root / "repo-a"
    repo_path.mkdir(parents=True)
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=workspace_root,
    )
    dashboard._banned_repository_roots = (
        Path("/").resolve(strict=False),
        banned_root.resolve(strict=False),
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": str(repo_path),
        }),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_repository_path"}


def test_dashboard_persists_github_repository_url_and_clone_metadata(db_connection_mock, tmp_path):
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": "https://github.com/example/project",
            "target_ref": "develop",
        }),
    )

    created_task_id = response["json"]["task"]["id"]
    created_task = db_connection_mock.tasks[created_task_id]
    assert response["status"] == 201
    assert response["json"]["task"]["phase"] == 0
    assert response["json"]["task"]["repository_path"] == "https://github.com/example/project.git"
    assert response["json"]["task"]["workspace_path"] == f"/workspace/{created_task_id}"
    assert created_task["workspace_path"] == f"/workspace/{created_task_id}"
    assert created_task["target_repo"] == "example/project"
    assert created_task["target_ref"] == "develop"
    assert created_task["working_branch"] == f"mn2/{created_task_id}/phase0"
    assert created_task["approval_required"] is True
    assert created_task["payload_json"]["repository_path"] == "https://github.com/example/project.git"
    assert created_task["payload_json"]["phase_flow"] == [0, 1, 2, 3, 4, 5]


def test_dashboard_persists_repository_path_and_exposes_it(db_connection_mock, tmp_path):
    repo_path = tmp_path / "repo-a"
    repo_path.mkdir()
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        workspace_root=tmp_path,
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({
            "task": "README作成",
            "instruction": "整備",
            "repository_path": str(repo_path),
        }),
    )

    created_task_id = response["json"]["task"]["id"]
    created_task = db_connection_mock.tasks[created_task_id]
    assert response["status"] == 201
    assert response["json"]["task"]["repository_path"] == str(repo_path)
    assert created_task["workspace_path"] == str(repo_path)
    assert created_task["approval_required"] is False
    assert created_task["payload_json"]["repository_path"] == str(repo_path)


def test_dashboard_returns_allowed_repository_branches_for_repository_url(db_connection_mock):
    class FakeRepositoryWorkspace:
        def list_repository_branches(self, repository_url: str):
            assert repository_url == "https://github.com/example/project.git"
            return ["main", "develop"]

    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        repository_workspace_manager=FakeRepositoryWorkspace(),
    )

    response = dashboard.serve_request(
        "GET",
        "/api/v1/repositories/branches?repository_url=https://github.com/example/project",
        body=None,
    )

    assert response["status"] == 200
    assert response["json"] == {"branches": ["main", "develop"], "default_branch": "main"}


def test_dashboard_returns_diff_for_waiting_approval_task(db_connection_mock, tmp_path):
    class FakeRepositoryWorkspace:
        def get_diff(self, **kwargs):
            assert kwargs["workspace_path"] == str(tmp_path / "1")
            assert kwargs["working_branch"] == "mn2/1/phase0"
            assert kwargs["merge_target"] == "develop"
            return "diff --git a/README.md b/README.md\n+hello\n"

    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "develop"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        repository_workspace_manager=FakeRepositoryWorkspace(),
    )

    response = dashboard.serve_request("GET", "/api/v1/tasks/1/diff?target=main", body=None)

    assert response["status"] == 200
    assert response["json"]["merge_target"] == "develop"
    assert "README.md" in response["json"]["diff"]


def test_dashboard_returns_404_when_waiting_task_working_branch_is_missing_for_diff(db_connection_mock, tmp_path):
    class FakeRepositoryWorkspace:
        def get_diff(self, **_kwargs):
            raise CommitPushError("working_branch_not_found")

    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        repository_workspace_manager=FakeRepositoryWorkspace(),
    )

    response = dashboard.serve_request("GET", "/api/v1/tasks/1/diff?target=main", body=None)

    assert response["status"] == 404
    assert response["json"] == {"error": "working_branch_not_found", "task_id": 1}


def test_dashboard_approves_waiting_task_and_marks_succeeded(db_connection_mock, tmp_path):
    class FakeRepositoryWorkspace:
        def merge_and_cleanup(self, **kwargs):
            assert kwargs["workspace_path"] == str(tmp_path / "1")
            assert kwargs["working_branch"] == "mn2/1/phase0"
            assert kwargs["merge_target"] == "develop"
            return {"merge_target": "develop", "working_branch": "mn2/1/phase0", "deleted_local_branch": True, "deleted_remote_branch": True}

    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "develop"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
    db_connection_mock.tasks[1]["result_summary_md"] = "READMEを更新しました"
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        repository_workspace_manager=FakeRepositoryWorkspace(),
    )

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks/1/approve",
        body=json.dumps({"merge_target": "main"}),
        headers={"Origin": "http://localhost"},
    )

    assert response["status"] == 200
    assert response["json"]["task"]["status"] == "succeeded"
    assert db_connection_mock.tasks[1]["status"] == "succeeded"
    db_connection_mock.commit.assert_called()
    assert any(log["event_type"] == "task_approved" for log in db_connection_mock.logs)


def test_dashboard_rejects_waiting_task_and_marks_blocked(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks/1/reject",
        body=json.dumps({"reason": "manual rejection"}),
        headers={"Origin": "http://localhost"},
    )

    assert response["status"] == 200
    assert response["json"]["task"]["status"] == "blocked"
    assert db_connection_mock.tasks[1]["status"] == "blocked"
    db_connection_mock.commit.assert_called()
    assert any(log["event_type"] == "task_rejected" for log in db_connection_mock.logs)


def test_dashboard_rejects_approve_for_non_waiting_task(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "running"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks/1/approve",
        body=json.dumps({"merge_target": "main"}),
        headers={"Origin": "http://localhost"},
    )

    assert response["status"] == 409
    assert response["json"]["error"] == "invalid_task_state"


def test_dashboard_returns_404_when_approving_unknown_task(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks/999/approve",
        body=json.dumps({"merge_target": "main"}),
        headers={"Origin": "http://localhost"},
    )

    assert response["status"] == 404
    assert response["json"]["error"] == "task_not_found"


def test_dashboard_rejects_double_approve_after_success(db_connection_mock, tmp_path):
    class FakeRepositoryWorkspace:
        def merge_and_cleanup(self, **kwargs):
            return {"merge_target": "develop", "working_branch": "mn2/1/phase0", "deleted_local_branch": True, "deleted_remote_branch": True}

    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "develop"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
    dashboard = SecureDashboard(
        db_connection=db_connection_mock,
        secret_scanner=SecretScanner(),
        repository_workspace_manager=FakeRepositoryWorkspace(),
    )

    first_response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks/1/approve",
        body=json.dumps({"merge_target": "main"}),
        headers={"Origin": "http://localhost"},
    )
    second_response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks/1/approve",
        body=json.dumps({"merge_target": "main"}),
        headers={"Origin": "http://localhost"},
    )

    assert first_response["status"] == 200
    assert second_response["status"] == 409
    assert second_response["json"]["error"] == "invalid_task_state"


def test_dashboard_rejects_cross_origin_approve_request(db_connection_mock, tmp_path):
    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["workspace_path"] = str(tmp_path / "1")
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "main"
    db_connection_mock.tasks[1]["working_branch"] = "mn2/1/phase0"
    db_connection_mock.tasks[1]["approval_required"] = True
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks/1/approve",
        body=json.dumps({"merge_target": "main"}),
        headers={"Origin": "https://evil.example"},
    )

    assert response["status"] == 403
    assert response["json"]["error"] == "csrf_origin_denied"


def test_dashboard_task_detail_includes_logs_and_result(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())
    db_connection_mock.tasks[1]["status"] = "waiting_approval"
    db_connection_mock.tasks[1]["target_repo"] = "example/project"
    db_connection_mock.tasks[1]["target_ref"] = "develop"
    db_connection_mock.tasks[1]["payload_json"] = {"repository_path": "https://github.com/example/project.git"}
    db_connection_mock.tasks[1]["result_summary_md"] = "完了: README を作成しました"
    db_connection_mock.logs.append(
        {
            "task_id": 1,
            "root_task_id": 1,
            "service": "brain",
            "event_type": "task_started",
            "message": "Task processing started",
        }
    )

    response = dashboard.serve_request("GET", "/api/v1/tasks/1", body=None)

    assert response["status"] == 200
    assert response["json"]["task"]["id"] == 1
    assert response["json"]["task"]["repository_path"] == "https://github.com/example/project.git"
    assert response["json"]["task"]["target_ref"] == "develop"
    assert response["json"]["task"]["result_summary_md"] == "完了: README を作成しました"
    assert response["json"]["logs"][0]["event_type"] == "task_started"


def test_dashboard_serializes_datetime_fields_in_task_responses(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())
    created_at = datetime(2026, 3, 8, 12, 34, 56)
    db_connection_mock.tasks[1]["created_at"] = created_at
    db_connection_mock.tasks[1]["started_at"] = created_at
    db_connection_mock.logs.append(
        {
            "task_id": 1,
            "root_task_id": 1,
            "service": "brain",
            "event_type": "task_started",
            "message": "Task processing started",
            "created_at": created_at,
        }
    )

    detail_response = dashboard.serve_request("GET", "/api/v1/tasks/1", body=None)
    list_response = dashboard.serve_request("GET", "/api/v1/tasks", body=None)

    assert detail_response["status"] == 200
    assert detail_response["json"]["task"]["created_at"] == "2026-03-08T12:34:56"
    assert detail_response["json"]["task"]["started_at"] == "2026-03-08T12:34:56"
    assert detail_response["json"]["logs"][0]["created_at"] == "2026-03-08T12:34:56"
    assert list_response["status"] == 200
    assert any(task["created_at"] == "2026-03-08T12:34:56" for task in list_response["json"]["tasks"] if task["id"] == 1)
