"""Outside facts about the claim from Tavily: weather on the day and what the
place is like. Ultra treats these as weaker evidence than the footage."""

from __future__ import annotations

from typing import Any

from .clients.tavily import TavilyClient

MAX_SOURCES = 3
SNIPPET_CHARS = 300


def _summarise(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "query": data.get("query", ""),
        "answer": data.get("answer"),
        "sources": [
            {"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("content") or "")[:SNIPPET_CHARS]}
            for r in (data.get("results") or [])[:MAX_SOURCES]
        ],
    }


def gather(tavily: TavilyClient, place: str | None, date: str | None) -> dict[str, Any] | None:
    """Weather needs place and date; place facts need only the place."""
    place, date = (place or "").strip(), (date or "").strip()
    if not place:
        return None
    if tavily.offline:
        return {"offline": True, "place": place, "date": date or None}
    out: dict[str, Any] = {"place": place, "date": date or None}
    if date:
        out["weather"] = _summarise(tavily.weather(place, date))
    out["place_facts"] = _summarise(tavily.search(
        f"{place} road layout, traffic signals, lanes and speed limit", max_results=MAX_SOURCES))
    return out


def for_prompt(context: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop URLs and bookkeeping before the context goes into the Ultra prompt."""
    if not context or context.get("offline"):
        return None
    slim: dict[str, Any] = {"place": context.get("place"), "date": context.get("date")}
    for key in ("weather", "place_facts"):
        if block := context.get(key):
            slim[key] = {"answer": block.get("answer"), "snippets": [s["snippet"] for s in block["sources"]]}
    return slim
