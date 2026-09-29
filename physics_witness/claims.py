"""Split a written story into short, individually checkable claims (Nano/Super)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from .clients.token_factory import TokenFactoryClient
from .jsonutil import extract_json

KINDS = ("order", "direction", "contact", "position", "weather", "lighting", "presence", "other")

SPLIT_PROMPT = """You split an insurance claimant's written account of an incident into atomic claims that a video could confirm or refute.

Rules:
- One physical assertion per claim (who moved, in what order, which direction, where contact happened, weather, lighting, who was present).
- Keep the claimant's wording where possible. Do not add facts.
- Drop opinions and feelings that no footage could check, but keep anything physical.
- kind is one of: {kinds}.
- "source" is the exact sentence of the story the claim came from.

Return only JSON: {{"claims": [{{"id": "c1", "text": "...", "kind": "...", "source": "..."}}]}}

Story:
\"\"\"{story}\"\"\""""


@dataclass
class Claim:
    id: str
    text: str
    kind: str = "other"
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def split_sentences(story: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", story.strip())
    return [p.strip() for p in parts if p.strip()]


def heuristic_claims(story: str) -> list[Claim]:
    return [Claim(id=f"c{i}", text=s, source=s) for i, s in enumerate(split_sentences(story), 1)]


def split_claims(client: TokenFactoryClient, model: str, story: str) -> list[Claim]:
    fallback = heuristic_claims(story)

    def stub() -> str:
        return json.dumps({"claims": [c.to_dict() for c in fallback]})

    result = client.chat(
        model,
        [{"role": "user", "content": SPLIT_PROMPT.format(kinds=", ".join(KINDS), story=story)}],
        task="split",
        offline_reply=stub,
        temperature=0.0,
        max_tokens=2048,
    )
    try:
        raw = extract_json(result.text)
        items = raw.get("claims", []) if isinstance(raw, dict) else raw
        claims = []
        for i, item in enumerate(items, 1):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            kind = item.get("kind", "other")
            claims.append(Claim(id=f"c{i}", text=text, kind=kind if kind in KINDS else "other",
                                source=str(item.get("source", "")).strip()))
        return claims or fallback
    except (ValueError, AttributeError, TypeError):
        return fallback
