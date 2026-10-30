"""Ready-made sample claims for people who arrive without their own footage
(e.g. hackathon judges). They live in a data file, not in code:

    demo/samples.json   (or the file named by PW_SAMPLES)

    {"samples": [{"id": "train-true", "title": "...", "label": "real" | "liar",
                  "video": "../short_test_video.webm", "story": "...",
                  "lie_sentence": "...", "place": "", "date": "", "license": "...",
                  "source_url": "", "description": "..."}]}

`video` is relative to the samples file. With PW_STORE=supabase, a clip that
is not on disk (e.g. git-ignored, so absent on a deployed server) is fetched
at start-up from the private bucket as `samples/<file name>`; put it there with
`physics-witness supabase samples`. Entries whose video is still missing
are listed as unavailable instead of failing, so a sample can be described in
the file before its clip is added. Running a sample twice reuses the cached
model replies, so after the first run a sample costs nothing and answers the
same way every time.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .store import VIDEO_EXTS

LABELS = ("real", "liar")
SAMPLE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


@dataclass
class Sample:
    id: str
    title: str
    label: str
    story: str
    video: Path
    lie_sentence: str = ""
    place: str = ""
    date: str = ""
    license: str = ""
    source_url: str = ""
    description: str = ""

    @property
    def available(self) -> bool:
        return self.video.is_file()

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("video")
        d["available"] = self.available
        return d


def load_samples(path: Path | None) -> list[Sample]:
    """Read the samples file. A missing file means no samples; a malformed
    entry is skipped (and reported by `check_samples`), never fatal."""
    if not path or not Path(path).is_file():
        return []
    base = Path(path).resolve().parent
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out, seen = [], set()
    for item in raw.get("samples", []):
        if not isinstance(item, dict):
            continue
        sid, video = str(item.get("id", "")), str(item.get("video", ""))
        if (not SAMPLE_ID.match(sid) or sid in seen or item.get("label") not in LABELS
                or not str(item.get("story", "")).strip() or Path(video).suffix.lower() not in VIDEO_EXTS):
            continue
        seen.add(sid)
        out.append(Sample(id=sid, title=str(item.get("title") or sid), label=item["label"],
                          story=str(item["story"]).strip(), video=(base / video).resolve(),
                          **{k: str(item.get(k) or "") for k in
                             ("lie_sentence", "place", "date", "license", "source_url", "description")}))
    return out


SAMPLES_PREFIX = "samples/"


def remote_name(sample: Sample) -> str:
    return SAMPLES_PREFIX + sample.video.name


def fetch_missing(samples: list[Sample], backend) -> list[str]:
    """Download sample clips that are missing on disk from the shared bucket.
    Returns the ids fetched. A clip that is not in the bucket stays unavailable."""
    fetched = []
    for s in samples:
        if s.available:
            continue
        try:
            if backend.get_file(remote_name(s), s.video):
                fetched.append(s.id)
        except Exception as exc:          # a sample is never worth failing start-up for
            print(f"physics-witness: could not fetch sample {s.id}: {exc}")
    return fetched


def check_samples(path: Path) -> list[str]:
    """Human-readable problems with the samples file (for the CLI)."""
    if not Path(path).is_file():
        return [f"{path} not found"]
    problems = []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    loaded = {s.id: s for s in load_samples(path)}
    for item in raw.get("samples", []):
        sid = str(item.get("id", "?")) if isinstance(item, dict) else "?"
        if sid not in loaded:
            problems.append(f"[{sid}] skipped: needs a lowercase id, label real|liar, a story and a video file name")
            continue
        s = loaded[sid]
        if not s.available:
            problems.append(f"[{sid}] video not found: {s.video}")
        if s.label == "liar" and (not s.lie_sentence or s.lie_sentence not in s.story):
            problems.append(f"[{sid}] liar sample: lie_sentence must be copied exactly from the story")
        if not s.license:
            problems.append(f"[{sid}] no license: only openly licensed or self-filmed clips for a public demo")
    return problems
