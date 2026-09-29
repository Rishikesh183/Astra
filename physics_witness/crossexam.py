"""Ultra cross-examines each claim against the events Cosmos reported."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .claims import Claim
from .clients.token_factory import TokenFactoryClient
from .events import WindowReading
from .jsonutil import extract_json

VERDICTS = ("supported", "contradicted", "cant_tell")

SYSTEM_PROMPT = """You are a careful accident investigator helping a human insurance adjuster.
You compare a claimant's statements with what a video shows. You never decide fraud; you report evidence.

For each claim give exactly one verdict:
- "supported": the observations clearly show it.
- "contradicted": the observations clearly show something incompatible with it.
- "cant_tell": the footage does not settle it (not visible, occluded, blurry, outside the clip, or observations disagree).

Rules:
- Prefer "cant_tell" over a guess. A wrong "contradicted" can hurt a real person.
- Only ordinal facts: order of events, direction, contact point, presence, lighting, weather. No numeric speeds.
- Observations come from separate overlapping frame windows; merge duplicates and resolve overlaps using timestamps.
- Cite the timestamps (seconds) of the observations you relied on in evidence_times.
- For "cant_tell", say in needed_evidence the one extra thing that would settle it (e.g. "footage from 10 seconds earlier", "the other camera angle").

Return only JSON:
{"verdicts": [{"claim_id": "c1", "verdict": "supported|contradicted|cant_tell", "evidence_times": [1.5], "reasoning": "...", "needed_evidence": ""}]}"""


@dataclass
class Verdict:
    claim_id: str
    verdict: str
    reasoning: str = ""
    evidence_times: list[float] = field(default_factory=list)
    needed_evidence: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def build_user_prompt(claims: list[Claim], readings: list[WindowReading], duration: float,
                      context: dict | None = None) -> str:
    obs = [{
        "window": r.window, "covers": [r.t_start, r.t_end],
        "events": [e.to_dict() for e in r.events], "conditions": r.conditions,
    } for r in readings]
    parts = [
        f"Clip length: {duration:.2f} s.",
        "Observations from the video model, by window:",
        json.dumps(obs, indent=1),
    ]
    if context:
        parts += ["Outside context (e.g. weather records; weaker than the footage):", json.dumps(context, indent=1)]
    parts += ["Claims to check:", json.dumps([c.to_dict() for c in claims], indent=1)]
    return "\n\n".join(parts)


def _normalise(v: str) -> str:
    v = (v or "").strip().lower().replace("'", "").replace(" ", "_").replace("-", "_")
    return {"cannot_tell": "cant_tell", "cant_tell": "cant_tell", "unknown": "cant_tell",
            "support": "supported", "contradict": "contradicted"}.get(v, v)


def parse_verdicts(text: str, claims: list[Claim], duration: float) -> list[Verdict]:
    by_id: dict[str, Verdict] = {}
    try:
        data = extract_json(text)
        items = data.get("verdicts", []) if isinstance(data, dict) else data
        for item in items:
            cid = str(item.get("claim_id", ""))
            verdict = _normalise(str(item.get("verdict", "")))
            if verdict not in VERDICTS:
                verdict = "cant_tell"
            times = []
            for t in item.get("evidence_times") or []:
                try:
                    t = float(t)
                except (TypeError, ValueError):
                    continue
                if 0 <= t <= duration + 0.5:
                    times.append(round(t, 2))
            by_id[cid] = Verdict(claim_id=cid, verdict=verdict, reasoning=str(item.get("reasoning", "")),
                                 evidence_times=times, needed_evidence=str(item.get("needed_evidence") or ""))
    except (ValueError, AttributeError, TypeError):
        pass
    # Every claim gets a verdict; anything the model skipped is an abstention.
    return [by_id.get(c.id) or Verdict(claim_id=c.id, verdict="cant_tell",
                                       reasoning="No verdict returned for this claim.")
            for c in claims]


def cross_examine(client: TokenFactoryClient, model: str, claims: list[Claim],
                  readings: list[WindowReading], duration: float, context: dict | None = None) -> list[Verdict]:
    def stub() -> str:
        return json.dumps({"verdicts": [{
            "claim_id": c.id, "verdict": "cant_tell", "evidence_times": [],
            "reasoning": "[offline stub] no model call made.", "needed_evidence": "",
        } for c in claims]})

    result = client.chat(
        model,
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": build_user_prompt(claims, readings, duration, context)}],
        offline_reply=stub,
        temperature=0.0,
        max_tokens=8192,
    )
    return parse_verdicts(result.text, claims, duration)
