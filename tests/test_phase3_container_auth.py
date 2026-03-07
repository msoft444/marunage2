import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.prod.yml"
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