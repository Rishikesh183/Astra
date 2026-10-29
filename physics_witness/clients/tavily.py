"""Tavily search client, used for weather and place facts about a claim."""

from __future__ import annotations

from typing import Any

import httpx

from ..cache import ResponseCache, make_cache
from ..config import Settings

TAVILY_URL = "https://api.tavily.com/search"
# Bad key, rate limit or plan/credit limit (432, 433): worth retrying with the fallback key.
KEY_PROBLEMS = {401, 403, 429, 432, 433}


class TavilyClient:
    def __init__(self, settings: Settings, cache: ResponseCache | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.cache = cache or make_cache(settings)
        self._http = httpx.Client(timeout=30.0, transport=transport)

    @property
    def offline(self) -> bool:
        return self.settings.offline or not self._keys()

    def _keys(self) -> list[str]:
        s = self.settings
        return [k for k in (s.tavily_api_key, s.tavily_fallback_key) if k]

    def search(self, query: str, *, max_results: int = 5, include_answer: bool = True) -> dict[str, Any]:
        if self.offline:
            return {"query": query, "answer": None, "results": [], "offline": True}
        payload = {"query": query, "max_results": max_results, "include_answer": include_answer}

        def call() -> dict[str, Any]:
            keys = self._keys()
            for i, key in enumerate(keys):
                resp = self._http.post(TAVILY_URL, json=payload, headers={"Authorization": f"Bearer {key}"})
                if resp.status_code in KEY_PROBLEMS and i < len(keys) - 1:
                    continue
                resp.raise_for_status()
                return resp.json()
            raise AssertionError("unreachable")

        data, _ = self.cache.get_or_call("tavily", payload, call)
        return data

    def weather(self, place: str, date: str) -> dict[str, Any]:
        return self.search(f"historical weather in {place} on {date}: rain, visibility, daylight")
