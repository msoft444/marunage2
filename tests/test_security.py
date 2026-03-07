from pathlib import Path

import pytest


def test_SS_01_multistage_secret_scan_blocks_nested_encoding(secret_scanner):
    result = secret_scanner.scan_multistage("Z2hwX2R1bW15")
    assert result["blocked"] is True and result["decode_depth"] >= 2


def test_SS_01b_hex_encoded_secret_is_detected(secret_scanner):
    result = secret_scanner.scan_multistage("6768705f64756d6d79")
    assert result["blocked"] is True


def test_SS_01c_url_encoded_secret_is_detected(secret_scanner):
    result = secret_scanner.scan_multistage("%67%68%70%5f%64%75%6d%6d%79")
    assert result["blocked"] is True


def test_SS_01d_qp_encoded_secret_is_detected(secret_scanner):
    result = secret_scanner.scan_multistage("=67=68=70=5F=64=75=6D=6D=79")
    assert result["blocked"] is True


def test_SS_02_unicode_homoglyphs_are_normalized(secret_scanner):
    result = secret_scanner.scan_unicode_identifier("ΡASSWORD", "dummy-secret")
    assert result["blocked"] is True and result["normalized"] is True


def test_SS_03_cross_file_fragments_are_detected(secret_scanner):
    result = secret_scanner.scan_cross_file_fragments(["ghp_dummy", "123456"])
    assert result["blocked"] is True and result["cross_file"] is True


def test_SS_04_binary_payloads_are_scanned(secret_scanner):
    result = secret_scanner.scan_binary_blob("fixture.png")
    assert result["blocked"] is True and result["raw_scan"] is True


def test_SS_05_amend_and_rebase_trigger_secret_hooks(secret_scanner):
    hooks = secret_scanner.supported_hooks()
    assert {"pre-commit", "commit-amend", "post-rewrite", "merge", "cherry-pick"}.issubset(hooks)


def test_SS_06_pre_push_scan_is_enabled(secret_scanner):
    result = secret_scanner.pre_push_scan()
    assert result["enabled"] is True


def test_SS_07_entropy_false_positive_has_escape_hatch(secret_scanner):
    result = secret_scanner.entropy_exception_flow()
    assert result["action"] == "human-review" and result["escalated"] is True


def test_JB_01_symlink_escape_is_blocked(sandbox):
    repo_dir = sandbox.workspace_root / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    safe_target = repo_dir / "safe.txt"
    safe_target.write_text("ok", encoding="utf-8")
    link_path = repo_dir / "link"
    link_path.symlink_to(safe_target)
    resolved = sandbox.resolve_symlink(str(link_path))
    assert Path(resolved).is_relative_to(sandbox.workspace_root)


def test_JB_01b_symlink_escape_target_is_rejected(sandbox, tmp_path):
    repo_dir = sandbox.workspace_root / "repo-escape"
    repo_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    link_path = repo_dir / "link"
    link_path.symlink_to(outside)
    with pytest.raises(ValueError):
        sandbox.resolve_symlink(str(link_path))


def test_JB_02_path_traversal_is_rejected(sandbox):
    allowed = sandbox.validate_relative_path("../../etc/passwd")
    assert allowed is False


def test_JB_03_outside_mounts_are_rejected(sandbox):
    allowed = sandbox.validate_mount_source("/")
    assert allowed is False


def test_JB_03b_prefix_collision_is_rejected(sandbox):
    allowed = sandbox.validate_mount_source(str(sandbox.workspace_root) + "-evil")
    assert allowed is False


def test_JB_04_privileged_flags_are_rejected(sandbox):
    allowed = sandbox.validate_docker_flags(["--privileged", "--pid=host"])
    assert allowed is False


def test_JB_05_workspace_path_is_normalized(sandbox):
    allowed = sandbox.validate_workspace_path("/workspace/../etc")
    assert allowed is False


def test_JB_06_submodule_path_must_stay_inside_repo(sandbox):
    allowed = sandbox.validate_submodule_path("../../outside")
    assert allowed is False


def test_UI_01_markdown_is_sanitized(dashboard):
    rendered = dashboard.render_markdown("<script>alert(1)</script>")
    assert "<script>" not in rendered


def test_UI_01b_event_handlers_and_javascript_urls_are_removed(dashboard):
    rendered = dashboard.render_markdown('<a href="javascript:alert(1)" onclick="alert(1)">x</a>')
    assert "onclick" not in rendered and "javascript:" not in rendered


def test_UI_02_timeline_is_paginated(dashboard):
    result = dashboard.fetch_timeline(10000)
    assert result["paginated"] is True and result["initial_load"] <= 100


def test_UI_03_approval_is_idempotent(dashboard):
    result = dashboard.approve_release()
    assert result["promote_release_tasks"] == 1


def test_FP_01_write_requires_manifest(file_ops):
    result = file_ops.write_with_manifest()
    assert result["manifest_written"] is True and result["write_started"] is False


def test_FP_02_concurrent_edit_is_blocked(file_ops):
    result = file_ops.concurrent_edit()
    assert result["lost_updates"] is False and result["blocked"] is True


def test_FP_03_marker_regeneration_preserves_foreign_regions(file_ops):
    result = file_ops.regenerate_markers()
    assert result["foreign_marker_destroyed"] is False


def test_CI_01_cli_profile_is_not_shell_expanded(command_runner):
    result = command_runner.launch_copilot("safe; rm -rf /")
    assert result["shell"] is False and result["blocked"] is True


def test_CI_02_workspace_path_rejects_control_characters(sandbox):
    allowed = sandbox.validate_control_chars("/workspace/1/repo\nmalicious")
    assert allowed is False


def test_CI_03_docker_command_strips_banned_flags(command_runner):
    command = command_runner.build_docker_command("image")
    assert "--privileged" not in command
