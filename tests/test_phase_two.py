"""Phase 2: evidence links, Tavily context, run store, and the web API."""

import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from physics_witness import context, evidence, pipeline
from physics_witness.cache import ResponseCache
from physics_witness.clients.tavily import TavilyClient
from physics_witness.clients.token_factory import TokenFactoryClient, UsageLedger
from physics_witness.config import Settings
from physics_witness.events import Event, WindowReading
from physics_witness.frames import sample_frames
from physics_witness.server import create_app
from physics_witness.store import RunStore
from test_smoke import STORY, FakeTokenFactory


def fake_tavily(calls):
    def handler(request):
        body = json.loads(request.content)
        calls.append(body["query"])
        answer = "Heavy rain all afternoon" if "weather" in body["query"] else "Four-lane junction with signals"
        return httpx.Response(200, json={"query": body["query"], "answer": answer, "results": [
            {"title": "Records", "url": "https://example.org/r", "content": "x" * 1000}]})
    return handler


# --- evidence ---------------------------------------------------------------

def test_link_picks_nearest_frames_and_overlapping_observations(clip):
    frames = [{"t": f.t, "file": f.filename} for f in sample_frames(clip, max_frames=6)]
    windows = [WindowReading(0, 0, 10, [f["t"] for f in frames], [
        Event(1.0, 3.0, "car A moves left", visibility="clear"),
        Event(6.0, 7.0, "car B brakes", visibility="partial"),
    ], {}).to_dict()]
    extra = {"frames": [{"t": 0.5, "file": "e1_frame_0000.50s.jpg"}],
             "windows": [{"window": 0, "events": [{"t_start": 0, "t_end": 1, "what": "rear camera: impact"}]}]}
    sources = {"main": {"frames": frames, "windows": windows}, "e1": extra}
    out = evidence.link({"claim_id": "c1", "evidence": [
        {"source": "e1", "t": 0.6}, {"source": "main", "t": 6.4}, {"source": "main", "t": 2.0},
        {"source": "e9", "t": 1.0}]}, sources)
    assert out["jump_to"] == 2.0 and out["jump"] == {"source": "main", "t": 2.0}
    assert [f["source"] for f in out["evidence_frames"]] == ["main", "main", "e1"]
    assert out["evidence_frames"][-1]["file"] == "e1_frame_0000.50s.jpg"
    assert [o["what"] for o in out["observations"]] == ["car A moves left", "car B brakes", "rear camera: impact"]
    only_extra = evidence.link({"claim_id": "c3", "evidence": [{"source": "e1", "t": 0.2}]}, sources)
    assert only_extra["jump_to"] is None and only_extra["jump"]["source"] == "e1"
    empty = evidence.link({"claim_id": "c2", "evidence": []}, sources)
    assert empty["jump_to"] is None and empty["evidence_frames"] == [] and empty["observations"] == []


# --- Tavily context ---------------------------------------------------------

def test_context_weather_and_place(tmp_path):
    calls = []
    t = TavilyClient(Settings(tavily_api_key="tvly-x", cache_dir=tmp_path), transport=httpx.MockTransport(fake_tavily(calls)))
    ctx = context.gather(t, "Pune, India", "2026-08-02")
    assert ctx["weather"]["answer"] == "Heavy rain all afternoon"
    assert ctx["place_facts"]["answer"].startswith("Four-lane")
    assert len(ctx["weather"]["sources"][0]["snippet"]) == context.SNIPPET_CHARS
    assert len(calls) == 2 and "2026-08-02" in calls[0]
    slim = context.for_prompt(ctx)
    assert "url" not in json.dumps(slim) and slim["weather"]["answer"] == "Heavy rain all afternoon"

    assert context.gather(t, "Pune, India", None).get("weather") is None      # no date: no weather lookup
    assert context.gather(t, "", "2026-08-02") is None                          # no place: nothing
    offline = context.gather(TavilyClient(Settings(cache_dir=tmp_path)), "Pune", "2026-08-02")
    assert offline["offline"] is True and context.for_prompt(offline) is None


