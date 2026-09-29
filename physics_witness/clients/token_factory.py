"""Nebius Token Factory client (OpenAI-compatible chat completions API)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..cache import ResponseCache
from ..config import Settings


class TokenFactoryError(RuntimeError):
    pass


@dataclass
class ChatResult:
    text: str
    model: str
    usage: dict[str, int]
    cached: bool = False


TOKEN_KEYS = ("prompt_tokens", "completion_tokens")


@dataclass
class UsageLedger:
    """Token counts per (task, model). Cached responses cost nothing; their
    tokens are counted as saved. Listeners hear about every call, which is
    what drives the live cost meter."""

    calls: int = 0
    cache_hits: int = 0
    _rows: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    listeners: list[Callable[["UsageLedger"], None]] = field(default_factory=list)

    def record(self, result: ChatResult, task: str = "other") -> None:
        self.calls += 1
        self.cache_hits += int(result.cached)
        row = self._rows.setdefault((task, result.model), {
            "calls": 0, "cached_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "saved_prompt_tokens": 0, "saved_completion_tokens": 0})
        row["calls"] += 1
        prefix = "saved_" if result.cached else ""
        row["cached_calls"] += int(result.cached)
        for key in TOKEN_KEYS:
            row[prefix + key] += int(result.usage.get(key, 0) or 0)
        for listener in self.listeners:
            listener(self)

    def rows(self) -> list[dict[str, Any]]:
        return [{"task": t, "model": m, **v} for (t, m), v in self._rows.items()]

    def to_dict(self) -> dict[str, Any]:
        by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for (_, model), v in self._rows.items():
            for key in TOKEN_KEYS:
                by_model[model][key] += v[key]
            by_model[model]["total_tokens"] += v["prompt_tokens"] + v["completion_tokens"]
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "by_model": {m: dict(v) for m, v in by_model.items()},
            "rows": self.rows(),
        }


def image_part(b64_jpeg: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_jpeg}"}}


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


class TokenFactoryClient:
    def __init__(self, settings: Settings, cache: ResponseCache | None = None,
                 ledger: UsageLedger | None = None, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.cache = cache or ResponseCache(settings.cache_dir)
        self.ledger = ledger or UsageLedger()
        self._http = httpx.Client(
            base_url=settings.nebius_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.nebius_api_key}"},
            timeout=settings.request_timeout,
            transport=transport,
        )

    @property
    def offline(self) -> bool:
        return self.settings.offline or not self.settings.nebius_api_key

    def _raise_for(self, resp: httpx.Response) -> None:
        if resp.status_code in (402, 403):
            raise TokenFactoryError(
                f"Token Factory returned {resp.status_code}: project credit may be exhausted "
                f"or the key lacks access. Body: {resp.text[:300]}"
            )
        if resp.is_error:
            raise TokenFactoryError(f"Token Factory {resp.status_code}: {resp.text[:500]}")

    def list_models(self) -> list[str]:
        resp = self._http.get("models")
        self._raise_for(resp)
        return sorted(m["id"] for m in resp.json().get("data", []))

    def chat(self, model: str, messages: list[dict[str, Any]], *, task: str = "other",
             offline_reply: Callable[[], str] | None = None, **params: Any) -> ChatResult:
        """Chat completion with caching. In offline mode, `offline_reply` supplies the text."""
        if self.offline:
            if offline_reply is None:
                raise TokenFactoryError("Offline mode (no NEBIUS_API_KEY or PW_OFFLINE=1) and no stub reply given")
            result = ChatResult(text=offline_reply(), model=model or "offline-stub", usage={})
            self.ledger.record(result, task)
            return result
        if not model:
            raise TokenFactoryError("No model name configured; run `physics-witness checks` to discover one")

        payload = {"model": model, "messages": messages, **params}

        def call() -> dict[str, Any]:
            resp = self._http.post("chat/completions", json=payload)
            self._raise_for(resp)
            return resp.json()

        data, hit = self.cache.get_or_call("chat", payload, call)
        text = data["choices"][0]["message"].get("content") or ""
        result = ChatResult(text=text, model=model, usage=data.get("usage") or {}, cached=hit)
        self.ledger.record(result, task)
        return result
