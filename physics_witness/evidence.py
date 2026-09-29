"""Tie each verdict to the sampled frames and Cosmos observations behind it,
so the UI can jump the right video to them and show the reasoning alongside.

Works on report dicts, so it applies equally to a fresh run and to a
re-judge that adds extra footage to a saved report."""

from __future__ import annotations

from typing import Any

SLACK_S = 0.5  # an observation counts for time t if t is within this of its span


def nearest_frame(frames: list[dict[str, Any]], t: float) -> dict[str, Any]:
    return min(frames, key=lambda f: abs(f["t"] - t))


def observations_at(windows: list[dict[str, Any]], t: float) -> list[dict[str, Any]]:
    seen, out = set(), []
    for w in windows:
        for e in w["events"]:
            if e["t_start"] - SLACK_S <= t <= e["t_end"] + SLACK_S and e["what"] not in seen:
                seen.add(e["what"])
                out.append({"window": w["window"], "t_start": round(e["t_start"], 2), "t_end": round(e["t_end"], 2),
                            "what": e["what"], "visibility": e.get("visibility", "clear")})
    return out


def link(verdict: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """`sources` maps source id -> {"frames": [{"t", "file"}], "windows": [window dicts]}."""
    refs = sorted((r for r in verdict.get("evidence") or [] if r["source"] in sources),
                  key=lambda r: (r["source"] != "main", r["source"], r["t"]))
    ev_frames, ev_obs, seen = [], [], set()
    for r in refs:
        src = sources[r["source"]]
        if src["frames"]:
            f = nearest_frame(src["frames"], r["t"])
            if f["file"] not in seen:
                seen.add(f["file"])
                ev_frames.append({"source": r["source"], "t": round(f["t"], 2), "file": f["file"]})
        for o in observations_at(src["windows"], r["t"]):
            o = {"source": r["source"], **o}
            if o not in ev_obs:
                ev_obs.append(o)
    jump = refs[0] if refs else None
    return {
        **verdict,
        "jump_to": jump["t"] if jump and jump["source"] == "main" else None,
        "jump": jump,
        "evidence_frames": ev_frames,
        "observations": ev_obs,
    }
