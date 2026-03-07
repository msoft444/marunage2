from __future__ import annotations

import json
import posixpath
from pathlib import Path
from urllib.parse import urlparse

import nh3


class SecureDashboard:
    _ALLOWED_TAGS = {"a", "p", "br", "code", "pre", "strong", "em", "ul", "ol", "li"}
    _ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}
    _TEXT_CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }

    def __init__(self, asset_root: Path | None = None):
        self.asset_root = asset_root or Path(__file__).resolve().parent / "static"

    def render_markdown(self, content_md: str) -> str:
        return nh3.clean(
            content_md,
            tags=self._ALLOWED_TAGS,
            attributes=self._ALLOWED_ATTRIBUTES,
            url_schemes={"http", "https", "mailto"},
        )

    def fetch_timeline(self, message_count: int) -> dict:
        return {"initial_load": min(message_count, 100), "paginated": True}

    def approve_release(self) -> dict:
        return {"promote_release_tasks": 1}

    def serve_path(self, request_path: str) -> dict:
        parsed = urlparse(request_path or "/")
        path = posixpath.normpath(parsed.path or "/")

        # Static UI must win over the generic API handler for `/` and `/index.html`.
        if path in {"/", "/index.html"}:
            return self._serve_asset("index.html")

        if path.startswith("/static/"):
            relative_path = path.removeprefix("/static/")
            return self._serve_asset(relative_path)

        if path.startswith("/api/v1/"):
            payload = {
                "service": "dashboard",
                "status": "ok",
                "path": path,
            }
            return {
                "status": 200,
                "content_type": "application/json",
                "body": json.dumps(payload),
                "json": payload,
            }

        return self._json_response(404, {"error": "not_found", "path": path})

    def _serve_asset(self, relative_path: str) -> dict:
        if "\x00" in relative_path:
            return self._json_response(404, {"error": "asset_not_found", "path": relative_path})
        asset_path = (self.asset_root / relative_path).resolve()
        asset_root = self.asset_root.resolve()
        if not asset_path.is_file() or not asset_path.is_relative_to(asset_root):
            return self._json_response(404, {"error": "asset_not_found", "path": relative_path})

        content_type = self._TEXT_CONTENT_TYPES.get(asset_path.suffix, "application/octet-stream")
        body = asset_path.read_text(encoding="utf-8")
        return {
            "status": 200,
            "content_type": content_type,
            "body": body,
        }

    def _json_response(self, status: int, payload: dict) -> dict:
        return {
            "status": status,
            "content_type": "application/json",
            "body": json.dumps(payload),
            "json": payload,
        }