from security import SecureDashboard

import pytest


def test_dashboard_root_serves_marunage_ui_document():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/")

    assert response["status"] == 200
    assert response["content_type"] == "text/html; charset=utf-8"
    assert "<title>Maru-nage v2 Dashboard</title>" in response["body"]
    assert 'id="marunage-app"' in response["body"]
    assert 'id="sidebar-nav"' in response["body"]
    assert 'id="task-form"' in response["body"]
    assert 'id="task-detail-view"' in response["body"]


def test_dashboard_index_html_prefers_static_ui_over_api_json():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/index.html")

    assert response["status"] == 200
    assert response["content_type"] == "text/html; charset=utf-8"
    assert response["body"].lstrip().startswith("<!doctype html>")
    assert '"service": "dashboard"' not in response["body"]


def test_dashboard_static_asset_is_served_with_javascript_mime_type():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/static/js/app.js")

    assert response["status"] == 200
    assert response["content_type"] == "application/javascript; charset=utf-8"
    assert "fetchJson('/api/v1/tasks'" in response["body"]
    assert "window.location.hash" in response["body"]


def test_dashboard_api_prefix_returns_json_payload():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/api/v1/health")

    assert response["status"] == 200
    assert response["content_type"] == "application/json"
    assert response["json"] == {
        "service": "dashboard",
        "status": "ok",
        "path": "/api/v1/health",
    }


@pytest.mark.parametrize("malicious_path", [
    "/static/../../../etc/passwd",
    "/static/%2e%2e/%2e%2e/etc/passwd",
    "/static/\x00hidden",
    "/static/../secrets/db_password.txt",
])
def test_static_path_traversal_returns_404(malicious_path):
    dashboard = SecureDashboard()

    response = dashboard.serve_path(malicious_path)

    assert response["status"] == 404


@pytest.mark.parametrize("malicious_path", [
    "/api/v1/../../../etc/passwd",
    "/api/v1/../../secrets/db_password.txt",
])
def test_api_traversal_via_dotdot_does_not_match_api_prefix(malicious_path):
    dashboard = SecureDashboard()

    response = dashboard.serve_path(malicious_path)

    assert response["status"] == 404
    assert response["content_type"] == "application/json"


def test_unknown_route_returns_404():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/random/nonexistent")

    assert response["status"] == 404