"""The Liar Test case file: one CSV row per clip (edit it in any spreadsheet).

Columns
  id                short unique name, e.g. dash-012
  clip              video file name inside the clips folder
  true_story        what really happened, as a claimant would write it
  altered_story     the same story with ONE physical detail changed
  alteration        direction | order | time_of_day | weather | contact | presence | other
  altered_sentence  the exact sentence of altered_story that was changed
  place, date       optional; used for the Tavily weather/place lookup
  license           required: licence of the clip (e.g. CC-BY-4.0, self-filmed)
  source_url        where the clip came from (blank for self-filmed)
  notes             anything else
"""

from __future__ import annotations

import csv
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from ..claims import split_sentences
from ..store import VIDEO_EXTS

ALTERATIONS = ("direction", "order", "time_of_day", "weather", "contact", "presence", "other")
COLUMNS = ("id", "clip", "true_story", "altered_story", "alteration", "altered_sentence",
           "place", "date", "license", "source_url", "notes")


@dataclass
class Case:
    id: str
    clip: str
    true_story: str
    altered_story: str = ""
    alteration: str = ""
    altered_sentence: str = ""
    place: str = ""
    date: str = ""
    license: str = ""
    source_url: str = ""
    notes: str = ""

    def story(self, kind: str) -> str:
        return self.true_story if kind == "true" else self.altered_story


def load_cases(path: Path) -> list[Case]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = {"id", "clip", "true_story"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        names = {f.name for f in fields(Case)}
        return [Case(**{k: (v or "").strip() for k, v in row.items() if k in names})
                for row in reader if any((v or "").strip() for v in row.values())]


def save_cases(cases: list[Case], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for c in cases:
            w.writerow(asdict(c))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def validate(cases: list[Case], clips_dir: Path) -> tuple[list[str], list[str]]:
    """Returns (errors, warnings). Errors block `liar run`; warnings don't."""
    errors, warnings = [], []
    seen = set()
    for c in cases:
        where = f"[{c.id or '?'}]"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", c.id or ""):
            errors.append(f"{where} id must be letters, digits, '.', '_' or '-'")
        if c.id in seen:
            errors.append(f"{where} duplicate id")
        seen.add(c.id)
        clip = Path(clips_dir) / c.clip
        if not c.clip:
            errors.append(f"{where} no clip")
        elif Path(c.clip).suffix.lower() not in VIDEO_EXTS:
            errors.append(f"{where} {c.clip}: not a video file ({', '.join(sorted(VIDEO_EXTS))})")
        elif not clip.is_file():
            errors.append(f"{where} clip not found: {clip}")
        if not c.true_story:
            errors.append(f"{where} true_story is empty")
        if not c.altered_story:
            warnings.append(f"{where} no altered_story yet (run `physics-witness liar alter`); only the true story will run")
        else:
            if _norm(c.altered_story) == _norm(c.true_story):
                errors.append(f"{where} altered_story is identical to true_story")
            if c.alteration not in ALTERATIONS:
                errors.append(f"{where} alteration must be one of {', '.join(ALTERATIONS)}")
            if not c.altered_sentence:
                errors.append(f"{where} altered_sentence is empty (needed to score a catch)")
            elif _norm(c.altered_sentence) not in {_norm(s) for s in split_sentences(c.altered_story)}:
                errors.append(f"{where} altered_sentence is not one of the sentences of altered_story")
        if not c.license:
            errors.append(f"{where} license is empty: only openly licensed or self-filmed clips")
        elif c.license.lower() not in ("self-filmed", "self filmed", "own") and not c.source_url:
            warnings.append(f"{where} license {c.license!r} but no source_url")
    return errors, warnings
