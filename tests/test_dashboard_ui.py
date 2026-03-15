from security import SecureDashboard

import pytest


def _extract_css_rule(body: str, selector: str) -> str:
    selector_index = body.index(selector)
    block_start = body.index("{", selector_index)
    block_end = body.index("}", block_start)
    return body[block_start + 1:block_end]


def _extract_css_property_names(rule: str) -> set[str]:
    property_names = set()
    for line in rule.splitlines():
        stripped_line = line.strip()
        if not stripped_line or ":" not in stripped_line:
            continue
        property_names.add(stripped_line.split(":", 1)[0])
    return property_names


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
    assert 'id="task-detail-instruction-panel"' in response["body"]
    assert 'id="task-detail-instruction"' in response["body"]
    assert '<h4>サブタスク</h4>' in response["body"]
    assert '<h4>ログ</h4>' in response["body"]
    assert '<h4>結果</h4>' in response["body"]
    assert '<h4>承認</h4>' in response["body"]
    assert 'id="task-repository-path"' in response["body"]
    assert 'id="task-target-ref"' in response["body"]
    assert 'id="task-approval-panel"' in response["body"]
    assert 'class="approval-actions"' in response["body"]
    assert 'id="task-subtasks-empty"' in response["body"]
    assert 'https://github.com/' in response["body"]


def test_dashboard_root_task_detail_sections_follow_reading_order():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/")

    body = response["body"]
    meta_index = body.index('id="task-detail-meta"')
    instruction_index = body.index('id="task-detail-instruction-panel"')
    subtasks_index = body.index('id="task-subtasks-panel"')
    logs_index = body.index('id="task-detail-logs"')
    result_index = body.index('id="task-detail-result"')
    approval_index = body.index('id="task-approval-panel"')

    assert meta_index < instruction_index < subtasks_index < logs_index < result_index < approval_index


def test_dashboard_task_form_exposes_single_repository_input():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/")

    assert response["body"].count('id="task-repository-path"') == 1
    assert response["body"].count('name="repository_path"') == 1
    assert response["body"].count('id="task-target-ref"') == 1
    assert response["body"].count('name="target_ref"') == 1
    assert response["body"].count('<span>対象リポジトリ</span>') == 1
    assert response["body"].count('<span>元ブランチ</span>') == 1
    assert 'placeholder="例: /workspace/repo-a または https://github.com/org/repo"' in response["body"]
    assert '候補ブランチを選択' in response["body"]


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
    assert "repository_path" in response["body"]
    assert "/api/v1/repositories/branches" in response["body"]
    assert "/approve" in response["body"]
    assert "/reject" in response["body"]
    assert "approvalPayload.task" in response["body"]
    assert "rejectPayload.task" in response["body"]
    assert "hideApprovalPanel" in response["body"]
    assert "working_branch_not_found" in response["body"]
    assert "target_ref" in response["body"]
    assert "payload.subtasks" in response["body"]
    assert "payload.task.instruction" in response["body"]
    assert "renderSubtaskAccordion" in response["body"]
    assert "引き継ぎ事項なし" in response["body"]
    assert "N/A" in response["body"]
    assert "task-detail-instruction" in response["body"]
    assert "現在フェーズ" in response["body"]
    assert "最終完了フェーズ" in response["body"]
    assert "状態" in response["body"]
    assert "担当サービス" in response["body"]
    assert "種別" in response["body"]
    assert "対象リポジトリ" in response["body"]
    assert "元ブランチ" in response["body"]
    assert "要約" in response["body"]
    assert "結果なし" in response["body"]
    assert "ログなし" in response["body"]
    assert '<li class="task-empty">ログなし</li>' in response["body"]
    assert '<p>結果なし</p>' in response["body"]
    assert "compose_validation_blocked" in response["body"]
    assert "Compose Validation により安全側でブロック" in response["body"]
    assert "violations" in response["body"]
    assert "violation 詳細なし" in response["body"]
    assert "escapeHtml(formatLogValue(violation.compose_file))" in response["body"]
    assert "escapeHtml(formatLogValue(violation.service))" in response["body"]
    assert "escapeHtml(formatLogValue(violation.field))" in response["body"]
    assert "escapeHtml(formatLogValue(violation.rule_id))" in response["body"]
    assert "escapeHtml(formatLogValue(violation.raw_value))" in response["body"]
    assert "escapeHtml(formatLogValue(violation.message))" in response["body"]
    assert "ファイル" in response["body"]
    assert "フィールド" in response["body"]
    assert "ルール" in response["body"]
    assert "引き継ぎ事項なし" in response["body"]
    assert "subtask-list" in response["body"]
    assert "subtask-accordion" in response["body"]
    assert "subtask-summary" in response["body"]
    assert "マージ済みまたは却下済みのため、承認操作はできません。" in response["body"]
    assert "task-merge-target" not in response["body"]


