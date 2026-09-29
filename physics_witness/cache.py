"""Disk cache for model responses. Every paid call goes through here so
re-runs (and recorded demos) cost nothing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable


class ResponseCache:
    def __init__(self, root: Path, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled

    def _path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / f"{key}.json"

    @staticmethod
    def key_for(payload: Any) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def get_or_call(self, namespace: str, payload: Any, call: Callable[[], Any]) -> tuple[Any, bool]:
        """Return (value, cache_hit)."""
        if not self.enabled:
            return call(), False
        path = self._path(namespace, self.key_for(payload))
        if path.is_file():
            return json.loads(path.read_text()), True
        value = call()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))
        return value, False
