import json

from scripts.service_runner import DashboardHandler
from security import SecretScanner, SecureDashboard


def test_dashboard_task_list_escapes_user_controlled_values(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps(
            {
                "task_type": "<script>alert(1)</script>",
                "assigned_service": "brain\"><img src=x onerror=alert(2)>",
                "instruction": "render check",
            }
        ),
    )

    created_task = response["json"]["task"]
    assert created_task["task_type"] == "<script>alert(1)</script>"
    assert created_task["assigned_service"] == "brain\"><img src=x onerror=alert(2)>"

    app_js = SecureDashboard().serve_path("/static/js/app.js")["body"]
    assert "function escapeHtml(value)" in app_js
    assert "escapeHtml(task.task_type)" in app_js
    assert "escapeHtml(task.assigned_service)" in app_js
    assert "escapeHtml(log.message)" in app_js


def test_dashboard_create_task_rejects_invalid_integer_fields(db_connection_mock):
    dashboard = SecureDashboard(db_connection=db_connection_mock, secret_scanner=SecretScanner())

    response = dashboard.serve_request(
        "POST",
        "/api/v1/tasks",
        body=json.dumps({"task": "broken", "instruction": "bad", "phase": "invalid"}),
    )

    assert response["status"] == 400
    assert response["json"] == {"error": "invalid_payload", "field": "phase"}


def test_dashboard_handler_rejects_oversized_post_body_without_reading_payload():
    send_error_calls = []
    read_sizes = []

    class OversizedStream:
        def read(self, size=-1):
            read_sizes.append(size)
            return b"{}"

    handler = type("HandlerDouble", (), {})()
    handler.headers = {"Content-Length": str(10 * 1024 * 1024 + 1)}
    handler.rfile = OversizedStream()
    handler.dashboard = SecureDashboard()
    handler._send_dashboard_response = lambda response: (_ for _ in ()).throw(AssertionError("must not send dashboard body"))
    handler.send_error = lambda status, message=None: send_error_calls.append((status, message))

    DashboardHandler.do_POST(handler)

    assert send_error_calls == [(413, "Payload Too Large")]
    assert read_sizes == []


def test_dashboard_handler_sets_security_headers():
    sent_headers = []
    body_chunks = []

    class Writer:
        def write(self, body):
            body_chunks.append(body)

    handler = type("HandlerDouble", (), {})()
    handler.wfile = Writer()
    handler.send_response = lambda status: sent_headers.append((":status", str(status)))
    handler.send_header = lambda key, value: sent_headers.append((key, value))
    handler.end_headers = lambda: sent_headers.append((":end", ""))

    DashboardHandler._send_dashboard_response(
        handler,
        {
            "status": 200,
            "content_type": "application/json",
            "body": "{}",
        },
    )

    assert ("Content-Security-Policy", "default-src 'self'") in sent_headers
    assert ("X-Frame-Options", "DENY") in sent_headers
    assert body_chunks == [b"{}"]
