from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable


class RepositoryPreparationError(RuntimeError):
    pass


class RepositoryWorkspaceManager:
    def __init__(self, git_command_runner: Callable[[list[str], Path], None] | None = None):
        self.git_command_runner = git_command_runner or self._run_git_command

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

    @staticmethod
    def _run_git_command(args: list[str], cwd: Path) -> None:
        subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)