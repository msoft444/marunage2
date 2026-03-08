import subprocess

import pytest

from backend.llm_client import (
    LLMAuthenticationError,
    LLMClient,
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMRateLimitError,
    LLMServiceError,
    LLMTimeoutError,
)


def test_llm_client_from_environment_requires_github_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("COPILOT_CLI_COMMAND", raising=False)

    with pytest.raises(LLMConfigurationError, match="GITHUB_TOKEN is not configured"):
        LLMClient.from_environment()


def test_llm_client_from_environment_uses_copilot_command(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    monkeypatch.setenv("COPILOT_CLI_COMMAND", "copilot --no-color")
    monkeypatch.setenv("LLM_TIMEOUT_SEC", "120")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")

    client = LLMClient.from_environment()

    assert client.command == ("copilot", "--no-color")
    assert client.timeout_sec == 120.0
    assert client.max_retries == 2


def test_llm_client_generate_runs_copilot_prompt_mode(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    client = LLMClient.from_environment()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="generated markdown\n", stderr="")

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    response = client.generate("README を更新する", metadata={"workspace_path": "/workspace/10"})

    assert response == "generated markdown"
    assert captured["command"][0] == "copilot"
    assert "README を更新する" in captured["command"]
    assert "-p" in captured["command"]
    assert "--silent" in captured["command"]
    assert "--allow-all-tools" in captured["command"]
    assert "--no-ask-user" in captured["command"]
    assert "--add-dir" in captured["command"]
    add_dir_index = captured["command"].index("--add-dir")
    assert captured["command"][add_dir_index + 1] == "/workspace/10"
    assert captured["kwargs"]["timeout"] == 120.0
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["capture_output"] is True


def test_llm_client_generate_omits_add_dir_without_workspace_path(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    client = LLMClient.from_environment()
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    response = client.generate("README を更新する")

    assert response == "ok"
    assert "--allow-all-tools" in captured["command"]
    assert "--no-ask-user" in captured["command"]
    assert "--add-dir" not in captured["command"]


def test_llm_client_generate_maps_missing_command_to_configuration_error(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    client = LLMClient.from_environment()

    def fake_run(*_args, **_kwargs):
        raise FileNotFoundError("copilot")

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    with pytest.raises(LLMConfigurationError, match="copilot command is not installed"):
        client.generate("README を更新する")


def test_llm_client_generate_maps_authentication_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    client = LLMClient.from_environment()

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="401 unauthorized")

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    with pytest.raises(LLMAuthenticationError, match="authentication failed"):
        client.generate("README を更新する")


def test_llm_client_generate_maps_rate_limit_and_retries(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    monkeypatch.setenv("LLM_MAX_RETRIES", "1")
    client = LLMClient.from_environment()
    attempts = {"count": 0}

    def fake_run(command, **_kwargs):
        attempts["count"] += 1
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="rate limit exceeded")

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    with pytest.raises(LLMRateLimitError, match="rate limit"):
        client.generate("README を更新する")

    assert attempts["count"] == 2


def test_llm_client_generate_maps_timeout(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    client = LLMClient.from_environment()

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["copilot"], timeout=120)

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    with pytest.raises(LLMTimeoutError, match="timed out"):
        client.generate("README を更新する")


def test_llm_client_generate_rejects_empty_output(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    client = LLMClient.from_environment()

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="  \n", stderr="")

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    with pytest.raises(LLMEmptyResponseError, match="empty"):
        client.generate("README を更新する")


def test_llm_client_generate_maps_unknown_cli_failure(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-123")
    client = LLMClient.from_environment()

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 42, stdout="", stderr="segmentation fault")

    monkeypatch.setattr("backend.llm_client.subprocess.run", fake_run)

    with pytest.raises(LLMServiceError, match="exited with code 42"):
        client.generate("README を更新する")