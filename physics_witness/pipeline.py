"""End-to-end run: story -> claims, video -> frames -> Cosmos events -> Ultra
verdicts -> evidence request. `rejudge` adds extra footage from the claimant
and re-judges the claims the first pass couldn't settle."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from . import act, costs, evidence, models, routing
from . import context as ctx
from .cache import ResponseCache
from .claims import Claim, split_claims
from .clients.tavily import TavilyClient
from .clients.token_factory import TokenFactoryClient, UsageLedger
from .config import Settings
from .crossexam import cross_examine
from .events import read_window
from .frames import Frame, sample_frames, save_frames, video_duration, windows

ProgressFn = Callable[[dict[str, Any]], None]
VERDICT_KEYS = ("supported", "contradicted", "cant_tell")


class Progress:
    """Reports the current stage plus a live cost snapshot after every model call."""

    def __init__(self, callback: ProgressFn | None, ledger: UsageLedger, prices: dict, base_rows: list | None = None):
        self.callback, self.ledger, self.prices = callback, ledger, prices
        self.base_rows = base_rows or []
        self.stage_name, self.detail = "starting", ""
        ledger.listeners.append(lambda _l: self.emit())

    def stage(self, name: str, detail: str = "") -> None:
        self.stage_name, self.detail = name, detail
        self.emit()

    def emit(self) -> None:
        if self.callback:
            rows = costs.merge_rows(self.base_rows, self.ledger.rows())
            self.callback({"stage": self.stage_name, "detail": self.detail, "cost": costs.estimate(rows, self.prices)})


def usage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_model: dict[str, dict[str, int]] = {}
    for r in rows:
        m = by_model.setdefault(r["model"], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        m["prompt_tokens"] += r["prompt_tokens"]
        m["completion_tokens"] += r["completion_tokens"]
        m["total_tokens"] += r["prompt_tokens"] + r["completion_tokens"]
    return {"calls": sum(r["calls"] for r in rows), "cache_hits": sum(r["cached_calls"] for r in rows),
            "by_model": by_model, "rows": rows}


def _read_source(client: TokenFactoryClient, model: str, sid: str, label: str, video: Path, progress: Progress, *,
                 max_frames: int, fps: float, window_size: int, overlap: int,
                 min_change: float) -> tuple[dict[str, Any], list[Frame]]:
    progress.stage("sampling", f"{label}: sampling frames")
    duration = video_duration(video)
    sampled = sample_frames(video, max_frames=max_frames, fps=fps, prefix="" if sid == "main" else f"{sid}_")
    kept, dropped = routing.drop_near_duplicates(sampled, min_change=min_change)
    groups = windows(kept, size=window_size, overlap=overlap)
    readings = []
    for i, g in enumerate(groups):
        progress.stage("seeing", f"{label}: Cosmos window {i + 1} of {len(groups)}")
        readings.append(read_window(client, model, g, i, source=sid))
    source = {
        "id": sid, "label": label, "video": Path(video).name, "duration_s": round(duration, 2),
        "frames": [{"t": round(f.t, 2), "motion": round(f.motion, 2), "file": f.filename} for f in kept],
        "frames_dropped": [round(f.t, 2) for f in dropped],
        "windows": [r.to_dict() for r in readings],
    }
    return source, kept


def _summary(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    return {k: sum(v["verdict"] == k for v in verdicts) for k in VERDICT_KEYS}


def _setup(settings: Settings | None, client: TokenFactoryClient | None, policy: str | None):
    settings = settings or Settings.from_env()
    cache = ResponseCache(settings.cache_dir)
    client = client or TokenFactoryClient(settings, cache, UsageLedger())
    names = models.resolve(client)
    policy = policy or settings.policy
    return settings, cache, client, names, policy, routing.route(names, policy), costs.load_prices(settings.prices_file)


def run(video: Path, story: str, *, settings: Settings | None = None, max_frames: int = 16,
        window_size: int = 6, overlap: int = 1, fps: float = 4.0, min_change: float = 1.5,
        out_dir: Path | None = None, place: str | None = None, date: str | None = None,
        client: TokenFactoryClient | None = None, tavily: TavilyClient | None = None,
        policy: str | None = None, on_progress: ProgressFn | None = None) -> dict[str, Any]:
    settings, cache, client, names, policy, routes, prices = _setup(settings, client, policy)
    tavily = tavily or TavilyClient(settings, cache)
    started = time.monotonic()
    progress = Progress(on_progress, client.ledger, prices)

    main, frames = _read_source(client, routes["see"], "main", "Original video", Path(video), progress,
                                max_frames=max_frames, fps=fps, window_size=window_size, overlap=overlap,
                                min_change=min_change)
    progress.stage("splitting", "Splitting the story into claims")
    claims = split_claims(client, routes["split"], story)
    progress.stage("context", "Looking up weather and place")
    context = ctx.gather(tavily, place, date)
    progress.stage("judging", "Cross-examining the story against the footage")
    verdicts = cross_examine(client, routes["judge"], claims, [main], ctx.for_prompt(context))

    text = {c.id: c.text for c in claims}
    linked = [evidence.link({**v.to_dict(), "claim": text[v.claim_id], "round": 0}, {"main": main}) for v in verdicts]
    progress.stage("asking", "Writing the evidence request")
    request = act.compose_request(client, routes["ask"], linked)
    progress.stage("done")

    rows = client.ledger.rows()
    report = {
        "video": str(video),
        "duration_s": main["duration_s"],
        "mode": "offline-stub" if client.offline else "live",
        "models": names,
        "routing": {"policy": policy, "routes": routes, "tasks": routing.TASKS},
        "story": story,
        "context": context,
        "sources": [main],
        "frames": main["frames"],
        "windows": main["windows"],
        "claims": [c.to_dict() for c in claims],
        "verdicts": linked,
        "summary": _summary(linked),
        "request": request,
        "rounds": [{"round": 0, "kind": "initial", "source": "main", "rejudged": [c.id for c in claims],
                    "changes": [], "request": request, "usage_rows": rows}],
        "usage": usage_summary(rows),
        "cost": costs.estimate(rows, prices),
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    if out_dir:
        out_dir = Path(out_dir)
        save_frames(frames, out_dir / "frames")
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def _ensure_sources(report: dict[str, Any]) -> None:
    """Reports saved before extra evidence existed have one implicit source."""
    if "sources" not in report:
        report["sources"] = [{"id": "main", "label": "Original video", "video": Path(report["video"]).name,
                              "duration_s": report["duration_s"], "frames": report["frames"],
                              "frames_dropped": [], "windows": report["windows"]}]
    report.setdefault("rounds", [{"round": 0, "kind": "initial", "source": "main", "rejudged": [],
                                  "changes": [], "request": None, "usage_rows": report["usage"].get("rows", [])}])


def rejudge(report: dict[str, Any], extra_video: Path, note: str = "", *, settings: Settings | None = None,
            max_frames: int = 16, window_size: int = 6, overlap: int = 1, fps: float = 4.0, min_change: float = 1.5,
            out_dir: Path | None = None, client: TokenFactoryClient | None = None, policy: str | None = None,
            on_progress: ProgressFn | None = None) -> dict[str, Any]:
    """Add extra footage as a new source and re-judge the Can't tell claims.
    Settled verdicts stay as they were. Returns the updated report."""
    settings, _cache, client, names, policy, routes, prices = _setup(settings, client, policy)
    _ensure_sources(report)
    started = time.monotonic()
    prior_rows = costs.merge_rows(*(r.get("usage_rows", []) for r in report["rounds"]))
    progress = Progress(on_progress, client.ledger, prices, base_rows=prior_rows)
    n = len(report["rounds"])
    sid = f"e{len(report['sources'])}"

    src, frames = _read_source(client, routes["see"], sid, f"Extra footage {len(report['sources'])}", Path(extra_video),
                               progress, max_frames=max_frames, fps=fps, window_size=window_size, overlap=overlap,
                               min_change=min_change)
    report["sources"].append(src)
    all_sources = {s["id"]: s for s in report["sources"]}

    pending = [v for v in report["verdicts"] if v["verdict"] == "cant_tell"]
    claims = [Claim(**c) for c in report["claims"] if c["id"] in {v["claim_id"] for v in pending}]
    changes = []
    if claims:
        progress.stage("judging", f"Re-judging {len(claims)} claim(s) with the extra footage")
        fresh = cross_examine(client, routes["judge"], claims, list(all_sources.values()),
                              ctx.for_prompt(report.get("context")), note=note or None)
        text = {c.id: c.text for c in claims}
        updated = {v.claim_id: evidence.link({**v.to_dict(), "claim": text[v.claim_id], "round": n}, all_sources)
                   for v in fresh}
        for i, old in enumerate(report["verdicts"]):
            new = updated.get(old["claim_id"])
            if new is None:
                continue
            report["verdicts"][i] = new
            if new["verdict"] != old["verdict"]:
                changes.append({"claim_id": old["claim_id"], "claim": old["claim"],
                                "before": old["verdict"], "after": new["verdict"]})
    progress.stage("asking", "Writing the next evidence request")
    request = act.compose_request(client, routes["ask"], report["verdicts"])
    progress.stage("done")

    rows = client.ledger.rows()
    report["rounds"].append({"round": n, "kind": "extra_evidence", "source": sid, "note": note,
                             "asked": (report.get("request") or {}).get("items", []),
                             "rejudged": [c.id for c in claims], "changes": changes, "request": request,
                             "usage_rows": rows, "elapsed_s": round(time.monotonic() - started, 2)})
    all_rows = costs.merge_rows(prior_rows, rows)
    report.update(request=request, summary=_summary(report["verdicts"]), usage=usage_summary(all_rows),
                  cost=costs.estimate(all_rows, prices),
                  mode="offline-stub" if client.offline else report.get("mode", "live"))
    if out_dir:
        out_dir = Path(out_dir)
        save_frames(frames, out_dir / "frames")
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report
