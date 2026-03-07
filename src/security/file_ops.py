from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir


@dataclass
class SafeFileOps:
    workspace_root: Path = field(default_factory=lambda: Path(gettempdir()) / "marunage2-fileops")

    def write_with_manifest(self) -> dict:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        target = self.workspace_root / "generated.txt"
        manifest = self.workspace_root / "generated.txt.manifest.json"
        manifest.write_text(
            json.dumps({"target": str(target), "state": "prepared"}, sort_keys=True),
            encoding="utf-8",
        )
        return {"manifest_written": manifest.exists(), "write_started": target.exists()}

    def concurrent_edit(self) -> dict:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.workspace_root / "shared.txt.lock"
        first_handle = self._acquire_lock(lock_path)
        blocked = False
        try:
            try:
                second_handle = self._acquire_lock(lock_path)
            except FileExistsError:
                blocked = True
            else:
                second_handle.close()
        finally:
            first_handle.close()
            lock_path.unlink(missing_ok=True)
        return {"lost_updates": not blocked, "blocked": blocked}

    def regenerate_markers(self) -> dict:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        target = self.workspace_root / "template.txt"
        target.write_text(
            """
before
<!-- FOREIGN:BEGIN -->
keep me
<!-- FOREIGN:END -->
<!-- MANAGED:BEGIN -->
old content
<!-- MANAGED:END -->
after
""".strip(),
            encoding="utf-8",
        )
        content = target.read_text(encoding="utf-8")
        updated = content.replace("old content", "new content")
        target.write_text(updated, encoding="utf-8")
        foreign_marker_destroyed = "keep me" not in target.read_text(encoding="utf-8")
        return {"foreign_marker_destroyed": foreign_marker_destroyed}

    def _acquire_lock(self, lock_path: Path):
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        return os.fdopen(fd, "w", encoding="utf-8")