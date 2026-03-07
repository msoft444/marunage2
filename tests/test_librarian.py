import json
import multiprocessing
from collections import namedtuple
from pathlib import Path
import shutil

from librarian import LibrarianService


def _append_wal_entries_in_child(wal_path: str, process_index: int, entry_count: int) -> None:
    librarian = LibrarianService(db_connection=object(), docker_client=object(), wal_path=Path(wal_path))
    for entry_index in range(entry_count):
        librarian._append_wal(
            {
                "wal_id": f"proc-{process_index}-entry-{entry_index}",
                "event": "db_outage",
                "action": "buffer",
            }
        )


def test_EX_01_mariadb_outage_uses_wal(librarian, db_connection_mock):
    db_connection_mock.execute.side_effect = ConnectionError("db down")
    result = librarian.handle_mariadb_outage()
    assert result["mode"] == "wal-buffered" and result["recovered"] is True and result["wal"] is True
    assert librarian.wal_path.exists() or librarian.memory_wal_buffer


def test_EX_02_chromadb_outage_falls_back(librarian):
    result = librarian.search_with_chromadb_outage()
    assert result["status"] == "partial-results" and result["fallback_used"] is True


def test_EX_03_mcp_outage_isolated_from_pdf_ingest(librarian):
    result = librarian.search_with_mcp_outage()
    assert result["status"] == "partial-results" and result["partial_results"] is True and result["pdf_ingest_blocked"] is False


def test_EX_04_docker_sock_outage_is_blocked_not_failed(librarian, docker_client_mock):
    docker_client_mock.ping.side_effect = ConnectionError("docker unavailable")
    result = librarian.handle_docker_outage()
    assert result["status"] == "blocked" and result["requeued"] is True


def test_EX_05_copilot_auth_expiry_does_not_consume_retry_budget(librarian):
    result = librarian.handle_copilot_auth()
    assert result["retry_count_incremented"] is False and result["status"] == "blocked"


def test_EX_06_disk_pressure_stops_new_intake(librarian):
    disk_usage_result = namedtuple("usage", ["total", "used", "free"])
    original_disk_usage = shutil.disk_usage
    shutil.disk_usage = lambda _path: disk_usage_result(total=100, used=95, free=5)
    try:
        result = librarian.handle_disk_pressure()
    finally:
        shutil.disk_usage = original_disk_usage
    assert result["alerted"] is True and result["intake_stopped"] is True and result["cleanup"] is True


def test_LB_01_zip_bomb_is_rejected_before_container_death(librarian):
    result = librarian.ingest_zip_bomb()
    assert result["status"] == "rejected" and result["partial_result"] is True


def test_LB_02_image_only_pdf_is_rejected(librarian):
    result = librarian.ingest_image_only_pdf()
    assert result["registered"] is False and result["reason"] == "text-extraction-unavailable"


def test_LB_03_register_delete_is_serialized(librarian):
    result = librarian.concurrent_knowledge_ops()
    assert result["remaining_chunks"] == 0 and result["serialized"] is True


def test_LB_04_chromadb_and_metadata_are_reconciled(librarian):
    result = librarian.reconcile_knowledge_state()
    assert result["state"] == "consistent" and result["reconciled"] is True


def test_LB_05_wal_replay_is_idempotent(librarian):
    librarian._append_wal({"wal_id": "event-1", "event": "db_outage", "action": "buffer"})
    first = librarian.replay_wal()
    second = librarian.replay_wal()
    assert first["replayed"] == 1 and second["replayed"] == 0


def test_LB_06_wal_falls_back_to_memory_on_oserror(librarian, monkeypatch):
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))
    librarian._append_wal({"event": "db_outage", "action": "buffer"})
    assert len(librarian.memory_wal_buffer) == 1


def test_LB_07_wal_append_is_safe_across_processes(tmp_path):
    wal_path = tmp_path / "concurrent-librarian.wal"
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(target=_append_wal_entries_in_child, args=(str(wal_path), process_index, 25))
        for process_index in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    entries = [json.loads(line) for line in wal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(entries) == 100
    assert len({entry["wal_id"] for entry in entries}) == 100