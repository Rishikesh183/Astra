"""Cosmos reads a window of timestamped frames and reports physical events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .clients.token_factory import TokenFactoryClient, image_part, text_part
from .frames import Frame
from .jsonutil import extract_json

COSMOS_PROMPT = """These {n} frames are in time order from one camera. Each frame has its timestamp printed in the black band at the bottom ({labels}).

Describe what physically happens, as an accident investigator would. Report only what the frames show:
- which road users or objects are present and where they are,
- who moves, in which direction (left/right/toward/away from the camera), and in what order,
- any contact, and where on each object it happens,
- lighting (day/dusk/night) and weather or road surface (dry/wet/rain/snow),
- whether the view is clear, partly blocked, or too blurry to tell.

Use the printed timestamps. Do not guess speeds in numbers. If something cannot be seen, say so instead of guessing.

Return only JSON:
{{"events": [{{"t_start": 0.0, "t_end": 0.0, "what": "...", "actors": ["..."], "visibility": "clear|partial|poor"}}],
  "conditions": {{"lighting": "...", "weather": "...", "road_surface": "...", "view": "clear|partial|poor"}}}}"""


@dataclass
class Event:
    t_start: float
    t_end: float
    what: str
    actors: list[str] = field(default_factory=list)
    visibility: str = "clear"
    window: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WindowReading:
    window: int
    t_start: float
    t_end: float
    frame_times: list[float]
    events: list[Event]
    conditions: dict
    raw: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw")
        return d


def _stub_reply(frames: list[Frame]) -> str:
    return json.dumps({
        "events": [{
            "t_start": frames[0].t, "t_end": frames[-1].t,
            "what": "[offline stub] no model call made; frames sampled but not interpreted",
            "actors": [], "visibility": "poor",
        }],
        "conditions": {"lighting": "unknown", "weather": "unknown", "road_surface": "unknown", "view": "poor"},
    })


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_window(client: TokenFactoryClient, model: str, frames: list[Frame], window: int) -> WindowReading:
    labels = ", ".join(f.label() for f in frames)
    content = [text_part(COSMOS_PROMPT.format(n=len(frames), labels=labels))]
    content += [image_part(f.to_b64_jpeg()) for f in frames]
    result = client.chat(
        model,
        [{"role": "user", "content": content}],
        offline_reply=lambda: _stub_reply(frames),
        temperature=0.2,
        max_tokens=4096,
    )
    lo, hi = frames[0].t, frames[-1].t
    events, conditions = [], {}
    try:
        data = extract_json(result.text)
        if isinstance(data, list):
            data = {"events": data}
        for e in data.get("events", []):
            t0 = min(max(_as_float(e.get("t_start"), lo), lo), hi)
            t1 = min(max(_as_float(e.get("t_end"), t0), t0), hi)
            actors = e.get("actors") or []
            events.append(Event(t_start=t0, t_end=t1, what=str(e.get("what", "")).strip(),
                                actors=[str(a) for a in actors] if isinstance(actors, list) else [str(actors)],
                                visibility=str(e.get("visibility", "clear")), window=window))
        conditions = data.get("conditions") or {}
    except (ValueError, AttributeError, TypeError):
        # Keep the free-text reading so Ultra still sees it.
        events.append(Event(t_start=lo, t_end=hi, what=result.text.strip()[:2000], visibility="partial", window=window))
    return WindowReading(window=window, t_start=lo, t_end=hi, frame_times=[f.t for f in frames],
                         events=events, conditions=conditions, raw=result.text)
