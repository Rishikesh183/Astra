"""The five Day 1-2 kill-switch checks. Each returns PASS / FAIL / SKIP / MANUAL
with detail, so the team can decide by end of Day 2 whether to commit."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import models
from .cache import ResponseCache
from .clients.tavily import TavilyClient
from .clients.token_factory import TokenFactoryClient, image_part, text_part
from .config import Settings
from .frames import ffmpeg_exe, sample_frames, save_frames, windows


@dataclass
class CheckResult:
    number: int
    name: str
    status: str   # PASS | FAIL | SKIP | MANUAL
    detail: str
    seconds: float = 0.0


def _timed(number: int, name: str, fn) -> CheckResult:
    t0 = time.monotonic()
    try:
        status, detail = fn()
    except Exception as exc:  # a check failing must not stop the others
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
    return CheckResult(number, name, status, detail, round(time.monotonic() - t0, 2))


def run_checks(settings: Settings, clip: Path | None = None, date: str = "2026-09-01",
               place: str = "Bengaluru, India") -> list[CheckResult]:
    # Checks always hit the live API: no cache, so a PASS means the service answered now.
    client = TokenFactoryClient(settings, ResponseCache(settings.cache_dir, enabled=False))
    tavily = TavilyClient(settings, ResponseCache(settings.cache_dir, enabled=False))
    names: dict[str, str] = {}
    frames = []
    results = []

    def no_key():
        return ("SKIP", "NEBIUS_API_KEY not set (or PW_OFFLINE=1)") if client.offline else None

    def check_frames():
        # Local stand-in for check 5 so checks 1-2 have frames to send.
        nonlocal frames
        if clip is None:
            return "SKIP", "no --clip given"
        frames = sample_frames(clip, max_frames=8)
        out = save_frames(frames, Path(".cache/checks/frames"))
        return "PASS", f"ffmpeg at {ffmpeg_exe()}; {len(frames)} frames with timestamps -> {out[0].parent}"

    def check_cosmos():
        if skip := no_key():
            return skip
        names.update(models.discover(client.list_models()))
        names["cosmos"] = settings.cosmos_model or names["cosmos"]
        if not names["cosmos"]:
            return "FAIL", "no Cosmos model in /models listing"
        content = [text_part("Describe what is physically happening in this image in two sentences.")]
        if frames:
            content.append(image_part(frames[0].to_b64_jpeg()))
        r = client.chat(names["cosmos"], [{"role": "user", "content": content}], max_tokens=1024)
        return ("PASS" if r.text.strip() else "FAIL"), f"model={names['cosmos']}: {r.text.strip()[:200]!r}"

    def check_video():
        if skip := no_key():
            return skip
        if not frames or not names.get("cosmos"):
            return "SKIP", "needs --clip and a working Cosmos model"
        from .events import read_window
        group = windows(frames, size=8)[0]
        reading = read_window(client, names["cosmos"], group, 0)
        timed = [e for e in reading.events if e.t_end > e.t_start]
        detail = "; ".join(f"{e.t_start:.1f}-{e.t_end:.1f}s {e.what[:60]}" for e in reading.events[:5])
        return ("PASS" if timed else "FAIL"), f"{len(group)} frames -> {len(reading.events)} events ({len(timed)} with a time span): {detail}"

    def check_ultra():
        if skip := no_key():
            return skip
        if not names:
            names.update(models.discover(client.list_models()))
        model = settings.ultra_model or names.get("ultra")
        if not model:
            return "FAIL", "no Nemotron Ultra in /models listing (fallback: Super 120B)"
        filler = ("Observation log line: a sedan waits at a red light, lane 2, dry road. " * 900)
        t0 = time.monotonic()
        r = client.chat(model, [{"role": "user", "content": filler + "\n\nReply with the single word OK."}], max_tokens=512)
        took = time.monotonic() - t0
        return ("PASS" if r.text.strip() else "FAIL"), f"model={model}, prompt_tokens={r.usage.get('prompt_tokens')}, {took:.1f}s: {r.text.strip()[:80]!r}"

    def check_tavily():
        if tavily.offline:
            return "SKIP", "TAVILY_API_KEY not set (or PW_OFFLINE=1)"
        data = tavily.weather(place, date)
        n = len(data.get("results", []))
        return ("PASS" if n or data.get("answer") else "FAIL"), f"{n} results; answer={str(data.get('answer'))[:160]!r}"

    def check_sandbox():
        return "MANUAL", ("Token Factory Sandbox API not wired yet: start a Sandbox, run "
                          "`python -m physics_witness frames <clip>` inside it, confirm frames are written. "
                          "Fallback: run frame extraction as a Serverless Job.")

    frame_result = _timed(0, "Local ffmpeg frame sampling", check_frames)
    results.append(frame_result)
    results.append(_timed(1, "Cosmos is callable", check_cosmos))
    results.append(_timed(2, "Video works (frames + timestamps -> timed events)", check_video))
    results.append(_timed(3, "Ultra is callable (~20K-token prompt)", check_ultra))
    results.append(_timed(4, "Tavily call", check_tavily))
    results.append(_timed(5, "Sandbox runs our code", check_sandbox))
    return results
