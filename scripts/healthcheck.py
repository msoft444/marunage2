from __future__ import annotations

import os
import sys


def main() -> int:
    required = [
        item.strip()
        for item in os.getenv("REQUIRED_ENV_VARS", "DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD").split(",")
        if item.strip()
    ]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        print(f"missing env vars: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1

    try:
        import backend  # noqa: F401
        import librarian  # noqa: F401
        import security  # noqa: F401
    except Exception as exc:
        print(f"import healthcheck failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())