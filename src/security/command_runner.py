from __future__ import annotations

import re


class SecureCommandRunner:
    _SAFE_NAME = re.compile(r"^[A-Za-z0-9._/-]+$")
    _BANNED_TOKENS = (";", "&&", "||", "`", "$(", ">", "<")

    def launch_copilot(self, cli_profile: str) -> dict:
        blocked = any(token in cli_profile for token in self._BANNED_TOKENS) or not re.fullmatch(r"[A-Za-z0-9_-]+", cli_profile)
        return {"shell": False, "blocked": blocked, "cli_profile": cli_profile}

    def build_docker_command(self, image_name: str) -> list[str]:
        safe_image = image_name if self._SAFE_NAME.fullmatch(image_name) else "invalid-image"
        return ["docker", "run", safe_image]