"""Tie each verdict to the sampled frames and Cosmos observations behind it,
so the UI can jump the video to them and show the reasoning alongside."""

from __future__ import annotations

from typing import Any

from .events import WindowReading
from .frames import Frame

SLACK_S = 0.5  # an observation counts for time t if t is within this of its span


def nearest_frame(frames: list[Frame], t: float) -> Frame:
    return min(frames, key=lambda f: abs(f.t - t))


def observations_at(readings: list[WindowReading], t: float) -> list[dict[str, Any]]:
    seen, out = set(), []
    for r in readings:
        for e in r.events:
            if e.t_start - SLACK_S <= t <= e.t_end + SLACK_S and e.what not in seen:
                seen.add(e.what)
                out.append({"window": r.window, "t_start": round(e.t_start, 2), "t_end": round(e.t_end, 2),
                            "what": e.what, "visibility": e.visibility})
    return out


def link(verdict: dict[str, Any], frames: list[Frame], readings: list[WindowReading]) -> dict[str, Any]:
    times = sorted(verdict.get("evidence_times") or [])
    ev_frames, ev_obs, seen = [], [], set()
    for t in times:
        f = nearest_frame(frames, t)
        if f.filename not in seen:
            seen.add(f.filename)
            ev_frames.append({"t": round(f.t, 2), "file": f.filename})
        for o in observations_at(readings, t):
            if o not in ev_obs:
                ev_obs.append(o)
    return {
        **verdict,
        "jump_to": times[0] if times else None,
        "evidence_frames": ev_frames,
        "observations": ev_obs,
    }
