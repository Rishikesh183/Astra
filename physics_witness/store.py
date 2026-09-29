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
SOURCE_ID = re.compile(r"^(main|e\d{1,2})$")
FRAME_NAME = re.compile(r"^(e\d{1,2}_)?frame_[0-9.]+s\.jpg$")
MAX_EXTRA = 5
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
        from .pipeline import rejudge
        self._rejudger = rejudge

    # --- paths --------------------------------------------------------------

    def dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id or ""):
            raise KeyError(run_id)
        path = self.root / run_id
        if not path.is_dir():
            raise KeyError(run_id)
        return path

    def video_path(self, run_id: str, source: str = "main") -> Path:
        if not SOURCE_ID.match(source or ""):
            raise KeyError(source)
        stem = "video" if source == "main" else source
        d = self.dir(run_id)
        for p in d.iterdir():
            if p.stem == stem and p.suffix.lower() in VIDEO_EXTS:
                return p
        raise KeyError(run_id)

    def frame_path(self, run_id: str, name: str) -> Path:
        if not FRAME_NAME.match(name or ""):
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

    def _progress(self, run_id: str) -> Callable[[dict[str, Any]], None]:
        return lambda p: self._write_meta(run_id, progress=p)

    def _execute(self, run_id: str) -> None:
        meta = self._write_meta(run_id, status="running", started=time.time(), progress=None)
        d = self.root / run_id
        try:
            report = self._runner(self.video_path(run_id), (d / "story.txt").read_text(),
                                  settings=self.settings, out_dir=d, on_progress=self._progress(run_id),
                                  place=meta.get("place") or None, date=meta.get("date") or None)
            self._write_meta(run_id, status="done", finished=time.time(), summary=report.get("summary"),
                             mode=report.get("mode"), rounds=len(report.get("rounds", [])),
                             cost=report.get("cost"))
        except Exception as exc:  # surface any failure to the UI instead of losing it
            (d / "error.log").write_text(traceback.format_exc())
            self._write_meta(run_id, status="error", finished=time.time(), error=f"{type(exc).__name__}: {exc}")

    def add_evidence(self, run_id: str, video_src: Path, note: str = "", *, move: bool = False,
                     wait: bool = False) -> str:
        """Attach extra footage the claimant sent and re-judge in the background.
        Returns the new source id (e1, e2, ...)."""
        meta = self.meta(run_id)
        if meta.get("status") != "done":
            raise ValueError(f"claim is {meta.get('status')}; wait until it is done")
        suffix = Path(video_src).suffix.lower()
        if suffix not in VIDEO_EXTS:
            raise ValueError(f"unsupported video type {suffix!r}; use one of {sorted(VIDEO_EXTS)}")
        report = self.report(run_id) or {}
        n = len(report.get("sources") or [None])
        if n > MAX_EXTRA:
            raise ValueError(f"at most {MAX_EXTRA} extra clips per claim")
        sid = f"e{n}"
        d = self.dir(run_id)
        (shutil.move if move else shutil.copyfile)(str(video_src), str(d / f"{sid}{suffix}"))
        self._write_meta(run_id, status="queued", stage_kind="rejudge", error=None)
        future = self._pool.submit(self._execute_rejudge, run_id, sid, note)
        if wait:
            future.result()
        return sid

    def _execute_rejudge(self, run_id: str, sid: str, note: str) -> None:
        self._write_meta(run_id, status="running", progress=None)
        d = self.root / run_id
        try:
            report = self._rejudger(self.report(run_id), self.video_path(run_id, sid), note,
                                    settings=self.settings, out_dir=d, on_progress=self._progress(run_id))
            self._write_meta(run_id, status="done", finished=time.time(), summary=report.get("summary"),
                             mode=report.get("mode"), rounds=len(report.get("rounds", [])),
                             cost=report.get("cost"), stage_kind=None)
        except Exception as exc:
            (d / "error.log").write_text(traceback.format_exc())
            # The saved report is untouched, so the claim stays usable; drop the clip that failed.
            self.video_path(run_id, sid).unlink(missing_ok=True)
            self._write_meta(run_id, status="done", stage_kind=None, finished=time.time(),
                             error=f"Extra evidence failed: {type(exc).__name__}: {exc}")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
