import json
from pathlib import Path

from security import SecretScanner, SecureDashboard


def test_dashboard_task_detail_returns_404_for_unknown_id(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request("GET", "/api/v1/tasks/999", body=None)

    assert response["status"] == 404
    assert response["content_type"] == "application/json"
    assert response["json"]["error"] == "task_not_found"


def test_dashboard_task_submission_with_secret_is_persisted_as_blocked(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({"task": "deploy", "instruction": "use token ghp_secret123456789"}),
    )

    created_task_id = response["json"]["task"]["id"]
    created_task = db_connection_mock.tasks[created_task_id]
    assert response["status"] == 201
    assert response["json"]["task"]["status"] == "blocked"
    assert created_task["status"] == "blocked"
    assert created_task["payload_json"]["security_scan"]["blocked"] is True


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
    assert created_task["payload_json"]["repository_path"] == str(repo_path)


def test_dashboard_task_detail_includes_logs_and_result(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())
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
    assert response["json"]["task"]["result_summary_md"] == "完了: README を作成しました"
    assert response["json"]["logs"][0]["event_type"] == "task_started"
