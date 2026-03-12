import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.yml"
TEST_COMPOSE_FILE = ROOT / "docker-compose.test.yml"
ENTRYPOINT = ROOT / "scripts" / "entrypoint.sh"
INIT_RUNTIME = ROOT / "scripts" / "init_runtime.sh"
README_RUNTIME = ROOT / "scripts" / "README-runtime.txt"


def test_phase3_compose_uses_github_token_for_all_app_services_without_legacy_auth_paths():
    content = COMPOSE_FILE.read_text(encoding="utf-8")

    assert ".copilot" not in content
    assert "HOST_COPILOT_CONFIG_DIR" not in content
    assert "COPILOT_CONFIG_DIR" not in content
    assert "GITHUB_TOKEN_FILE" not in content
    assert "github_token:" not in content
    assert content.count("GITHUB_TOKEN:") >= 4


def test_phase3_prod_mariadb_uses_runtime_env_file_for_db_identity():
    content = COMPOSE_FILE.read_text(encoding="utf-8")

    assert "name: marunage2-prod" in content
    assert "mariadb:\n    image: mariadb:11.8\n    restart: unless-stopped\n    env_file:" in content
    assert "- ${RUNTIME_ENV_FILE:-.env.runtime}" in content
    assert "MARIADB_DATABASE: ${DB_NAME}" in content
    assert "MARIADB_USER: ${DB_USER}" in content


def test_phase3_test_compose_uses_separate_project_name_from_prod():
    prod_content = COMPOSE_FILE.read_text(encoding="utf-8")
    test_content = TEST_COMPOSE_FILE.read_text(encoding="utf-8")

    assert "name: marunage2-prod" in prod_content
    assert "name: marunage2-test" in test_content


def test_phase3_dashboard_has_workspace_volume_mount():
    """Dashboard needs /workspace access for diff, merge-targets, and approve APIs."""
    content = COMPOSE_FILE.read_text(encoding="utf-8")
    # Find the dashboard service block and check for workspace mount
    in_dashboard = False
    in_next_service = False
    dashboard_lines = []
    for line in content.splitlines():
        if line.strip() == "dashboard:":
            in_dashboard = True
            continue
        if in_dashboard:
            # A new top-level service starts with 2-space indent + name + colon
            if line and not line.startswith("    ") and not line.startswith("  ") and line.strip():
                break
            if line and not line.startswith("    ") and line.strip().endswith(":") and not line.startswith("      "):
                break
            dashboard_lines.append(line)
    dashboard_block = "\n".join(dashboard_lines)
    assert "./workspace:/workspace" in dashboard_block, (
        "dashboard service must mount ./workspace:/workspace for approval workflow"
    )


def test_runtime_dockerfile_installs_copilot_cli_without_gh_cli():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "gh.io/copilot-install" in dockerfile
    assert "copilot help" in dockerfile or "command -v copilot" in dockerfile
    assert "gh-copilot" not in dockerfile


def test_phase3_init_runtime_stops_requesting_github_token_secret_file():
    content = INIT_RUNTIME.read_text(encoding="utf-8")

    assert "github_token" not in content
    assert "gh auth login" in content
    assert "gh auth token" in content


def test_phase3_runtime_readme_uses_gh_auth_login_flow():
    content = README_RUNTIME.read_text(encoding="utf-8")

    assert "gh auth login" in content
    assert "github_token" not in content
    assert "gh auth token" in content or "run_prod_with_gh_auth" in content


@pytest.mark.parametrize("service_name", ["dashboard", "librarian"])
def test_entrypoint_requires_github_token_for_all_application_services(service_name):
    env = {
        "PATH": os.environ["PATH"],
        "DB_HOST": "localhost",
        "DB_PORT": "3306",
        "DB_NAME": "marunage2",
        "DB_USER": "marunage",
        "DB_PASSWORD": "dummy",
        "REQUIRED_ENV_VARS": "DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD",
    }

    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "python", "scripts/service_runner.py", service_name],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "missing required environment variables" in result.stderr
    assert "GITHUB_TOKEN" in result.stderr


def test_entrypoint_no_longer_requires_copilot_mount_variables_for_brain():
    env = {
        "PATH": os.environ["PATH"],
        "DB_HOST": "localhost",
        "DB_PORT": "3306",
        "DB_NAME": "marunage2",
        "DB_USER": "marunage",
        "DB_PASSWORD": "dummy",
        "TARGET_REPO": "msoft444/marunage2",
        "TARGET_REF": "main",
        "REQUIRED_ENV_VARS": "DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD",
    }

    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "python", "scripts/service_runner.py", "brain"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "GITHUB_TOKEN" in result.stderr
    assert "COPILOT_CONFIG_DIR" not in result.stderr
    assert "COPILOT_API_KEY" not in result.stderr


def test_entrypoint_brain_requires_copilot_command_when_github_token_is_present():
    env = {
        "PATH": "/usr/bin:/bin",
        "DB_HOST": "localhost",
        "DB_PORT": "3306",
        "DB_NAME": "marunage2",
        "DB_USER": "marunage",
        "DB_PASSWORD": "dummy",
        "TARGET_REPO": "msoft444/marunage2",
        "TARGET_REF": "main",
        "GITHUB_TOKEN": "token-123",
        "REQUIRED_ENV_VARS": "DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD",
    }

    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "python", "scripts/service_runner.py", "brain"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "copilot" in result.stderr
    assert "not installed" in result.stderr


def test_entrypoint_no_longer_requires_copilot_mount_variables_for_guardian():
    env = {
        "PATH": os.environ["PATH"],
        "DB_HOST": "localhost",
        "DB_PORT": "3306",
        "DB_NAME": "marunage2",
        "DB_USER": "marunage",
        "DB_PASSWORD": "dummy",
        "REQUIRED_ENV_VARS": "DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD",
    }

    result = subprocess.run(
        ["bash", str(ENTRYPOINT), "python", "scripts/service_runner.py", "guardian"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "GITHUB_TOKEN" in result.stderr
    assert "COPILOT_CONFIG_DIR" not in result.stderr
    assert "COPILOT_API_KEY" not in result.stderr


def test_gh_auth_launcher_errors_when_gh_is_missing():
    from scripts.gh_token_compose import resolve_github_token

    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("gh")

    with pytest.raises(RuntimeError, match="gh.*not installed"):
        resolve_github_token(run_command=fake_run)


def test_gh_auth_launcher_errors_when_gh_auth_token_fails():
    from scripts.gh_token_compose import resolve_github_token

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["gh", "auth", "token"], returncode=1, stdout="", stderr="not logged in")

    with pytest.raises(RuntimeError, match="gh auth token failed"):
        resolve_github_token(run_command=fake_run)


def test_gh_auth_launcher_errors_when_token_is_empty():
    from scripts.gh_token_compose import resolve_github_token

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=["gh", "auth", "token"], returncode=0, stdout="  \n", stderr="")

    with pytest.raises(RuntimeError, match="empty GITHUB_TOKEN"):
        resolve_github_token(run_command=fake_run)


def test_gh_auth_launcher_injects_token_into_compose_environment():
    from scripts.gh_token_compose import build_compose_environment

    env = build_compose_environment({"DB_HOST": "mariadb"}, "token-123")

    assert env["DB_HOST"] == "mariadb"
    assert env["GITHUB_TOKEN"] == "token-123"