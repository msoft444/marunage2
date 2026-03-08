from __future__ import annotations

import os
import subprocess
from pathlib import Path
import re
from typing import Any, Callable

from security.sandbox import WorkspaceSandbox


class ArtifactApplyError(RuntimeError):
    pass

import subprocess


class RepositoryPreparationError(RuntimeError):
    pass


class RepositoryWorkspaceManager:
    def __init__(self, git_command_runner: Callable[[list[str], Path], None] | None = None):
        self.git_command_runner = git_command_runner or self._run_git_command

    def apply_artifact(
        self,
        workspace_path: str,
        working_branch: str,
        task_title: str | None = None,
        result_summary_md: str | None = None,
    ) -> dict[str, Any]:
        workspace_root = Path(workspace_path)
        repo_path = workspace_root / "repo"
        artifacts_path = workspace_root / "artifacts"
        patches_path = workspace_root / "patches"
        artifact_path = artifacts_path / "llm_response.md"
        patch_path = patches_path / "artifact_apply.patch"

        if not artifact_path.exists():
            raise ArtifactApplyError("artifact_not_found")
        if not repo_path.is_dir():
            raise ArtifactApplyError("repository_not_found")

        artifact_body = artifact_path.read_text(encoding="utf-8")
        artifact_size = len(artifact_body.encode("utf-8"))
        max_artifact_bytes = int(os.getenv("ARTIFACT_MAX_BYTES", "131072"))
        if artifact_size > max_artifact_bytes:
            raise ArtifactApplyError(f"artifact_too_large: {artifact_size} > {max_artifact_bytes}")

        diff_text = self._extract_unified_diff(artifact_body)
        changed_files = self._validate_diff(diff_text, repo_path)

        patches_path.mkdir(parents=True, exist_ok=True)
        patch_path.write_text(diff_text, encoding="utf-8")

        try:
            self._invoke_git(["git", "apply", "--check", str(patch_path)], repo_path)
            self._invoke_git(["git", "apply", str(patch_path)], repo_path)
        except Exception as error:
            raise ArtifactApplyError(f"patch_apply_error: {self._stringify_git_error(error)}") from error

        if not self._changed_files(repo_path):
            raise ArtifactApplyError("artifact_apply_no_changes")

        commit_message = self._build_commit_message(task_title, result_summary_md)
        try:
            self._invoke_git(["git", "config", "user.name", "Maru-nage Bot"], repo_path)
            self._invoke_git(["git", "config", "user.email", "marunage@example.invalid"], repo_path)
            self._invoke_git(["git", "add", "--", *changed_files], repo_path)
            self._invoke_git(["git", "commit", "-m", commit_message], repo_path)
        except Exception as error:
            raise ArtifactApplyError(f"git_commit_failed: {self._stringify_git_error(error)}") from error

        try:
            commit_sha = self._invoke_git_capture(["git", "rev-parse", "HEAD"], repo_path).strip()
            self._invoke_git(["git", "push", "origin", working_branch], repo_path)
        except Exception as error:
            raise ArtifactApplyError(f"git_push_failed: {self._stringify_git_error(error)}") from error

        return {
            "artifact_path": str(artifact_path),
            "patch_path": str(patch_path),
            "changed_files": changed_files,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "working_branch": working_branch,
        }

    def prepare_repository(
        self,
        workspace_path: str,
        target_repo: str,
        target_ref: str,
        working_branch: str,
    ) -> dict[str, str]:
        workspace_root = Path(workspace_path)
        repo_path = workspace_root / "repo"
        artifacts_path = workspace_root / "artifacts"
        docs_snapshot_path = workspace_root / "system_docs_snapshot"
        patches_path = workspace_root / "patches"

        try:
            workspace_root.mkdir(parents=True, exist_ok=True)
            artifacts_path.mkdir(parents=True, exist_ok=True)
            docs_snapshot_path.mkdir(parents=True, exist_ok=True)
            patches_path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise RepositoryPreparationError(f"workspace directory setup failed: {error}") from error

        clone_url = f"https://github.com/{target_repo}.git"
        if not repo_path.exists():
            self._invoke_git(["git", "clone", clone_url, str(repo_path)], workspace_root)

        self._invoke_git(["git", "checkout", target_ref], repo_path)
        self._invoke_git(["git", "checkout", "-B", working_branch], repo_path)
        return {
            "workspace_path": str(workspace_root),
            "repo_path": str(repo_path),
            "artifacts_path": str(artifacts_path),
            "docs_snapshot_path": str(docs_snapshot_path),
            "patches_path": str(patches_path),
        }

    def _invoke_git(self, args: list[str], cwd: Path) -> None:
        try:
            self.git_command_runner(args, cwd)
        except Exception as error:
            raise RepositoryPreparationError(str(error)) from error

    def _invoke_git_capture(self, args: list[str], cwd: Path) -> str:
        result = self.git_command_runner(args, cwd)
        if isinstance(result, str):
            return result
        stdout = getattr(result, "stdout", "")
        if isinstance(stdout, bytes):
            return stdout.decode("utf-8", errors="replace")
        return str(stdout or "")

    @staticmethod
    def _extract_unified_diff(artifact_body: str) -> str:
        lines = artifact_body.splitlines()
        start_index: int | None = None
        end_index = len(lines)
        for index, line in enumerate(lines):
            if line.startswith("--- a/"):
                start_index = index
                break
        if start_index is None:
            raise ArtifactApplyError("no_diff_section")
        for index in range(start_index + 1, len(lines)):
            if lines[index].startswith("```"):
                end_index = index
                break
        diff_text = "\n".join(lines[start_index:end_index]).strip()
        if not diff_text:
            raise ArtifactApplyError("no_diff_section")
        return f"{diff_text}\n"

    @staticmethod
    def _validate_diff(diff_text: str, repo_path: Path) -> list[str]:
        sandbox = WorkspaceSandbox(str(repo_path))
        repo_root = repo_path.resolve(strict=False)
        lines = diff_text.splitlines()
        changed_files: list[str] = []
        has_hunk = False
        index = 0
        unsupported_pattern = re.compile(r"^(rename from|rename to|deleted file mode|new file mode|Binary files|GIT binary patch)")

        while index < len(lines):
            line = lines[index]
            if unsupported_pattern.match(line):
                raise ArtifactApplyError("unsupported_diff_operation")
            if line.startswith("@@"):
                has_hunk = True
            if line.startswith("--- "):
                if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                    raise ArtifactApplyError("invalid_diff_format")
                old_path = lines[index][4:].strip()
                new_path = lines[index + 1][4:].strip()
                validated = RepositoryWorkspaceManager._validate_diff_target(old_path, new_path, repo_root, sandbox)
                changed_files.append(validated)
                index += 2
                continue
            index += 1

        if not changed_files or not has_hunk:
            raise ArtifactApplyError("invalid_diff_format")
        return changed_files

    @staticmethod
    def _validate_diff_target(old_path: str, new_path: str, repo_root: Path, sandbox: WorkspaceSandbox) -> str:
        if old_path == "/dev/null" or new_path == "/dev/null":
            raise ArtifactApplyError("unsupported_diff_operation")
        if not old_path.startswith("a/") or not new_path.startswith("b/"):
            raise ArtifactApplyError("invalid_diff_format")
        relative_old = old_path[2:].strip()
        relative_new = new_path[2:].strip()
        if relative_old != relative_new:
            raise ArtifactApplyError("unsupported_diff_operation")
        if not relative_new:
            raise ArtifactApplyError("empty_diff_path")
        if Path(relative_new).is_absolute():
            raise ArtifactApplyError("absolute_diff_path")
        if not sandbox.validate_relative_path(relative_new):
            raise ArtifactApplyError("path_traversal")
        if relative_new == ".git" or relative_new.startswith(".git/"):
            raise ArtifactApplyError("git_metadata_write_forbidden")

        target_path = repo_root / relative_new
        resolved_target = target_path.resolve(strict=False)
        if not resolved_target.is_relative_to(repo_root):
            raise ArtifactApplyError("path_traversal")
        if not target_path.exists():
            raise ArtifactApplyError("patch_apply_error: target file missing")
        if target_path.is_symlink():
            symlink_target = target_path.resolve(strict=True)
            if not symlink_target.is_relative_to(repo_root):
                raise ArtifactApplyError("symlink_escape")
        if target_path.is_dir():
            raise ArtifactApplyError("patch_apply_error: target is directory")
        return relative_new

    def _changed_files(self, repo_path: Path) -> list[str]:
        status_output = self._invoke_git_capture(["git", "status", "--short"], repo_path)
        changed_files: list[str] = []
        for line in status_output.splitlines():
            if not line.strip():
                continue
            changed_files.append(line[3:].strip())
        return changed_files

    @staticmethod
    def _build_commit_message(task_title: str | None, result_summary_md: str | None) -> str:
        raw_message = task_title or result_summary_md or "Apply generated artifact"
        sanitized = RepositoryWorkspaceManager._mask_secrets(raw_message)
        sanitized = " ".join(sanitized.replace("\r", " ").replace("\n", " ").split())
        sanitized = sanitized[:120].strip()
        return sanitized or "Apply generated artifact"

    @staticmethod
    def _mask_secrets(value: str) -> str:
        masked = value
        for env_var in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
            secret = os.getenv(env_var, "").strip()
            if secret:
                masked = masked.replace(secret, "[MASKED_GITHUB_TOKEN]")
        return masked

    @staticmethod
    def _stringify_git_error(error: Exception) -> str:
        stderr = getattr(error, "stderr", None)
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if stderr:
            return str(stderr).strip()
        stdout = getattr(error, "stdout", None)
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if stdout:
            return str(stdout).strip()
        return str(error)

    @staticmethod
    def _run_git_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)