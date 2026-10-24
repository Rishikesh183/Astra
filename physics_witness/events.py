"""Cosmos reads a window of timestamped frames and reports physical events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .clients.token_factory import TokenFactoryClient, image_part, text_part
from .frames import Frame
from .jsonutil import extract_json

COSMOS_PROMPT = """These {n} frames are in time order from one camera. Each frame has its timestamp printed in the black band at the bottom ({labels}).

Describe what physically happens, as an accident investigator would. Report only what the frames show:
- the camera itself: is it a dashcam looking out through a vehicle's windscreen, a fixed CCTV camera, or handheld? If it is in a vehicle, is that vehicle stationary or moving, and in which direction?
- which road users or objects are present and where they are relative to the camera (ahead of it, to its left or right, behind other objects),
- who moves, in which direction (left/right/toward/away from the camera), and in what order,
- any contact, and where on each object it happens,
- lighting (day/dusk/night) and weather or road surface (dry/wet/rain/snow),
- whether the view is clear, partly blocked, or too blurry to tell.

Two things matter a lot for the investigator, so check every pair of neighbouring frames for them:
- a camera jolt: the view suddenly tilts, shakes, spins or points at the sky or ground while it is still the same camera and place. That usually marks an impact on the camera's own vehicle. Give the timestamp of the first jolted frame.
- a scene cut: the footage switches to a different place, camera or recording (different street, different on-screen caption or overlay layout, a jump in lighting). Joined clips are common in forwarded videos. Give the timestamp of the first frame of the new footage. A jolt is not a cut.

Also copy any on-screen text exactly as written, such as a dashcam date, clock time, GPS position or speed readout, or a TV caption. Text in a non-Latin script can be copied as is.

Use the printed timestamps at the bottom of each frame for event times; on-screen clocks are only for the camera_overlay field. Do not guess speeds in numbers. If something cannot be seen, say so instead of guessing.

Return only JSON:
{{"events": [{{"t_start": 0.0, "t_end": 0.0, "what": "...", "actors": ["..."], "visibility": "clear|partial|poor"}}],
  "conditions": {{"lighting": "...", "weather": "...", "road_surface": "...", "view": "clear|partial|poor",
                  "camera": "dashcam|cctv|handheld|unknown", "camera_motion": "stationary|moving|unknown",
                  "camera_overlay": "on-screen text, or empty"}},
  "camera_jolts": [{{"t": 0.0, "what": "..."}}],
  "scene_cuts": [{{"t": 0.0, "what": "..."}}]}}
Use empty lists when there are none."""


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
    source: str = "main"
    camera_jolts: list = field(default_factory=list)   # [{"t", "what"}]
    scene_cuts: list = field(default_factory=list)     # [{"t", "what"}]

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


def _marks(items, frames: list[Frame]) -> list[dict]:
    """Jolts/cuts the model reported, snapped to the sampled frame times it saw.
    A mark on the window's first frame is dropped: there is no earlier frame
    in this window to compare it with, and the previous window (which overlaps
    by one frame) is the one that can see it."""
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            item = {"t": item}
        t = _as_float(item.get("t"), None)
        if t is None or not frames:
            continue
        snapped = min((f.t for f in frames), key=lambda ft: abs(ft - t))
        if abs(snapped - t) > 1.5:
            continue
        mark = {"t": round(snapped, 2), "what": str(item.get("what") or "").strip()[:300]}
        if mark["t"] > frames[0].t and all(m["t"] != mark["t"] for m in out):
            out.append(mark)
    return out


def read_window(client: TokenFactoryClient, model: str, frames: list[Frame], window: int,
                source: str = "main") -> WindowReading:
    labels = ", ".join(f.label() for f in frames)
    content = [text_part(COSMOS_PROMPT.format(n=len(frames), labels=labels))]
    content += [image_part(f.to_b64_jpeg()) for f in frames]
    result = client.chat(
        model,
        [{"role": "user", "content": content}],
        task="see",
        offline_reply=lambda: _stub_reply(frames),
        temperature=0.2,
        max_tokens=4096,
    )
    lo, hi = frames[0].t, frames[-1].t
    events, conditions, jolts, cuts = [], {}, [], []
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
        jolts = _marks(data.get("camera_jolts"), frames)
        cuts = _marks(data.get("scene_cuts"), frames)
    except (ValueError, AttributeError, TypeError):
        # Keep the free-text reading so Ultra still sees it.
        events.append(Event(t_start=lo, t_end=hi, what=result.text.strip()[:2000], visibility="partial", window=window))
    return WindowReading(window=window, t_start=lo, t_end=hi, frame_times=[f.t for f in frames],
                         events=events, conditions=conditions, raw=result.text, source=source,
                         camera_jolts=jolts, scene_cuts=cuts)
