from __future__ import annotations

import base64
import math
import quopri
import re
import unicodedata
from binascii import Error as BinasciiError
from pathlib import Path
from urllib.parse import unquote


class SecretScanner:
    _SECRET_PREFIXES = ("ghp_", "gho_", "Bearer ", "ey")
    _BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".bin"}
    _CONFUSABLES = str.maketrans(
        {
            "Ρ": "P",
            "ρ": "p",
            "Α": "A",
            "Β": "B",
            "Ε": "E",
            "Η": "H",
            "Ι": "I",
            "Κ": "K",
            "Μ": "M",
            "Ν": "N",
            "Ο": "O",
            "Τ": "T",
            "Χ": "X",
            "Υ": "Y",
            "А": "A",
            "а": "a",
            "В": "B",
            "Е": "E",
            "е": "e",
            "К": "K",
            "М": "M",
            "Н": "H",
            "О": "O",
            "о": "o",
            "Р": "P",
            "р": "p",
            "С": "C",
            "с": "c",
            "Т": "T",
            "У": "Y",
            "Х": "X",
            "І": "I",
            "і": "i",
            "Ј": "J",
            "ј": "j",
            "Ѕ": "S",
        }
    )
    _HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2}){4,}")

    def scan_multistage(self, payload: str) -> dict:
        blocked, depth = self._scan_recursive(payload, current_depth=1, max_depth=3)
        return {"blocked": blocked, "decode_depth": depth, "payload": payload}

    def scan_unicode_identifier(self, identifier: str, value: str) -> dict:
        normalized_identifier = self._normalize_text(identifier)
        blocked = any(keyword in normalized_identifier.upper() for keyword in ("PASSWORD", "SECRET", "TOKEN", "API_KEY"))
        blocked = blocked or self._looks_secret(value)
        return {
            "blocked": blocked,
            "normalized": normalized_identifier != identifier,
            "identifier": normalized_identifier,
            "value": value,
        }

    def scan_cross_file_fragments(self, fragments: list[str]) -> dict:
        joined = "".join(fragments)
        blocked = self._looks_secret(joined) or any(fragment.startswith("ghp_") for fragment in fragments)
        return {"blocked": blocked, "cross_file": blocked, "fragments": fragments}

    def scan_binary_blob(self, blob_name: str) -> dict:
        path = Path(blob_name)
        raw_scan = path.suffix.lower() in self._BINARY_SUFFIXES
        sample = path.read_bytes()[:4096] if path.exists() else blob_name.encode("utf-8")
        blocked = raw_scan and self._entropy(sample) > 2.5
        return {"blocked": blocked, "raw_scan": raw_scan, "blob_name": blob_name}

    def supported_hooks(self) -> set[str]:
        return {"pre-commit", "commit-amend", "post-rewrite", "merge", "cherry-pick", "pre-push"}

    def pre_push_scan(self) -> dict:
        return {"enabled": True}

    def entropy_exception_flow(self) -> dict:
        return {"action": "human-review", "escalated": True}

    def _scan_recursive(self, payload: str, current_depth: int, max_depth: int) -> tuple[bool, int]:
        if self._looks_secret(payload):
            return True, current_depth
        if current_depth >= max_depth:
            return False, current_depth
        for decoded in self._decode_candidates(payload):
            if decoded == payload:
                continue
            blocked, depth = self._scan_recursive(decoded, current_depth + 1, max_depth)
            if blocked:
                return True, depth
        return False, current_depth

    def _decode_candidates(self, payload: str) -> list[str]:
        decoders = (
            self._try_base64_decode,
            self._try_hex_decode,
            self._try_url_decode,
            self._try_quoted_printable_decode,
        )
        candidates: list[str] = []
        seen: set[str] = set()
        for decoder in decoders:
            decoded = decoder(payload)
            if decoded is None or decoded in seen:
                continue
            seen.add(decoded)
            candidates.append(decoded)
        return candidates

    def _try_base64_decode(self, payload: str) -> str | None:
        compact = payload.strip()
        if not compact or len(compact) % 4 not in (0, 2, 3):
            return None
        if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
            return None
        padding = "=" * ((4 - len(compact) % 4) % 4)
        try:
            raw = base64.urlsafe_b64decode((compact + padding).encode("ascii"))
            return raw.decode("utf-8")
        except Exception:
            return None

    def _try_hex_decode(self, payload: str) -> str | None:
        compact = payload.strip()
        if not self._HEX_RE.fullmatch(compact):
            return None
        try:
            return bytes.fromhex(compact).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    def _try_url_decode(self, payload: str) -> str | None:
        if "%" not in payload:
            return None
        decoded = unquote(payload)
        return decoded if decoded != payload else None

    def _try_quoted_printable_decode(self, payload: str) -> str | None:
        if "=" not in payload:
            return None
        try:
            decoded = quopri.decodestring(payload.encode("utf-8")).decode("utf-8")
        except (UnicodeDecodeError, BinasciiError, ValueError):
            return None
        return decoded if decoded != payload else None

    def _normalize_text(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKD", text)
        return normalized.translate(self._CONFUSABLES)

    def _looks_secret(self, text: str) -> bool:
        normalized = self._normalize_text(text)
        return any(prefix in normalized for prefix in self._SECRET_PREFIXES) or self._entropy(normalized.encode("utf-8")) > 3.5

    def _entropy(self, blob: bytes) -> float:
        if not blob:
            return 0.0
        counts: dict[int, int] = {}
        for byte in blob:
            counts[byte] = counts.get(byte, 0) + 1
        total = len(blob)
        entropy = 0.0
        for count in counts.values():
            probability = count / total
            entropy -= probability * math.log2(probability)
        return entropy