def test_dashboard_static_asset_is_served_with_css_mime_type_and_approval_styles():
    dashboard = SecureDashboard()

    response = dashboard.serve_path("/static/css/app.css")
    paragraph_rule = _extract_css_rule(response["body"], ".detail-reading-panel p")
    hover_rule = _extract_css_rule(response["body"], ".detail-reading-panel:hover")
    disabled_rule = _extract_css_rule(response["body"], ".detail-reading-panel.is-disabled")
    violation_wrap_rule = _extract_css_rule(response["body"], ".log-violation-wrap")
    violation_table_cells_rule = _extract_css_rule(response["body"], ".log-violation-table td")
    hover_properties = _extract_css_property_names(hover_rule)
    disabled_properties = _extract_css_property_names(disabled_rule)

    assert response["status"] == 200
    assert response["content_type"] == "text/css; charset=utf-8"
    assert "#task-diff-preview" in response["body"]
    assert ".approval-actions" in response["body"]
    assert "#task-approve" in response["body"]
    assert "#task-reject" in response["body"]
    assert ".subtask-list" in response["body"]
    assert ".subtask-item" in response["body"]
    assert ".subtask-accordion" in response["body"]
    assert ".subtask-summary" in response["body"]
    assert ".log-blocked-reason" in response["body"]
    assert ".log-violation-table" in response["body"]
    assert ".task-status-blocked" in response["body"]
    assert ".task-status-failed" in response["body"]
    assert ".detail-text-block" in response["body"]
    assert ".detail-reading-panel" in response["body"]
    assert ".detail-section-title" in response["body"]
    assert "white-space: pre-wrap" in response["body"]
    assert "overflow-y: auto" in response["body"]
    assert "line-height: 1.6" in response["body"]
    assert "padding: 18px" in response["body"]
    assert "color: #3a2f22" in response["body"]
    assert "background: #faf6ef" in response["body"]
    assert "border: 1px solid #d5c3a5" in response["body"]
    assert ".detail-reading-panel:hover" in response["body"]
    assert ".detail-reading-panel:focus-within" in response["body"]
    assert ".detail-reading-panel.is-disabled" in response["body"]
    assert "opacity: 0.72" in response["body"]
    assert "box-shadow: 0 0 0 1px rgba(134, 110, 74, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.82)" in response["body"]
    assert "color: inherit" in paragraph_rule
    assert "max-height: 400px" in violation_wrap_rule
    assert "overflow-y: auto" in violation_wrap_rule
    assert "word-break: break-all" in violation_table_cells_rule
    assert "overflow-wrap: break-word" in violation_table_cells_rule
    assert "background: #fdfaf4" in hover_rule
    assert "border-color: #c5ae87" in hover_rule
    assert "box-shadow: 0 12px 24px rgba(110, 91, 60, 0.08), 0 0 0 1px rgba(134, 110, 74, 0.12), inset 0 1px 0 rgba(255, 255, 255, 0.92)" in hover_rule
    assert "color" not in hover_properties
    assert "opacity: 0.72" in disabled_rule
    assert "background: #f4ede2" in disabled_rule
    assert "border-color: #d7c9b4" in disabled_rule
    assert "color" not in disabled_properties


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
