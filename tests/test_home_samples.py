"""Home page data (claim cards), /claims/<id> routing, and sample claims."""

import json
import shutil
import time

from fastapi.testclient import TestClient

from physics_witness import cli
from physics_witness.config import Settings
from physics_witness.samples import check_samples, load_samples
from physics_witness.server import create_app
from physics_witness.store import RunStore

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def write_samples(tmp_path, clip):
    shutil.copy(clip, tmp_path / "train.mp4")
    data = {"samples": [
        {"id": "crossing-true", "title": "Level crossing", "label": "real", "video": "train.mp4",
         "story": "A train hit the car.", "license": "self-filmed"},
        {"id": "crossing-liar", "title": "Wrong side", "label": "liar", "video": "train.mp4",
         "story": "A train came from the left. It hit the car.", "lie_sentence": "A train came from the left.", "license": "self-filmed"},
        {"id": "missing-clip", "title": "Not added yet", "label": "real", "video": "videos/none.mp4", "story": "x."},
        {"id": "Bad Id", "label": "real", "video": "train.mp4", "story": "x"},          # skipped: id
        {"id": "no-label", "video": "train.mp4", "story": "x"},                          # skipped: label
        {"id": "crossing-true", "label": "real", "video": "train.mp4", "story": "dup"},   # skipped: duplicate
    ]}
    path = tmp_path / "samples.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def wait_done(http, rid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        meta = http.get(f"/api/claims/{rid}").json()["meta"]
        if meta["status"] not in ("queued", "running"):
            return meta
        time.sleep(0.1)
    raise AssertionError("claim did not finish")


def test_samples_file_loading_and_checks(clip, tmp_path):
    path = write_samples(tmp_path, clip)
    samples = {s.id: s for s in load_samples(path)}
    assert list(samples) == ["crossing-true", "crossing-liar", "missing-clip"]
    assert samples["crossing-true"].available and not samples["missing-clip"].available
    assert "video" not in samples["crossing-liar"].public() and samples["crossing-liar"].public()["available"] is True
    problems = "\n".join(check_samples(path))
    assert "[missing-clip] video not found" in problems and "[missing-clip] no license" in problems
    assert "[Bad Id] skipped" in problems and "[crossing-liar]" not in problems
    assert load_samples(tmp_path / "nope.json") == []


def test_repo_samples_file_is_valid():
    samples = {s.id: s for s in load_samples(ROOT / "demo" / "samples.json")}
    assert {"crossing-true", "crossing-liar", "intersection-true"} <= set(samples)
    assert sum(s.label == "liar" for s in samples.values()) >= 1 and sum(s.label == "real" for s in samples.values()) >= 2
    for s in samples.values():
        if s.label == "liar":
            assert s.lie_sentence and s.lie_sentence in s.story
    assert samples["crossing-true"].available                     # the train clip is in the repo


def test_sample_api_and_claim_routing(clip, tmp_path):
    settings = Settings(offline=True, cache_dir=tmp_path / "c", samples_file=write_samples(tmp_path, clip))
    store = RunStore(tmp_path / "claims", settings)
    with TestClient(create_app(store=store)) as http:
        listed = http.get("/api/samples").json()
        assert [s["id"] for s in listed] == ["crossing-true", "crossing-liar", "missing-clip"]
        assert [s["available"] for s in listed] == [True, True, False]
        assert http.get("/api/samples/crossing-true/video", headers={"Range": "bytes=0-9"}).status_code == 206
        assert http.get("/api/samples/missing-clip/video").status_code == 404
        assert http.get("/api/samples/../etc/video").status_code == 404
        assert http.post("/api/samples/missing-clip/try").status_code == 404
        assert http.post("/api/samples/nope/try").status_code == 404

        r = http.post("/api/samples/crossing-liar/try")
        assert r.status_code == 202
        rid = r.json()["id"]
        meta = wait_done(http, rid)
        assert meta["sample"] == {"id": "crossing-liar", "label": "liar", "lie_sentence": "A train came from the left."}
        assert meta["title"] == "Wrong side" and meta["status"] == "done"
        # card fields for the home page
        assert meta["thumb"].startswith("frame_") and meta["duration_s"] > 9 and meta["claims"] == 2
        assert meta["story"].startswith("A train came from the left") and meta["joined"] is False
        assert http.get(f"/api/claims/{rid}/frames/{meta['thumb']}").status_code == 200

        # /claims/<id> serves the app so links can be bookmarked; unknown ids 404
        page = http.get(f"/claims/{rid}")
        assert page.status_code == 200 and "Physics Witness" in page.text
        missing = http.get("/claims/000000000000")
        assert missing.status_code == 404 and "Physics Witness" in missing.text      # the app, which says "not found"
        assert http.get("/claims/not-an-id").status_code == 404
        assert http.get("/").status_code == 200


def test_older_claims_get_card_fields_when_listed(clip, tmp_path):
    store = RunStore(tmp_path / "claims", Settings(offline=True, cache_dir=tmp_path / "c"))
    rid = store.create(clip, "A car passed.", wait=True)
    meta_path = tmp_path / "claims" / rid / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for k in ("thumb", "duration_s", "claims", "story", "tokens", "usd", "joined", "extra_clips"):
        meta.pop(k)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")          # as written by an older version
    listed = store.list()[0]
    assert listed["thumb"] and listed["claims"] == 1 and listed["duration_s"] > 9
    assert "thumb" in json.loads(meta_path.read_text(encoding="utf-8"))   # saved, not recomputed every time
    store.shutdown()


def test_samples_cli(clip, tmp_path, capsys):
    path = write_samples(tmp_path, clip)
    assert cli.main(["samples", "--file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "ok  crossing-true" in out and "--  missing-clip" in out and "video not found" in out
