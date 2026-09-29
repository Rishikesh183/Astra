"""End-to-end run: story -> claims, video -> frames -> Cosmos events -> Ultra verdicts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import models
from .cache import ResponseCache
from .claims import split_claims
from .clients.token_factory import TokenFactoryClient, UsageLedger
from .config import Settings
from .crossexam import cross_examine
from .events import read_window
from .frames import sample_frames, save_frames, video_duration, windows


def run(video: Path, story: str, *, settings: Settings | None = None, max_frames: int = 16,
        window_size: int = 6, overlap: int = 1, fps: float = 4.0, out_dir: Path | None = None,
        context: dict | None = None, client: TokenFactoryClient | None = None) -> dict[str, Any]:
    settings = settings or Settings.from_env()
    client = client or TokenFactoryClient(settings, ResponseCache(settings.cache_dir), UsageLedger())
    started = time.monotonic()
    names = models.resolve(client)

    video = Path(video)
    duration = video_duration(video)
    frames = sample_frames(video, max_frames=max_frames, fps=fps)
    groups = windows(frames, size=window_size, overlap=overlap)

    claims = split_claims(client, names["splitter"] or names["ultra"], story)
    readings = [read_window(client, names["cosmos"], g, i) for i, g in enumerate(groups)]
    verdicts = cross_examine(client, names["ultra"], claims, readings, duration, context)

    claim_by_id = {c.id: c for c in claims}
    report = {
        "video": str(video),
        "duration_s": round(duration, 2),
        "mode": "offline-stub" if client.offline else "live",
        "models": names,
        "frames": [{"t": round(f.t, 2), "motion": round(f.motion, 2)} for f in frames],
        "windows": [r.to_dict() for r in readings],
        "claims": [c.to_dict() for c in claims],
        "verdicts": [{**v.to_dict(), "claim": claim_by_id[v.claim_id].text} for v in verdicts],
        "summary": {k: sum(v.verdict == k for v in verdicts) for k in ("supported", "contradicted", "cant_tell")},
        "usage": client.ledger.to_dict(),
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    if out_dir:
        out_dir = Path(out_dir)
        save_frames(frames, out_dir / "frames")
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report
