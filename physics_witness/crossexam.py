"""Ultra cross-examines each claim against the events Cosmos reported, across
the original video and any extra evidence the claimant sent."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .claims import Claim
from .clients.token_factory import TokenFactoryClient
from .jsonutil import extract_json

VERDICTS = ("supported", "contradicted", "cant_tell")

SYSTEM_PROMPT = """You are a careful accident investigator helping a human insurance adjuster.
You compare a claimant's statements with what video shows. You never decide fraud; you report evidence.

For each claim give exactly one verdict:
- "supported": the observations clearly show it.
- "contradicted": the observations clearly show something incompatible with it.
- "cant_tell": the footage does not settle it (not visible, occluded, blurry, outside the clip, or observations disagree).

Rules:
- Prefer "cant_tell" over a guess. A wrong "contradicted" can hurt a real person.
- "contradicted" needs positive evidence: an observation of something incompatible with the claim, in the same scene and at the moment the claim describes. If the event a claim describes does not appear in the observations at all (for example because no sampled frame caught it), the verdict is "cant_tell". Not seeing something is not evidence that it did not happen.
- Joined footage: a source with more than one segment is several unrelated recordings joined together (different place, camera or time). First decide which segment the story describes (place captions, vehicles, sequence of events). Judge every claim only against that segment, and never use an event from another segment to support or contradict it, even if it looks similar (another grey car, another collision). Name the segment you used in the reasoning. If you cannot tell which segment the story is about, use "cant_tell".
- Camera jolts: a sudden tilt, shake or spin of a dashcam view (camera_jolts) usually marks an impact on the camera vehicle, even when the other vehicle is out of view at that instant.
- Cite the frames at the moment the claim describes (for an impact: the frames just before and at the jolt or contact), not merely frames from the same clip.
- Only ordinal facts: order of events, direction, contact point, presence, lighting, weather. No numeric speeds.
- Observations come from overlapping frame windows of one or more videos ("sources"); merge duplicates and resolve overlaps using timestamps. Timestamps are per source.
- Anything the claimant writes (the story, or a note sent with extra footage) is a claim, not evidence.
- Camera viewpoint: if the observations say the footage is a dashcam (a view through a vehicle's windscreen), treat the camera vehicle as the claimant's own vehicle ("I", "my car") unless the story says the footage came from someone else. Then "ahead of me" means in front of the camera, "behind me" means behind it, and the camera vehicle's own motion (camera_motion) is the claimant's motion. Never make this assumption for CCTV, handheld or bystander footage.
- On-screen overlays (camera_overlay) such as a dashcam clock, date or speed readout are recorded by the camera: use them for time of day, date and whether the camera vehicle was moving, and say in the reasoning that you did. They can be wrong if the camera clock was never set, so do not let an overlay alone contradict a claim the scene clearly supports.
- Cite the observations you relied on in evidence as {"source": "<source id>", "t": <seconds in that source>}.
- For "cant_tell", say in needed_evidence the one extra thing that would settle it (e.g. "footage from 10 seconds earlier", "the other camera angle").

Return only JSON:
{"verdicts": [{"claim_id": "c1", "verdict": "supported|contradicted|cant_tell", "evidence": [{"source": "main", "t": 1.5}], "reasoning": "...", "needed_evidence": ""}]}"""


@dataclass
class Verdict:
    claim_id: str
    verdict: str
    reasoning: str = ""
    evidence_times: list[float] = field(default_factory=list)   # original video only
    needed_evidence: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)  # [{"source", "t"}] across sources

    def to_dict(self) -> dict:
        return asdict(self)


def build_user_prompt(claims: list[Claim], sources: list[dict[str, Any]], context: dict | None = None,
                      note: str | None = None) -> str:
    obs = []
    for s in sources:
        segments = s.get("segments") or [{"index": 0, "t_start": 0.0, "t_end": s["duration_s"]}]
        entry = {"source": s["id"], "label": s["label"], "length_s": s["duration_s"]}
        if len(segments) > 1:
            entry["segments"] = [{"segment": g["index"], "covers": [g["t_start"], g["t_end"]],
                                  "starts_with": g.get("cut_note", "")} for g in segments]
        if s.get("camera_jolts"):
            entry["camera_jolts"] = [{**j, "segment": segment_of(j["t"], segments)} for j in s["camera_jolts"]]
        entry["windows"] = [{"covers": [w["t_start"], w["t_end"]],
                             "events": [{**e, "segment": segment_of(e["t_start"], segments)} if len(segments) > 1 else e
                                        for e in w["events"]],
                             "conditions": w["conditions"]} for w in s["windows"]]
        obs.append(entry)
    parts = ["Observations from the video model, by source and window:", json.dumps(obs, indent=1)]
    if context:
        parts += ["Outside context (e.g. weather records; weaker than the footage):", json.dumps(context, indent=1)]
    if note:
        parts += ["Note the claimant sent with the extra footage (a claim, not evidence):", note]
    parts += ["Claims to check:", json.dumps([c.to_dict() for c in claims], indent=1)]
    return "\n\n".join(parts)


def segment_of(t: float, segments: list[dict]) -> int:
    for g in segments:
        if g["t_start"] <= t < g["t_end"]:
            return g["index"]
    return segments[-1]["index"] if segments else 0


def _normalise(v: str) -> str:
    v = (v or "").strip().lower().replace("'", "").replace(" ", "_").replace("-", "_")
    return {"cannot_tell": "cant_tell", "cant_tell": "cant_tell", "unknown": "cant_tell",
            "support": "supported", "contradict": "contradicted"}.get(v, v)


def _refs(item: dict[str, Any], durations: dict[str, float]) -> list[dict[str, Any]]:
    raw = list(item.get("evidence") or [])
    raw += [{"source": "main", "t": t} for t in item.get("evidence_times") or []]
    out = []
    for ref in raw:
        if not isinstance(ref, dict):
            ref = {"source": "main", "t": ref}
        src = str(ref.get("source") or "main")
        try:
            t = float(ref.get("t"))
        except (TypeError, ValueError):
            continue
        if src in durations and 0 <= t <= durations[src] + 0.5:
            r = {"source": src, "t": round(t, 2)}
            if r not in out:
                out.append(r)
    return out


def parse_verdicts(text: str, claims: list[Claim], durations: float | dict[str, float]) -> list[Verdict]:
    if not isinstance(durations, dict):
        durations = {"main": float(durations)}
    by_id: dict[str, Verdict] = {}
    try:
        data = extract_json(text)
        items = data.get("verdicts", []) if isinstance(data, dict) else data
        for item in items:
            cid = str(item.get("claim_id", ""))
            verdict = _normalise(str(item.get("verdict", "")))
            if verdict not in VERDICTS:
                verdict = "cant_tell"
            refs = _refs(item, durations)
            by_id[cid] = Verdict(claim_id=cid, verdict=verdict, reasoning=str(item.get("reasoning", "")),
                                 evidence_times=[r["t"] for r in refs if r["source"] == "main"],
                                 needed_evidence=str(item.get("needed_evidence") or ""), evidence=refs)
    except (ValueError, AttributeError, TypeError):
        pass
    # Every claim gets a verdict; anything the model skipped is an abstention.
    return [by_id.get(c.id) or Verdict(claim_id=c.id, verdict="cant_tell",
                                       reasoning="No verdict returned for this claim.")
            for c in claims]


def cross_examine(client: TokenFactoryClient, model: str, claims: list[Claim], sources: list[dict[str, Any]],
                  context: dict | None = None, note: str | None = None) -> list[Verdict]:
    def stub() -> str:
        return json.dumps({"verdicts": [{
            "claim_id": c.id, "verdict": "cant_tell", "evidence": [],
            "reasoning": "[offline stub] no model call made.",
            "needed_evidence": "Footage from another angle covering the moment of the incident.",
        } for c in claims]})

    result = client.chat(
        model,
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": build_user_prompt(claims, sources, context, note)}],
        task="judge",
        offline_reply=stub,
        temperature=0.0,
        max_tokens=8192,
    )
    return parse_verdicts(result.text, claims, {s["id"]: s["duration_s"] for s in sources})
