"""Cancel / delete claims, recovery after a restart, and long-video sampling."""

import json
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from physics_witness import cli, pipeline
from physics_witness.config import Settings
from physics_witness.frames import (MAX_CANDIDATES, Cancelled, extract_candidates, ffmpeg_exe, sample_frames,
                                    video_duration)
from physics_witness.server import create_app
from physics_witness.store import RunStore
from test_smoke import STORY


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("long") / "long.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=duration=150:size=320x180:rate=10",
                    "-pix_fmt", "yuv420p", "-g", "50", str(out)], check=True)
    return out


def wait_for(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# --- frames ------------------------------------------------------------------------

def test_duration_from_header_is_fast(clip, long_clip):
    t0 = time.time()
    assert video_duration(long_clip) == pytest.approx(150, abs=0.2)
    assert video_duration(clip) == pytest.approx(10, abs=0.2)
    assert time.time() - t0 < 2


def test_long_clip_is_sampled_by_seeking_with_a_capped_budget(long_clip):
    cands = extract_candidates(long_clip, fps=4)
    times = [t for t, _ in cands]
    assert 2 <= len(cands) <= MAX_CANDIDATES
    assert times == sorted(times) and times[0] == 0.0 and times[-1] > 145
    frames = sample_frames(long_clip, max_frames=8)
    assert len(frames) == 8 and frames[-1].t > 145


def test_cancel_stops_sampling(clip, long_clip):
    stop = threading.Event()
    stop.set()
    with pytest.raises(Cancelled):
        sample_frames(long_clip, cancel=stop)
    # Cancel while ffmpeg is decoding a short clip at a high rate.
    stop = threading.Event()
    threading.Timer(0.15, stop.set).start()
    t0 = time.time()
    with pytest.raises(Cancelled):
        extract_candidates(long_clip, fps=4, max_candidates=10_000, cancel=stop)
    assert time.time() - t0 < 5


def test_pipeline_run_stops_at_next_stage(clip, tmp_path):
    stop = threading.Event()
    seen = []

    def progress(p):
        seen.append(p["stage"])
        if p["stage"] == "seeing":
            stop.set()
    with pytest.raises(Cancelled):
        pipeline.run(clip, STORY, settings=Settings(offline=True, cache_dir=tmp_path), max_frames=12,
                     on_progress=progress, cancel=stop)
    assert "judging" not in seen


# --- store ---------------------------------------------------------------------------

def blocking_runner(started: threading.Event):
    def runner(video, story, *, cancel, **kw):
        started.set()
        while True:
            if cancel.is_set():
                raise Cancelled()
            time.sleep(0.02)
    return runner


def test_cancel_running_and_queued_claims(clip, tmp_path):
    started = threading.Event()
    store = RunStore(tmp_path / "claims", Settings(offline=True), workers=1, runner=blocking_runner(started))
    first = store.create(clip, STORY)
    queued = store.create(clip, STORY)                          # one worker: this one waits in the queue
    assert started.wait(5)
    assert store.meta(queued)["status"] == "queued"
    assert store.cancel(queued) and store.cancel(first)
    assert wait_for(lambda: store.meta(first)["status"] == "cancelled")
    assert wait_for(lambda: store.meta(queued)["status"] == "cancelled")
    assert store.cancel(first) is False                          # nothing left to cancel
    assert store.meta(first)["cancelling"] is False
    store.shutdown()


def test_restart_marks_interrupted_claims(clip, tmp_path):
    root = tmp_path / "claims"
    store = RunStore(root, Settings(offline=True, cache_dir=tmp_path / "c"))
    done = store.create(clip, STORY, wait=True)
    stuck = store.create(clip, STORY, wait=True)
    store.shutdown()
    # Simulate a server that died mid-run: one first run, one re-judge with a half-added clip.
    meta = json.loads((root / stuck / "meta.json").read_text())
    (root / stuck / "report.json").unlink()
    (root / stuck / "meta.json").write_text(json.dumps({**meta, "status": "running"}))
    meta = json.loads((root / done / "meta.json").read_text())
    (root / done / "meta.json").write_text(json.dumps({**meta, "status": "running", "stage_kind": "rejudge"}))
    (root / done / "e1.mp4").write_bytes(b"x")

    fresh = RunStore(root, Settings(offline=True, cache_dir=tmp_path / "c"))
    assert fresh.meta(stuck)["status"] == "error" and "Interrupted" in fresh.meta(stuck)["error"]
    assert fresh.meta(done)["status"] == "done" and "interrupted" in fresh.meta(done)["error"]
    assert not (root / done / "e1.mp4").exists() and (root / done / "video.mp4").exists()
    assert fresh.cancel(stuck) is False
    fresh.shutdown()


def test_delete_claim(clip, tmp_path):
    started = threading.Event()
    store = RunStore(tmp_path / "claims", Settings(offline=True), runner=blocking_runner(started))
    rid = store.create(clip, STORY)
    assert started.wait(5)
    with pytest.raises(ValueError, match="cancel"):
        store.delete(rid)
    store.cancel(rid)
    assert wait_for(lambda: store.meta(rid)["status"] == "cancelled")
    store.delete(rid)
    assert not (tmp_path / "claims" / rid).exists() and store.list() == []
    with pytest.raises(KeyError):
        store.delete(rid)
    store.shutdown()


def test_cancel_rejudge_keeps_report(clip, tmp_path):
    store = RunStore(tmp_path / "claims", Settings(offline=True, cache_dir=tmp_path / "c"))
    rid = store.create(clip, STORY, wait=True)
    before = store.report(rid)
    started = threading.Event()

    def slow_rejudge(report, video, note, *, cancel, **kw):
        started.set()
        while not cancel.is_set():
            time.sleep(0.02)
        raise Cancelled()
    store._rejudger = slow_rejudge
    store.add_evidence(rid, clip)
    assert started.wait(5) and store.cancel(rid)
    assert wait_for(lambda: store.meta(rid)["status"] == "done")
    assert "cancelled" in store.meta(rid)["error"] and store.report(rid) == before
    with pytest.raises(KeyError):
        store.video_path(rid, "e1")
    store.shutdown()


def test_cancel_and_delete_api(clip, tmp_path):
    started = threading.Event()
    store = RunStore(tmp_path / "claims", Settings(offline=True), runner=blocking_runner(started))
    with TestClient(create_app(store=store)) as http:
        rid = store.create(clip, STORY)
        assert started.wait(5)
        assert http.delete(f"/api/claims/{rid}").status_code == 409
        assert http.post(f"/api/claims/{rid}/cancel").status_code == 202
        assert wait_for(lambda: http.get(f"/api/claims/{rid}").json()["meta"]["status"] == "cancelled")
        assert http.post(f"/api/claims/{rid}/cancel").status_code == 409
        assert http.delete(f"/api/claims/{rid}").status_code == 204
        assert http.get(f"/api/claims/{rid}").status_code == 404
        assert http.delete("/api/claims/000000000000").status_code == 404
        assert http.post("/api/claims/000000000000/cancel").status_code == 404


# --- trim -----------------------------------------------------------------------------

def test_trim_command(long_clip, tmp_path, capsys):
    out = tmp_path / "cut.mp4"
    assert cli.main(["trim", str(long_clip), "--start", "1:05", "--length", "12", "--out", str(out)]) == 0
    assert video_duration(out) == pytest.approx(12, abs=0.3)
    assert cli._seconds("1:05") == 65 and cli._seconds("0:01:05.5") == 65.5 and cli._seconds("20") == 20


def test_trim_reports_a_missing_video(tmp_path, capsys):
    assert cli.main(["trim", str(tmp_path / "nope.webm"), "--start", "0", "--length", "5"]) == 1
    assert "no video at" in capsys.readouterr().err
