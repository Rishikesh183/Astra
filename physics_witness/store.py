"""Claims on disk: one directory per claim with the video, story, report and
evidence frames. Runs execute on a small thread pool so the UI stays responsive."""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .config import Settings

RUN_ID = re.compile(r"^[a-f0-9]{12}$")
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}


class RunStore:
    def __init__(self, root: Path, settings: Settings | None = None, workers: int = 2,
                 runner: Callable[..., dict[str, Any]] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings or Settings.from_env()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pw-run")
        self._lock = threading.Lock()
        if runner is None:
            from .pipeline import run as runner
        self._runner = runner

    # --- paths --------------------------------------------------------------

    def dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id or ""):
            raise KeyError(run_id)
        path = self.root / run_id
        if not path.is_dir():
            raise KeyError(run_id)
        return path

    def video_path(self, run_id: str) -> Path:
        d = self.dir(run_id)
        for p in d.iterdir():
            if p.stem == "video" and p.suffix.lower() in VIDEO_EXTS:
                return p
        raise KeyError(run_id)

    def frame_path(self, run_id: str, name: str) -> Path:
        if not re.fullmatch(r"frame_[0-9.]+s\.jpg", name):
            raise KeyError(name)
        p = self.dir(run_id) / "frames" / name
        if not p.is_file():
            raise KeyError(name)
        return p

    # --- metadata -----------------------------------------------------------

    def _write_meta(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            path = self.root / run_id / "meta.json"
            meta = json.loads(path.read_text()) if path.is_file() else {}
            meta.update(changes)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
            tmp.replace(path)
            return meta

    def meta(self, run_id: str) -> dict[str, Any]:
        return json.loads((self.dir(run_id) / "meta.json").read_text())

    def report(self, run_id: str) -> dict[str, Any] | None:
        p = self.dir(run_id) / "report.json"
        return json.loads(p.read_text()) if p.is_file() else None

    def list(self) -> list[dict[str, Any]]:
        out = []
        for d in self.root.iterdir():
            if RUN_ID.match(d.name) and (d / "meta.json").is_file():
                try:
                    out.append(json.loads((d / "meta.json").read_text()))
                except json.JSONDecodeError:
                    continue
        return sorted(out, key=lambda m: m.get("created", 0), reverse=True)

    # --- runs ---------------------------------------------------------------

    def create(self, video_src: Path, story: str, *, title: str = "", place: str = "", date: str = "",
               move: bool = False, wait: bool = False) -> str:
        suffix = Path(video_src).suffix.lower()
        if suffix not in VIDEO_EXTS:
            raise ValueError(f"unsupported video type {suffix!r}; use one of {sorted(VIDEO_EXTS)}")
        if not story.strip():
            raise ValueError("story is empty")
        run_id = uuid.uuid4().hex[:12]
        d = self.root / run_id
        d.mkdir(parents=True)
        dest = d / f"video{suffix}"
        (shutil.move if move else shutil.copyfile)(str(video_src), str(dest))
        (d / "story.txt").write_text(story)
        self._write_meta(run_id, id=run_id, title=title.strip() or story.strip().split("\n")[0][:80],
                         place=place.strip(), date=date.strip(), status="queued",
                         created=time.time(), error=None, summary=None)
        future = self._pool.submit(self._execute, run_id)
        if wait:
            future.result()
        return run_id

    def _execute(self, run_id: str) -> None:
        meta = self._write_meta(run_id, status="running", started=time.time())
        d = self.root / run_id
        try:
            report = self._runner(self.video_path(run_id), (d / "story.txt").read_text(),
                                  settings=self.settings, out_dir=d,
                                  place=meta.get("place") or None, date=meta.get("date") or None)
            self._write_meta(run_id, status="done", finished=time.time(),
                             summary=report.get("summary"), mode=report.get("mode"))
        except Exception as exc:  # surface any failure to the UI instead of losing it
            (d / "error.log").write_text(traceback.format_exc())
            self._write_meta(run_id, status="error", finished=time.time(), error=f"{type(exc).__name__}: {exc}")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
