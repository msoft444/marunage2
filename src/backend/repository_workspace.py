from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from security.sandbox import WorkspaceSandbox


class ArtifactApplyError(RuntimeError):
    pass


class CommitPushError(RuntimeError):
    pass


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
        raise ArtifactApplyError("artifact_apply_deprecated")

    def commit_and_push(
        self,
        workspace_path: str,
        working_branch: str,
        task_title: str | None = None,
        result_summary_md: str | None = None,
    ) -> dict[str, Any]:
        workspace_root = Path(workspace_path)
        repo_path = workspace_root / "repo"

        if not repo_path.is_dir():
            raise CommitPushError("repository_not_found")
        if not isinstance(working_branch, str) or not working_branch.strip():
            raise CommitPushError("working_branch_missing")

        changed_files = self.validate_changed_files(repo_path)
        if not changed_files:
            raise CommitPushError("phase_edit_no_changes")

        self._scan_changed_files_for_secrets(repo_path, changed_files)
        commit_message = self._build_commit_message(task_title, result_summary_md, default_message="Apply direct-edit changes")

        try:
            self._invoke_git(["git", "config", "user.name", "Maru-nage Bot"], repo_path)
            self._invoke_git(["git", "config", "user.email", "marunage@example.invalid"], repo_path)
            self._invoke_git(["git", "add", "--all", "--", *changed_files], repo_path)
            self._invoke_git(["git", "commit", "-m", commit_message], repo_path)
        except Exception as error:
            raise CommitPushError(f"git_commit_failed: {self._stringify_git_error(error)}") from error

        try:
            commit_sha = self._invoke_git_capture(["git", "rev-parse", "HEAD"], repo_path).strip()
            remote_url = self._invoke_git_capture(["git", "remote", "get-url", "origin"], repo_path).strip()
            self._invoke_git(self._with_github_auth(["git", "push", "origin", working_branch], remote_url), repo_path)
        except Exception as error:
            raise CommitPushError(f"git_push_failed: {self._stringify_git_error(error)}") from error

        return {
            "changed_files": changed_files,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "working_branch": working_branch,
        }

    def validate_changed_files(self, repo_path: Path) -> list[str]:
        sandbox = WorkspaceSandbox(str(repo_path))
        repo_root = repo_path.resolve(strict=False)
        changed_files = self._changed_files(repo_path)
        if not changed_files:
            return []

        max_changed_files = int(os.getenv("MAX_CHANGED_FILES", "100"))
        unique_files: list[str] = []
        seen: set[str] = set()
        for relative_path in changed_files:
            normalized_path = self._validate_changed_file_path(relative_path, repo_root, sandbox)
            if normalized_path not in seen:
                unique_files.append(normalized_path)
                seen.add(normalized_path)

        if len(unique_files) > max_changed_files:
            raise CommitPushError(f"too_many_changed_files: {len(unique_files)} > {max_changed_files}")
        return unique_files

    def list_merge_targets(self, workspace_path: str, working_branch: str) -> list[str]:
        repo_path = self._repo_path(workspace_path)
        self._require_working_branch(working_branch)
        try:
            self._invoke_git_with_origin_auth(["git", "fetch", "origin", "--prune"], repo_path)
            self._resolve_working_branch_ref(repo_path, working_branch)
            output = self._invoke_git_capture(["git", "branch", "-r", "--format=%(refname:short)"], repo_path)
        except Exception as error:
            if isinstance(error, CommitPushError):
                raise
            raise CommitPushError(f"merge_target_list_failed: {self._stringify_git_error(error)}") from error

        allowed_targets = self._allowed_merge_targets()
        merge_targets: list[str] = []
        for line in output.splitlines():
            candidate = line.strip()
            if not candidate or "->" in candidate or not candidate.startswith("origin/"):
                continue
            branch_name = candidate.removeprefix("origin/")
            if branch_name in allowed_targets and branch_name not in merge_targets:
                merge_targets.append(branch_name)
        return merge_targets

    def list_repository_branches(self, repository_url: str) -> list[str]:
        try:
            output = self._invoke_git_capture(
                self._with_github_auth(["git", "ls-remote", "--heads", repository_url], repository_url),
                Path.cwd(),
            )
        except Exception as error:
            raise CommitPushError(f"branch_list_failed: {self._stringify_git_error(error)}") from error

        allowed_targets = self._allowed_merge_targets()
        branches: list[str] = []
        for line in output.splitlines():
            if "\trefs/heads/" not in line:
                continue
            branch_name = line.split("\trefs/heads/", 1)[1].strip()
            if branch_name in allowed_targets and branch_name not in branches:
                branches.append(branch_name)
        return branches

    def get_diff(self, workspace_path: str, working_branch: str, merge_target: str = "main") -> str:
        repo_path = self._repo_path(workspace_path)
        self._require_working_branch(working_branch)
        merge_target = self._validate_merge_target(merge_target)
        self._assert_remote_branch_exists(repo_path, merge_target)
        diff_source = self._resolve_working_branch_ref(repo_path, working_branch)
        try:
            return self._invoke_git_capture(
                ["git", "diff", "--no-ext-diff", f"origin/{merge_target}...{diff_source}"],
                repo_path,
            )
        except Exception as error:
            raise CommitPushError(f"diff_generation_failed: {self._stringify_git_error(error)}") from error

    def merge_and_cleanup(self, workspace_path: str, working_branch: str, merge_target: str = "main") -> dict[str, Any]:
        repo_path = self._repo_path(workspace_path)
        self._require_working_branch(working_branch)
        merge_target = self._validate_merge_target(merge_target)
        self._assert_remote_branch_exists(repo_path, merge_target)
        self._ensure_local_working_branch(repo_path, working_branch)

        try:
            self._invoke_git_with_origin_auth(["git", "fetch", "origin", merge_target], repo_path)
            if self._local_branch_exists(repo_path, merge_target):
                self._invoke_git(["git", "checkout", merge_target], repo_path)
                self._invoke_git(["git", "merge", "--ff-only", f"origin/{merge_target}"], repo_path)
            else:
                self._invoke_git(["git", "checkout", "-b", merge_target, f"origin/{merge_target}"], repo_path)
            self._invoke_git(
                ["git", "merge", "--no-ff", working_branch, "-m", f"Merge {working_branch} into {merge_target}"],
                repo_path,
            )
        except Exception as error:
            message = self._stringify_git_error(error)
            if "CONFLICT" in message or "Automatic merge failed" in message:
                raise CommitPushError(f"merge_conflict: {message}") from error
            raise CommitPushError(f"merge_failed: {message}") from error

        try:
            remote_url = self._invoke_git_capture(["git", "remote", "get-url", "origin"], repo_path).strip()
            self._invoke_git(self._with_github_auth(["git", "push", "origin", merge_target], remote_url), repo_path)
        except Exception as error:
            raise CommitPushError(f"merge_push_rejected: {self._stringify_git_error(error)}") from error

        deleted_local_branch = False
        try:
            if self._local_branch_exists(repo_path, working_branch):
                self._invoke_git(["git", "branch", "-d", working_branch], repo_path)
                deleted_local_branch = True
        except Exception as error:
            raise CommitPushError(f"branch_cleanup_local_failed: {self._stringify_git_error(error)}") from error

        deleted_remote_branch = False
        try:
            remote_url = self._invoke_git_capture(["git", "remote", "get-url", "origin"], repo_path).strip()
            self._invoke_git(self._with_github_auth(["git", "push", "origin", "--delete", working_branch], remote_url), repo_path)
            deleted_remote_branch = True
        except Exception as error:
            raise CommitPushError(f"branch_cleanup_remote_failed: {self._stringify_git_error(error)}") from error

        return {
            "merge_target": merge_target,
            "working_branch": working_branch,
            "deleted_local_branch": deleted_local_branch,
            "deleted_remote_branch": deleted_remote_branch,
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
        try:
            if not repo_path.exists():
                self._invoke_git(self._with_github_auth(["git", "clone", clone_url, str(repo_path)], clone_url), workspace_root)
            self._invoke_git_with_origin_auth(["git", "fetch", "origin", "--prune"], repo_path, fallback_remote_url=clone_url)

            if self._remote_branch_exists(repo_path, working_branch):
                self._invoke_git(["git", "checkout", "-B", working_branch, f"origin/{working_branch}"], repo_path)
            else:
                self._invoke_git(["git", "checkout", target_ref], repo_path)
                self._invoke_git(["git", "checkout", "-B", working_branch, target_ref], repo_path)
        except Exception as error:
            raise RepositoryPreparationError(self._stringify_git_error(error)) from error
        return {
            "workspace_path": str(workspace_root),
            "repo_path": str(repo_path),
            "artifacts_path": str(artifacts_path),
            "docs_snapshot_path": str(docs_snapshot_path),
            "patches_path": str(patches_path),
        }

    def _invoke_git(self, args: list[str], cwd: Path) -> None:
        self.git_command_runner(args, cwd)

    def _invoke_git_capture(self, args: list[str], cwd: Path) -> str:
        result = self.git_command_runner(args, cwd)
        if isinstance(result, str):
            return result
        stdout = getattr(result, "stdout", "")
        if isinstance(stdout, bytes):
            return stdout.decode("utf-8", errors="replace")
        return str(stdout or "")

    def _origin_remote_url(self, repo_path: Path, fallback_remote_url: str | None = None) -> str:
        try:
            remote_url = self._invoke_git_capture(["git", "remote", "get-url", "origin"], repo_path).strip()
        except Exception:
            if fallback_remote_url is None:
                raise
            return fallback_remote_url
        return remote_url or (fallback_remote_url or "")

    def _invoke_git_with_origin_auth(self, args: list[str], cwd: Path, fallback_remote_url: str | None = None) -> None:
        remote_url = self._origin_remote_url(cwd, fallback_remote_url=fallback_remote_url)
        self._invoke_git(self._with_github_auth(args, remote_url), cwd)

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
            remote_url = self._invoke_git_capture(["git", "remote", "get-url", "origin"], repo_path).strip()
            self._invoke_git(self._with_github_auth(["git", "push", "origin", working_branch], remote_url), repo_path)
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
    def _allowed_merge_targets() -> set[str]:
        configured = os.getenv("MERGE_TARGET_ALLOWLIST", "main,develop")
        return {item.strip() for item in configured.split(",") if item.strip()}

    @classmethod
    def allowed_merge_targets(cls) -> set[str]:
        return cls._allowed_merge_targets()

    def _validate_merge_target(self, merge_target: str) -> str:
        normalized = (merge_target or "main").strip()
        if not normalized:
            normalized = "main"
        if not re.fullmatch(r"[a-zA-Z0-9._/-]+", normalized):
            raise CommitPushError("merge_target_not_allowed")
        if normalized not in self._allowed_merge_targets():
            raise CommitPushError("merge_target_not_allowed")
        return normalized

    def validate_merge_target(self, merge_target: str) -> str:
        return self._validate_merge_target(merge_target)

    @staticmethod
    def _repo_path(workspace_path: str) -> Path:
        repo_path = Path(workspace_path) / "repo"
        if not repo_path.is_dir():
            raise CommitPushError("repository_not_found")
        return repo_path

    @staticmethod
    def _require_working_branch(working_branch: str) -> None:
        if not isinstance(working_branch, str) or not working_branch.strip():
            raise CommitPushError("working_branch_missing")

    def _assert_remote_branch_exists(self, repo_path: Path, merge_target: str) -> None:
        try:
            self._invoke_git_with_origin_auth(["git", "fetch", "origin", merge_target], repo_path)
            self._invoke_git(["git", "rev-parse", "--verify", f"origin/{merge_target}"], repo_path)
        except Exception as error:
            raise CommitPushError(f"merge_target_not_found: {self._stringify_git_error(error)}") from error

    def _resolve_working_branch_ref(self, repo_path: Path, working_branch: str) -> str:
        if self._local_branch_exists(repo_path, working_branch):
            return working_branch
        try:
            self._invoke_git_with_origin_auth(["git", "fetch", "origin", working_branch], repo_path)
            self._invoke_git(["git", "rev-parse", "--verify", f"origin/{working_branch}"], repo_path)
        except Exception as error:
            raise CommitPushError("working_branch_not_found") from error
        return f"origin/{working_branch}"

    def _ensure_local_working_branch(self, repo_path: Path, working_branch: str) -> None:
        working_branch_ref = self._resolve_working_branch_ref(repo_path, working_branch)
        if working_branch_ref == working_branch:
            return
        try:
            self._invoke_git(["git", "checkout", "-B", working_branch, working_branch_ref], repo_path)
        except Exception as error:
            raise CommitPushError(f"working_branch_checkout_failed: {self._stringify_git_error(error)}") from error

    def _local_branch_exists(self, repo_path: Path, branch_name: str) -> bool:
        try:
            self._invoke_git(["git", "rev-parse", "--verify", branch_name], repo_path)
        except Exception:
            return False
        return True

    def _remote_branch_exists(self, repo_path: Path, branch_name: str) -> bool:
        try:
            self._invoke_git(["git", "rev-parse", "--verify", f"origin/{branch_name}"], repo_path)
        except Exception:
            return False
        return True

    @staticmethod
    def _build_commit_message(task_title: str | None, result_summary_md: str | None, default_message: str = "Apply generated artifact") -> str:
        raw_message = task_title or result_summary_md or default_message
        sanitized = RepositoryWorkspaceManager._mask_secrets(raw_message)
        sanitized = " ".join(sanitized.replace("\r", " ").replace("\n", " ").split())
        sanitized = sanitized[:120].strip()
        return sanitized or default_message

    @staticmethod
    def _with_github_auth(args: list[str], remote_url: str) -> list[str]:
        if not remote_url.startswith("https://github.com/"):
            return args

        token = os.getenv("GITHUB_TOKEN", "").strip()
        if not token:
            return args

        basic_auth = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        return [
            args[0],
            "-c",
            f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic_auth}",
            *args[1:],
        ]

    @staticmethod
    def _validate_changed_file_path(relative_path: str, repo_root: Path, sandbox: WorkspaceSandbox) -> str:
        stripped_path = relative_path.strip()
        if not stripped_path:
            raise CommitPushError("empty_changed_file_path")
        if Path(stripped_path).is_absolute():
            raise CommitPushError("absolute_changed_file_path")
        if not sandbox.validate_relative_path(stripped_path):
            raise CommitPushError("path_traversal")
        if stripped_path == ".git" or stripped_path.startswith(".git/"):
            raise CommitPushError("git_metadata_write_forbidden")

        target_path = repo_root / stripped_path
        resolved_target = target_path.resolve(strict=False)
        if not resolved_target.is_relative_to(repo_root):
            raise CommitPushError("path_traversal")
        if target_path.is_symlink():
            try:
                symlink_target = target_path.resolve(strict=True)
            except FileNotFoundError as error:
                raise CommitPushError("symlink_escape") from error
            if not symlink_target.is_relative_to(repo_root):
                raise CommitPushError("symlink_escape")
        return stripped_path

    def _scan_changed_files_for_secrets(self, repo_path: Path, changed_files: list[str]) -> None:
        for relative_path in changed_files:
            target_path = repo_path / relative_path
            if not target_path.exists() or target_path.is_dir():
                continue
            file_bytes = target_path.read_bytes()
            for env_var in ("GITHUB_TOKEN", "GH_TOKEN", "COPILOT_GITHUB_TOKEN"):
                secret = os.getenv(env_var, "").strip()
                if secret and secret.encode("utf-8") in file_bytes:
                    raise CommitPushError("secret_in_changed_files")

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
        timeout_seconds = int(os.getenv("GIT_COMMAND_TIMEOUT_SECONDS", "60"))
        return subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True, timeout=timeout_seconds)