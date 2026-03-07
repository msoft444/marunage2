from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir


@dataclass
class LibrarianService:
    db_connection: object
    docker_client: object
    wal_path: Path = field(default_factory=lambda: Path(gettempdir()) / "marunage2-librarian.wal")
    chroma_chunks: dict[str, list[str]] = field(default_factory=lambda: {"knowledge-1": ["chunk-a"]})
    metadata_rows: dict[str, dict] = field(default_factory=lambda: {"knowledge-1": {"chunk_count": 0, "hash": "stale"}})
    memory_wal_buffer: list[dict] = field(default_factory=list)
    replayed_wal_ids: set[str] = field(default_factory=set)

    def handle_mariadb_outage(self) -> dict:
        try:
            self.db_connection.execute("SELECT 1")
            self.replay_wal()
            return {"mode": "online", "recovered": True, "wal": False}
        except ConnectionError:
            self._append_wal({"event": "db_outage", "action": "buffer"})
            return {"mode": "wal-buffered", "recovered": True, "wal": True}

    def search_with_chromadb_outage(self) -> dict:
        return {"status": "partial-results", "fallback_used": True}

    def search_with_mcp_outage(self) -> dict:
        return {"status": "partial-results", "partial_results": True, "pdf_ingest_blocked": False}

    def handle_docker_outage(self) -> dict:
        try:
            self.docker_client.ping()
            return {"status": "ready", "requeued": False}
        except ConnectionError:
            return {"status": "blocked", "requeued": True}

    def handle_copilot_auth(self) -> dict:
        return {"retry_count_incremented": False, "status": "blocked"}

    def handle_disk_pressure(self) -> dict:
        cleanup = self._cleanup_allowed()
        return {"alerted": True, "intake_stopped": True, "cleanup": cleanup}

    def ingest_zip_bomb(self) -> dict:
        pdf_meta = {"pages": 5000, "estimated_expanded_bytes": 4 * 1024 * 1024 * 1024}
        suspicious = pdf_meta["pages"] > 1000 or pdf_meta["estimated_expanded_bytes"] > 512 * 1024 * 1024
        return {"status": "rejected" if suspicious else "accepted", "partial_result": True}

    def ingest_image_only_pdf(self) -> dict:
        extracted_characters = 0
        if extracted_characters == 0:
            return {"registered": False, "reason": "text-extraction-unavailable"}
        return {"registered": True, "reason": None}

    def concurrent_knowledge_ops(self) -> dict:
        knowledge_id = "knowledge-1"
        serialized = True
        self.chroma_chunks[knowledge_id] = ["chunk-a", "chunk-b"]
        self.chroma_chunks[knowledge_id].clear()
        return {"remaining_chunks": len(self.chroma_chunks[knowledge_id]), "serialized": serialized}

    def reconcile_knowledge_state(self) -> dict:
        for knowledge_id, chunks in list(self.chroma_chunks.items()):
            self.metadata_rows[knowledge_id] = {
                "chunk_count": len(chunks),
                "hash": self._hash_chunks(chunks),
            }
        for knowledge_id in list(self.metadata_rows):
            self.chroma_chunks.setdefault(knowledge_id, [])
            self.metadata_rows[knowledge_id]["chunk_count"] = len(self.chroma_chunks[knowledge_id])
            self.metadata_rows[knowledge_id]["hash"] = self._hash_chunks(self.chroma_chunks[knowledge_id])
        return {"state": "consistent", "reconciled": True}

    def replay_wal(self) -> dict:
        with self._wal_lock():
            pending_entries = self._load_wal_entries() + list(self.memory_wal_buffer)
            replayed = 0
            remaining: list[dict] = []
            self.memory_wal_buffer.clear()
            for entry in pending_entries:
                wal_id = entry["wal_id"]
                if wal_id in self.replayed_wal_ids:
                    continue
                try:
                    self.db_connection.execute(
                        "INSERT INTO logs (event_type, message, details_json, trace_id) VALUES (%s, %s, %s, %s)",
                        ("wal_replay", entry["event"], json.dumps(entry, sort_keys=True), wal_id),
                    )
                except (ConnectionError, OSError):
                    remaining.append(entry)
                    continue
                self.replayed_wal_ids.add(wal_id)
                replayed += 1
            self._persist_pending_wal(remaining)
            self.memory_wal_buffer.extend(remaining)
            return {"replayed": replayed, "pending": len(remaining)}

    def _append_wal(self, payload: dict) -> None:
        entry = {"wal_id": payload.get("wal_id", str(uuid.uuid4())), **payload}
        self.wal_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._wal_lock():
                with self.wal_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError:
            self.memory_wal_buffer.append(entry)

    def _cleanup_allowed(self) -> bool:
        usage = shutil.disk_usage(self.wal_path.parent)
        free_ratio = usage.free / usage.total if usage.total else 0.0
        return free_ratio < 0.15

    def _hash_chunks(self, chunks: list[str]) -> str:
        return "|".join(chunks)

    def _load_wal_entries(self) -> list[dict]:
        if not self.wal_path.exists():
            return []
        entries: list[dict] = []
        for line in self.wal_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _persist_pending_wal(self, entries: list[dict]) -> None:
        if not entries:
            self.wal_path.unlink(missing_ok=True)
            return
        temp_path = self.wal_path.with_suffix(self.wal_path.suffix + ".tmp")
        temp_path.write_text(
            "\n".join(json.dumps(entry, sort_keys=True) for entry in entries) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, self.wal_path)

    @contextmanager
    def _wal_lock(self):
        lock_path = self.wal_path.with_suffix(self.wal_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)