"""Claims: one record per claim (metadata, report) plus its video, extra clips
and evidence frames. Where the records live is the backend's job
(backends.py: local files, or Supabase Postgres + Storage); the local claim
folder is always the working copy that runs read and write. Runs execute on a
small thread pool so the UI stays responsive."""

from __future__ import annotations

import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .backends import make_backend
from .config import Settings
from .frames import Cancelled, check_cancel

RUN_ID = re.compile(r"^[a-f0-9]{12}$")
BUSY = ("queued", "running")
SOURCE_ID = re.compile(r"^(main|e\d{1,2})$")
FRAME_NAME = re.compile(r"^(e\d{1,2}_)?frame_[0-9.]+s\.jpg$")
MAX_EXTRA = 5
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
# A shared store may hold claims another server instance is running. Only a
# claim that has not reported progress for this long is treated as dead.
STALE_S = 600


class RunStore:
    def __init__(self, root: Path, settings: Settings | None = None, workers: int = 2,
                 runner: Callable[..., dict[str, Any]] | None = None, backend=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings or Settings.from_env()
        self.backend = backend or make_backend(self.settings, self.root)
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pw-run")
        self._lock = threading.Lock()
        if runner is None:
            from .pipeline import run as runner
        self._runner = runner
        from .pipeline import rejudge
        self._rejudger = rejudge
        self._cancel: dict[str, threading.Event] = {}
        self._recover_interrupted()

    @property
    def shared(self) -> bool:
        """True when other server instances may use the same store."""
        return self.backend.kind != "files"

    def _recover_interrupted(self) -> None:
        """Runs only live in a server's threads. With local files, anything still
        queued/running at start-up died with the previous process. With a shared
        store, only claims that have gone quiet (no progress for STALE_S) are."""
        for meta in self.backend.list_metas():
            self._expire(meta, startup=True)

    def _expire(self, meta: dict[str, Any], startup: bool = False) -> dict[str, Any]:
        run_id = meta.get("id")
        if meta.get("status") not in BUSY or not run_id or run_id in self._cancel:
            return meta
        quiet = time.time() - float(meta.get("updated") or meta.get("created") or 0)
        if not (startup and not self.shared) and quiet < STALE_S:
            return meta
        if self.backend.read_report(run_id) is not None:
            # A re-judge was cut off: the saved report is intact, drop the half-added clip.
            self._drop_unjudged_clips(run_id)
            return self._write_meta(run_id, status="done", stage_kind=None, progress=None,
                                    error="Extra footage was interrupted because the server stopped. Upload it again.")
        return self._write_meta(run_id, status="error", progress=None,
                                error="Interrupted: the server stopped while this claim was running. Delete it and upload again.")

    def _drop_unjudged_clips(self, run_id: str) -> None:
        d = self.root / run_id
        if not d.is_dir():
            return
        known = {s["id"] for s in (self.backend.read_report(run_id) or {}).get("sources", [{"id": "main"}])}
        for f in d.iterdir():
            if re.fullmatch(r"e\d{1,2}", f.stem) and f.suffix.lower() in VIDEO_EXTS and f.stem not in known:
                f.unlink(missing_ok=True)

    # --- paths --------------------------------------------------------------

    def dir(self, run_id: str) -> Path:
        """The claim's local working folder (created on demand for claims that
        live in a shared store). KeyError if the claim does not exist."""
        if not RUN_ID.match(run_id or ""):
            raise KeyError(run_id)
        path = self.root / run_id
        if path.is_dir() and (not self.shared or self.backend.exists(run_id)):
            return path
        if self.shared and self.backend.exists(run_id):
            path.mkdir(parents=True, exist_ok=True)
            return path
        raise KeyError(run_id)

    def _video_name(self, run_id: str, source: str) -> str | None:
        stem = "video" if source == "main" else source
        d = self.root / run_id
        for p in d.iterdir() if d.is_dir() else []:
            if p.stem == stem and p.suffix.lower() in VIDEO_EXTS:
                return p.name
        return (self.meta(run_id).get("videos") or {}).get(source)

    def video_path(self, run_id: str, source: str = "main") -> Path:
        """Local copy of a claim's video. KeyError if there is none here (use video_url)."""
        if not SOURCE_ID.match(source or ""):
            raise KeyError(source)
        self.dir(run_id)
        name = self._video_name(run_id, source)
        p = self.root / run_id / name if name else None
        if p is None or not p.is_file():
            raise KeyError(run_id)
        return p

    def video_url(self, run_id: str, source: str = "main") -> str | None:
        """Signed URL of a video kept in the shared store (None for local files)."""
        if not SOURCE_ID.match(source or ""):
            raise KeyError(source)
        self.dir(run_id)
        name = self._video_name(run_id, source)
        return self.backend.file_url(run_id, name) if name else None

    def frame_path(self, run_id: str, name: str) -> Path:
        if not FRAME_NAME.match(name or ""):
            raise KeyError(name)
        p = self.dir(run_id) / "frames" / name
        if not p.is_file():
            raise KeyError(name)
        return p

    def frame_url(self, run_id: str, name: str) -> str | None:
        if not FRAME_NAME.match(name or ""):
            raise KeyError(name)
        self.dir(run_id)
        return self.backend.file_url(run_id, f"frames/{name}")

    # --- metadata -----------------------------------------------------------

    def _write_meta(self, run_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            meta = self.backend.read_meta(run_id) or {}
            meta.update(changes)
            meta["updated"] = time.time()          # heartbeat for _expire
            self.backend.write_meta(run_id, meta)
            return meta

    def meta(self, run_id: str) -> dict[str, Any]:
        if not RUN_ID.match(run_id or ""):
            raise KeyError(run_id)
        meta = self.backend.read_meta(run_id)
        if meta is None:
            raise KeyError(run_id)
        return self._expire(meta) if self.shared else meta

    def report(self, run_id: str) -> dict[str, Any] | None:
        self.meta(run_id)
        return self.backend.read_report(run_id)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for meta in self.backend.list_metas():
            if not RUN_ID.match(str(meta.get("id", ""))):
                continue
            if self.shared:
                meta = self._expire(meta)
            if meta.get("status") == "done" and "thumb" not in meta:
                try:   # claims made before the home page existed
                    report = self.backend.read_report(meta["id"])
                    if report:
                        meta = self._write_meta(meta["id"], **self.card_fields(report))
                except (KeyError, TypeError, ValueError):
                    pass
            out.append(meta)
        return sorted(out, key=lambda m: m.get("created", 0), reverse=True)

    # --- runs ---------------------------------------------------------------

    def create(self, video_src: Path, story: str, *, title: str = "", place: str = "", date: str = "",
               move: bool = False, wait: bool = False, sample: dict[str, str] | None = None) -> str:
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
        (d / "story.txt").write_text(story, encoding="utf-8")
        self._cancel[run_id] = threading.Event()
        self._write_meta(run_id, id=run_id, title=title.strip() or story.strip().split("\n")[0][:80],
                         place=place.strip(), date=date.strip(), status="queued", story_text=story,
                         created=time.time(), error=None, summary=None, sample=sample,
                         videos={"main": dest.name}, objects=[])
        future = self._pool.submit(self._execute, run_id)
        if wait:
            future.result()
        return run_id

    @staticmethod
    def card_fields(report: dict[str, Any]) -> dict[str, Any]:
        """What the home page shows for a claim, kept with the metadata so listing
        claims never has to open every report."""
        sources = report.get("sources") or []
        frames = report.get("frames") or []
        cost = report.get("cost") or {}
        return {
            "thumb": frames[min(1, len(frames) - 1)]["file"] if frames else None,
            "duration_s": report.get("duration_s"),
            "claims": len(report.get("claims") or []),
            "story": (report.get("story") or "")[:220],
            "tokens": cost.get("tokens"),
            "usd": cost.get("total_usd") if cost.get("priced") else None,
            "joined": any(len(s.get("segments") or []) > 1 for s in sources),
            "extra_clips": max(0, len(sources) - 1),
        }

    def _progress(self, run_id: str) -> Callable[[dict[str, Any]], None]:
        def on_progress(p: dict[str, Any]) -> None:
            meta = self._write_meta(run_id, progress=p)
            if meta.get("cancel_requested"):        # set by another server instance
                self._cancel.setdefault(run_id, threading.Event()).set()
        return on_progress

    def _publish(self, run_id: str, report: dict[str, Any], extra_video: str | None = None) -> dict[str, Any]:
        """Save the report and upload the claim's files to the store."""
        self.backend.write_report(run_id, report)
        d = self.root / run_id
        meta = self.backend.read_meta(run_id) or {}
        known = set(meta.get("objects") or [])
        files = [(p.name, p) for p in d.iterdir() if p.suffix.lower() in VIDEO_EXTS]
        files += [(f"frames/{p.name}", p) for p in sorted((d / "frames").glob("*.jpg"))]
        new = [(rel, p) for rel, p in files if f"{run_id}/{rel}" not in known]
        uploaded = self.backend.publish_files(run_id, new)
        videos = dict(meta.get("videos") or {})
        if extra_video:
            videos[Path(extra_video).stem] = extra_video
        return {"objects": sorted(known | set(uploaded)), "videos": videos}

    def _execute(self, run_id: str) -> None:
        cancel = self._cancel.setdefault(run_id, threading.Event())
        d = self.root / run_id
        try:
            check_cancel(cancel)                      # cancelled while still queued
            meta = self._write_meta(run_id, status="running", started=time.time(), progress=None)
            check_cancel(cancel if not meta.get("cancel_requested") else _set(cancel))
            story = meta.get("story_text") or (d / "story.txt").read_text(encoding="utf-8")
            report = self._runner(self.video_path(run_id), story,
                                  settings=self.settings, out_dir=d, on_progress=self._progress(run_id),
                                  place=meta.get("place") or None, date=meta.get("date") or None, cancel=cancel)
            files = self._publish(run_id, report)
            self._write_meta(run_id, status="done", finished=time.time(), summary=report.get("summary"),
                             mode=report.get("mode"), rounds=len(report.get("rounds", [])),
                             cost=report.get("cost"), cancelling=False, cancel_requested=False,
                             **files, **self.card_fields(report))
        except Cancelled:
            self.backend.clear_report(run_id)
            (d / "report.json").unlink(missing_ok=True)
            self._write_meta(run_id, status="cancelled", finished=time.time(), progress=None, error=None,
                             cancelling=False, cancel_requested=False)
        except Exception as exc:  # surface any failure to the UI instead of losing it
            (d / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            self._write_meta(run_id, status="error", finished=time.time(), error=f"{type(exc).__name__}: {exc}")
        finally:
            self._cancel.pop(run_id, None)

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
        self._cancel[run_id] = threading.Event()
        self._write_meta(run_id, status="queued", stage_kind="rejudge", error=None)
        future = self._pool.submit(self._execute_rejudge, run_id, sid, note)
        if wait:
            future.result()
        return sid

    def _execute_rejudge(self, run_id: str, sid: str, note: str) -> None:
        cancel = self._cancel.setdefault(run_id, threading.Event())
        d = self.root / run_id
        try:
            check_cancel(cancel)
            self._write_meta(run_id, status="running", progress=None)
            clip = self.video_path(run_id, sid)
            report = self._rejudger(self.backend.read_report(run_id), clip, note,
                                    settings=self.settings, out_dir=d, on_progress=self._progress(run_id),
                                    cancel=cancel)
            files = self._publish(run_id, report, extra_video=clip.name)
            self._write_meta(run_id, status="done", finished=time.time(), summary=report.get("summary"),
                             mode=report.get("mode"), rounds=len(report.get("rounds", [])),
                             cost=report.get("cost"), stage_kind=None, cancelling=False, cancel_requested=False,
                             **files, **self.card_fields(report))
        except Cancelled:
            # The saved report was never touched; just drop the clip.
            _unlink_video(self, run_id, sid)
            self._write_meta(run_id, status="done", stage_kind=None, progress=None, finished=time.time(),
                             error="Extra footage cancelled. The verdicts are unchanged.",
                             cancelling=False, cancel_requested=False)
        except Exception as exc:
            (d / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            # The saved report is untouched, so the claim stays usable; drop the clip that failed.
            _unlink_video(self, run_id, sid)
            self._write_meta(run_id, status="done", stage_kind=None, finished=time.time(),
                             error=f"Extra evidence failed: {type(exc).__name__}: {exc}")
        finally:
            self._cancel.pop(run_id, None)

    def cancel(self, run_id: str) -> bool:
        """Ask a queued or running claim to stop. It stops at the next stage or
        model call (within a second or two while sampling frames). Returns False
        if there was nothing to cancel."""
        meta = self.meta(run_id)
        if meta.get("status") not in BUSY:
            return False
        event = self._cancel.get(run_id)
        if event is None:
            if self.shared and time.time() - float(meta.get("updated") or 0) < STALE_S:
                # Running on another server instance: it checks this flag after every step.
                self._write_meta(run_id, cancel_requested=True, cancelling=True)
            else:
                # Not running anywhere any more (stale after a restart): mark it directly.
                self._write_meta(run_id, status="cancelled", progress=None)
            return True
        # Flag first, then signal: the run writes cancelling=False when it stops,
        # and that must not be overwritten by a late cancelling=True.
        self._write_meta(run_id, cancelling=True)
        event.set()
        return True

    def delete(self, run_id: str) -> None:
        meta = self.meta(run_id)
        if meta.get("status") in BUSY:
            raise ValueError("cancel the claim before deleting it")
        self.backend.delete(run_id, meta)
        shutil.rmtree(self.root / run_id, ignore_errors=True)
        self._cancel.pop(run_id, None)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def _set(event: threading.Event) -> threading.Event:
    event.set()
    return event


def _unlink_video(store: RunStore, run_id: str, sid: str) -> None:
    try:
        store.video_path(run_id, sid).unlink(missing_ok=True)
    except KeyError:
        pass
