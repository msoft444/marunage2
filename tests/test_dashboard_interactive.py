import json

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