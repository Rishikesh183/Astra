"""Disk cache for model responses. Every paid call goes through here so
re-runs (and recorded demos) cost nothing."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable


class ResponseCache:
    """Two levels: local JSON files (fast, survive restarts on one machine), then
    an optional shared store (`remote`, e.g. Supabase's model_cache table) that
    every server instance and fresh container can read."""

    def __init__(self, root: Path, enabled: bool = True, remote=None):
        self.remote = remote
        self.root = Path(root)
        self.enabled = enabled

    def _path(self, namespace: str, key: str) -> Path:
        return self.root / namespace / f"{key}.json"

    @staticmethod
    def key_for(payload: Any) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()

    def get_or_call(self, namespace: str, payload: Any, call: Callable[[], Any],
                    keep: Callable[[Any], bool] | None = None) -> tuple[Any, bool]:
        """Return (value, cache_hit). Values `keep` rejects are returned but not
        stored, so a bad reply (e.g. an empty one) is retried next time instead
        of being replayed from the cache forever."""
        if not self.enabled:
            return call(), False
        path = self._path(namespace, self.key_for(payload))
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), True
        key = path.stem
        if self.remote is not None:
            try:
                shared = self.remote.cache_get(namespace, key)
            except Exception:              # the shared cache is an optimisation, never a failure
                shared = None
            if shared is not None:
                self._write_local(path, shared)
                return shared, True
        value = call()
        if keep is not None and not keep(value):
            return value, False
        if self.remote is not None:
            try:
                self.remote.cache_put(namespace, key, value)
            except Exception:
                pass
        self._write_local(path, value)
        return value, False

    @staticmethod
    def _write_local(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so parallel runs (Liar Test workers) never read a half-written file.
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def make_cache(settings, enabled: bool = True) -> ResponseCache:
    """The cache for these settings: local files, plus Supabase's model_cache
    table when PW_STORE=supabase."""
    remote = None
    if enabled and (settings.store or "files") == "supabase" and settings.supabase_url and settings.supabase_key:
        from .backends import SupabaseBackend
        remote = SupabaseBackend(settings.supabase_url, settings.supabase_key, settings.supabase_bucket)
    return ResponseCache(settings.cache_dir, enabled=enabled, remote=remote)
