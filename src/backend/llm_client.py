from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass


class LLMError(RuntimeError):
    pass


class LLMConfigurationError(LLMError):
    pass


class LLMAuthenticationError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMServiceError(LLMError):
    pass


class LLMEmptyResponseError(LLMError):
    pass


@dataclass
class LLMClient:
    command: tuple[str, ...]
    timeout_sec: float = 120.0
    max_retries: int = 1
    model: str | None = None

    @classmethod
    def from_environment(cls) -> "LLMClient":
        github_token = os.getenv("GITHUB_TOKEN", "").strip()
        if not github_token:
            raise LLMConfigurationError("GITHUB_TOKEN is not configured")

        timeout_sec = float(os.getenv("LLM_TIMEOUT_SEC", "120"))
        max_retries = int(os.getenv("LLM_MAX_RETRIES", "1"))
        model = os.getenv("COPILOT_MODEL", "").strip() or None
        command_text = os.getenv("COPILOT_CLI_COMMAND", "copilot").strip() or "copilot"
        command = cls._parse_command(command_text)
        return cls(command=command, timeout_sec=timeout_sec, max_retries=max_retries, model=model)

    @staticmethod
    def _parse_command(command_text: str) -> tuple[str, ...]:
        command = tuple(shlex.split(command_text))
        if not command:
            raise LLMConfigurationError("COPILOT_CLI_COMMAND is empty")
        return command

    def generate(self, prompt: str, metadata: dict | None = None) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMConfigurationError("prompt must not be empty")

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._generate_with_copilot(prompt.strip(), metadata or {})
            except LLMRateLimitError as error_instance:
                last_error = error_instance
                if attempt >= self.max_retries:
                    raise

        if last_error is not None:
            raise last_error
        raise LLMServiceError("LLM request did not complete")

    def _generate_with_copilot(self, prompt: str, metadata: dict) -> str:
        command = list(self.command)
        working_directory = metadata.get("workspace_path") if isinstance(metadata.get("workspace_path"), str) else None

        if self.model:
            command.extend(["--model", self.model])
        command.extend(["--allow-all-tools", "--no-ask-user"])
        if working_directory:
            command.extend(["--add-dir", working_directory])
        command.extend(["-p", prompt, "--silent"])

        env = os.environ.copy()
        env.setdefault("COPILOT_CLI", "1")

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                env=env,
                cwd=working_directory or None,
            )
        except FileNotFoundError as error_instance:
            raise LLMConfigurationError("copilot command is not installed") from error_instance
        except subprocess.TimeoutExpired as error_instance:
            raise LLMTimeoutError("copilot prompt timed out") from error_instance
        except OSError as error_instance:
            raise LLMServiceError(f"copilot command failed: {error_instance}") from error_instance

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()

        if completed.returncode != 0:
            self._raise_for_cli_failure(completed.returncode, stderr)
        if not stdout:
            raise LLMEmptyResponseError("copilot response was empty")
        return stdout

    @staticmethod
    def _raise_for_cli_failure(returncode: int, stderr: str) -> None:
        message = stderr.strip() or "no error output"
        lowered = message.lower()

        if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
            raise LLMAuthenticationError(f"copilot authentication failed: {message}")
        if "rate limit" in lowered or "quota" in lowered or "premium request" in lowered:
            raise LLMRateLimitError(f"copilot rate limit exceeded: {message}")
        if "not installed" in lowered or "command not found" in lowered:
            raise LLMConfigurationError("copilot command is not installed")
        raise LLMServiceError(f"copilot command exited with code {returncode}: {message}")
