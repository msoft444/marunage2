from __future__ import annotations

import nh3


class SecureDashboard:
    _ALLOWED_TAGS = {"a", "p", "br", "code", "pre", "strong", "em", "ul", "ol", "li"}
    _ALLOWED_ATTRIBUTES = {"a": {"href", "title"}}

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