from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backend import WorkerEngine
from backend.database import LeaseConflictError
from security import SecureDashboard


LOGGER = logging.getLogger("marunage2.service")


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def load_file_backed_secrets() -> None:
    for env_name, value in list(os.environ.items()):
        if not env_name.endswith("_FILE") or not value:
            continue
        target_name = env_name[:-5]
        secret_path = Path(value)
        if not secret_path.exists():
            raise RuntimeError(f"secret file for {target_name} not found: {secret_path}")
        os.environ[target_name] = secret_path.read_text(encoding="utf-8").strip()


def validate_runtime_env(extra_required: list[str] | None = None) -> None:
    base_required = [
        item.strip()
        for item in os.getenv(
            "REQUIRED_ENV_VARS",
            "DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD",
        ).split(",")
        if item.strip()
    ]
    required = base_required + list(extra_required or [])
    missing = sorted({name for name in required if not os.getenv(name)})
    if missing:
        raise RuntimeError(f"missing required environment variables: {', '.join(missing)}")


def ping_database() -> None:
    import mariadb

    connection = mariadb.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        autocommit=True,
    )
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.close()
    finally:
        connection.close()


def open_database_connection():
    import mariadb

    return mariadb.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        autocommit=False,
    )


class DashboardHandler(BaseHTTPRequestHandler):
    dashboard = SecureDashboard()

    def do_GET(self) -> None:  # noqa: N802
        response = self.dashboard.serve_path(self.path)
        body = response["body"].encode("utf-8")
        self.send_response(response["status"])
        self.send_header("Content-Type", response["content_type"])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        LOGGER.info("dashboard request: " + format, *args)


def run_dashboard() -> int:
    validate_runtime_env()
    ping_database()
    port = int(os.getenv("DASHBOARD_PORT", "18080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    LOGGER.info("dashboard listening on port %s", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def run_worker(service_name: str, extra_required: list[str] | None = None) -> int:
    validate_runtime_env(extra_required)
    connection = open_database_connection()
    interval = int(os.getenv("SERVICE_LOOP_INTERVAL_SEC", "30"))
    stop_event = threading.Event()
    worker_name = f"{service_name}-{os.getpid()}"
    engine = WorkerEngine(
        connection,
        service_name=service_name,
        worker_name=worker_name,
        connection_factory=open_database_connection,
    )

    def _stop(_signum, _frame) -> None:
        LOGGER.info("received shutdown signal for %s", service_name)
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    LOGGER.info("%s service started", service_name)
    try:
        while not stop_event.is_set():
            try:
                engine.ensure_connection()
                engine.recover_expired_tasks()
                processed = engine.run_once()
                LOGGER.info("%s heartbeat ok", service_name)
                if processed:
                    LOGGER.info("%s processed one queued task", service_name)
            except LeaseConflictError:
                LOGGER.info("%s Task already leased by another worker", service_name)
            except Exception:
                LOGGER.exception("%s worker cycle failed", service_name)
            if stop_event.wait(interval):
                break
    finally:
        engine.connection.close()
    LOGGER.info("%s service stopped", service_name)
    return 0


def main(argv: list[str]) -> int:
    configure_logging()
    load_file_backed_secrets()

    if len(argv) != 2:
        print("usage: python scripts/service_runner.py <brain|librarian|dashboard|guardian>", file=sys.stderr)
        return 2

    service_name = argv[1]
    if service_name == "dashboard":
        return run_dashboard()
    if service_name == "brain":
        return run_worker("brain", ["TARGET_REPO", "TARGET_REF", "COPILOT_CONFIG_DIR", "GITHUB_TOKEN"])
    if service_name == "guardian":
        return run_worker("guardian", ["COPILOT_CONFIG_DIR", "GITHUB_TOKEN"])
    if service_name == "librarian":
        return run_worker("librarian")

    print(f"unknown service: {service_name}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))