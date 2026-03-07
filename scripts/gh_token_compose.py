from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence


DEFAULT_COMPOSE_COMMAND = [
    "docker",
    "compose",
    "-f",
    "docker-compose.prod.yml",
    "--env-file",
    ".env.runtime",
]


def resolve_github_token(run_command=subprocess.run) -> str:
    try:
        result = run_command(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gh is not installed. Run `gh auth login` after installing GitHub CLI.") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise RuntimeError(f"gh auth token failed{detail}")

    token = (result.stdout or "").strip()
    if not token:
        raise RuntimeError("empty GITHUB_TOKEN returned by gh auth token")
    return token


def build_compose_environment(base_env: Mapping[str, str], token: str) -> dict[str, str]:
    environment = dict(base_env)
    environment["GITHUB_TOKEN"] = token
    return environment


def compose_command(args: Sequence[str]) -> list[str]:
    return [*DEFAULT_COMPOSE_COMMAND, *args]


def main(argv: Sequence[str] | None = None, run_command=subprocess.run) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        args = ["up", "--build"]

    try:
        token = resolve_github_token(run_command=run_command)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    environment = build_compose_environment(os.environ.copy(), token)
    result = run_command(compose_command(args), env=environment, check=False)
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())