def test_live_pipeline_uses_tavily_and_links_evidence(clip, tmp_path):
    fake, calls = FakeTokenFactory(), []
    settings = Settings(nebius_api_key="v1.test", tavily_api_key="tvly-x", cache_dir=tmp_path / "cache")
    cache = ResponseCache(settings.cache_dir)
    client = TokenFactoryClient(settings, cache, UsageLedger(), transport=httpx.MockTransport(fake))
    tavily = TavilyClient(settings, cache, transport=httpx.MockTransport(fake_tavily(calls)))
    report = pipeline.run(clip, STORY, settings=settings, client=client, tavily=tavily, max_frames=8,
                          out_dir=tmp_path / "run", place="Pune, India", date="2026-08-02")

    ultra = [r for r in fake.requests if "Ultra" in r["model"]][0]["messages"][1]["content"]
    assert "Heavy rain all afternoon" in ultra and "example.org" not in ultra
    assert report["context"]["weather"]["sources"][0]["url"] == "https://example.org/r"
    assert report["story"] == STORY
    by = {v["claim_id"]: v for v in report["verdicts"]}
    assert by["c2"]["jump_to"] == 5.0 and by["c2"]["evidence_frames"]
    assert by["c2"]["observations"][0]["what"].startswith("grey car")
    for v in report["verdicts"]:
        for f in v["evidence_frames"]:
            assert (tmp_path / "run" / "frames" / f["file"]).is_file()
    assert by["c4"]["needed_evidence"] == "footage from 10 seconds later" and by["c4"]["jump_to"] is None


# --- run store --------------------------------------------------------------

def test_store_runs_and_records_errors(clip, tmp_path):
    store = RunStore(tmp_path / "claims", Settings(offline=True, cache_dir=tmp_path / "c"))
    rid = store.create(clip, STORY, place="Pune", wait=True)
    meta = store.meta(rid)
    assert meta["status"] == "done" and meta["summary"]["cant_tell"] == 4 and meta["title"].startswith("I was stopped")
    assert store.video_path(rid).name == "video.mp4" and clip.is_file()   # source copied, not moved
    assert store.report(rid)["context"] == {"offline": True, "place": "Pune", "date": None}
    assert [m["id"] for m in store.list()] == [rid]
    frame = store.report(rid)["frames"][0]["file"]
    assert store.frame_path(rid, frame).is_file()
    for bad in ("../meta.json", "frame_1.jpg/../../x", "meta.json"):
        with pytest.raises(KeyError):
            store.frame_path(rid, bad)
    for bad in ("../x", "zzzzzzzzzzzz", ""):
        with pytest.raises(KeyError):
            store.dir(bad)
    with pytest.raises(ValueError):
        store.create(clip, "   ")

    def boom(*a, **k):
        raise RuntimeError("Token Factory 402")
    broken = RunStore(tmp_path / "claims2", Settings(offline=True), runner=boom)
    rid2 = broken.create(clip, STORY, wait=True)
    assert broken.meta(rid2)["status"] == "error" and "402" in broken.meta(rid2)["error"]
    store.shutdown(), broken.shutdown()


# --- web API ----------------------------------------------------------------

def test_web_app_upload_poll_and_serve(clip, tmp_path):
    store = RunStore(tmp_path / "claims", Settings(offline=True, cache_dir=tmp_path / "c"))
    with TestClient(create_app(store=store)) as http:
        page = http.get("/")
        assert page.status_code == 200 and "Physics Witness" in page.text
        assert http.get("/api/health").json() == {"ok": True, "nebius": False, "tavily": False}
        assert http.get("/api/claims").json() == []

        with open(clip, "rb") as fh:
            r = http.post("/api/claims", files={"video": ("crash.mp4", fh, "video/mp4")},
                          data={"story": STORY, "place": "Pune", "date": "2026-08-02", "title": "Rear-end"})
        assert r.status_code == 202
        rid = r.json()["id"]
        for _ in range(100):
            body = http.get(f"/api/claims/{rid}").json()
            if body["meta"]["status"] in ("done", "error"):
                break
            time.sleep(0.1)
        assert body["meta"]["status"] == "done", body["meta"]
        assert body["meta"]["title"] == "Rear-end" and len(body["report"]["verdicts"]) == 4

        video = http.get(f"/api/claims/{rid}/video", headers={"Range": "bytes=0-99"})
        assert video.status_code == 206 and len(video.content) == 100   # seeking needs range support
        frame = body["report"]["frames"][0]["file"]
        img = http.get(f"/api/claims/{rid}/frames/{frame}")
        assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"

        assert http.get("/api/claims/000000000000").status_code == 404
        assert http.get(f"/api/claims/{rid}/frames/..%2Fmeta.json").status_code == 404
        bad = http.post("/api/claims", files={"video": ("x.txt", b"hi", "text/plain")}, data={"story": "s"})
        assert bad.status_code == 400
        empty = http.post("/api/claims", files={"video": ("x.mp4", b"hi", "video/mp4")}, data={"story": "  "})
        assert empty.status_code == 400
