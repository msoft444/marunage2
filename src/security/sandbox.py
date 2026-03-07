from __future__ import annotations

from pathlib import Path


class WorkspaceSandbox:
    def __init__(self, workspace_root: str = "/workspace"):
        self.workspace_root = Path(workspace_root).resolve(strict=False)
        self.allowed_mount_roots = (self.workspace_root,)
        self.banned_flags = {"--privileged", "--pid=host", "--network=host", "--ipc=host", "--cap-add=ALL"}

    def resolve_symlink(self, link_path: str) -> str:
        resolved = Path(link_path).resolve(strict=True)
        if not self._is_within_allowed_root(resolved):
            raise ValueError("symlink target escapes workspace")
        return str(resolved)

    def validate_relative_path(self, relative_path: str) -> bool:
        if ".." in Path(relative_path).parts:
            return False
        return True

    def validate_mount_source(self, source_path: str) -> bool:
        resolved = Path(source_path).resolve(strict=False)
        return self._is_within_allowed_root(resolved)

    def validate_docker_flags(self, flags: list[str]) -> bool:
        return not any(flag in self.banned_flags for flag in flags)

    def validate_workspace_path(self, workspace_path: str) -> bool:
        path = Path(workspace_path)
        try:
            normalized = path.resolve(strict=False)
        except RuntimeError:
            return False
        return self._is_within_allowed_root(normalized)

    def validate_submodule_path(self, submodule_path: str) -> bool:
        if not self.validate_relative_path(submodule_path):
            return False
        return self.validate_workspace_path(str((self.workspace_root / submodule_path).resolve(strict=False)))

    def validate_control_chars(self, workspace_path: str) -> bool:
        return not any(ord(char) < 32 or ord(char) == 127 for char in workspace_path)

    def _is_within_allowed_root(self, candidate: Path) -> bool:
        for root in self.allowed_mount_roots:
            if candidate.is_relative_to(root):
                return True
        return False