"""Phase 3: act loop (ask -> extra footage -> re-judge), model routing, cost meter."""

import json
import subprocess
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from physics_witness import act, costs, pipeline, routing
from physics_witness.cache import ResponseCache
from physics_witness.clients.token_factory import ChatResult, TokenFactoryClient, UsageLedger
from physics_witness.config import Settings
from physics_witness.frames import ffmpeg_exe, sample_frames
from physics_witness.server import create_app
from physics_witness.store import RunStore
from test_smoke import STORY, FakeTokenFactory

PRICES = {"cosmos": {"input": 1.0, "output": 2.0}, "nemotron-3-ultra": {"input": 3.0, "output": 6.0},
          "nemotron-3-super": {"input": 0.5, "output": 1.0}, "_note": "ignored"}


class RejudgeFake(FakeTokenFactory):
    """Once extra footage (source e1) is in the prompt, the rear-camera clip settles c4."""

    def __call__(self, request):
        body = json.loads(request.content) if request.content else {}
        if "Ultra" in body.get("model", "") and '"source": "e1"' in body["messages"][1]["content"]:
            self.requests.append(body)
            reply = json.dumps({"verdicts": [{"claim_id": "c4", "verdict": "supported",
                                              "evidence": [{"source": "e1", "t": 1.0}, {"source": "main", "t": 9.0}],
                                              "reasoning": "rear camera shows the car leaving to the left"}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}],
                                             "usage": {"prompt_tokens": 1000, "completion_tokens": 50}})
        return super().__call__(request)


def live(tmp_path, fake=None, prices=True, **extra):
    pf = tmp_path / "pricing.json"
    if prices:
        pf.write_text(json.dumps(PRICES))
    settings = Settings(nebius_api_key="v1.test", cache_dir=tmp_path / "cache", prices_file=pf, **extra)
    fake = fake or RejudgeFake()
    client = TokenFactoryClient(settings, ResponseCache(settings.cache_dir), UsageLedger(),
                                transport=httpx.MockTransport(fake))
    return settings, client, fake


@pytest.fixture(scope="module")
def static_clip(tmp_path_factory):
    out = tmp_path_factory.mktemp("static") / "static.mp4"
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x180:d=6:r=25",
                    "-pix_fmt", "yuv420p", str(out)], check=True)
    return out


# --- cost meter -------------------------------------------------------------

def test_prices_and_estimate(tmp_path):
    pf = tmp_path / "p.json"
    pf.write_text(json.dumps({**PRICES, "nano": {"input": None, "output": None}}))
    prices = costs.load_prices(pf)
    assert set(prices) == {"cosmos", "nemotron-3-ultra", "nemotron-3-super"}      # nulls and _note skipped
    assert costs.price_for("nvidia/Nemotron-3-Ultra", prices)["input"] == 3.0
    assert costs.price_for("nvidia/Nemotron-3-Nano", prices) is None
    assert costs.load_prices(tmp_path / "missing.json") == {}

    ledger = UsageLedger()
    ledger.record(ChatResult("x", "nvidia/Nemotron-3-Ultra", {"prompt_tokens": 1_000_000, "completion_tokens": 100_000}), "judge")
    ledger.record(ChatResult("x", "nvidia/Nemotron-3-Ultra", {"prompt_tokens": 500_000, "completion_tokens": 0}, cached=True), "judge")
    ledger.record(ChatResult("x", "nvidia/Cosmos3", {"prompt_tokens": 2000, "completion_tokens": 1000}), "see")
    est = costs.estimate(ledger.rows(), prices)
    assert est["total_usd"] == pytest.approx(3.0 + 0.6 + 0.002 + 0.002)
    assert est["saved_usd"] == pytest.approx(1.5) and est["saved_tokens"] == 500_000
    assert est["by_task"]["judge"]["calls"] == 2 and est["by_task"]["judge"]["cached_calls"] == 1
    assert est["unpriced_models"] == []

    ledger.record(ChatResult("x", "nvidia/Nemotron-3-Nano", {"prompt_tokens": 10, "completion_tokens": 5}), "split")
    est = costs.estimate(ledger.rows(), prices)
    assert est["total_usd"] is None and est["unpriced_models"] == ["nvidia/Nemotron-3-Nano"]
    assert est["by_task"]["split"]["usd"] is None and est["known_usd"] == pytest.approx(3.604)

    merged = costs.merge_rows(ledger.rows(), ledger.rows())
    assert sum(r["calls"] for r in merged) == 8 and len(merged) == 3


# --- routing ----------------------------------------------------------------

def test_route_policies_and_fallbacks():
    names = {"cosmos": "C", "ultra": "U", "splitter": "N", "super": "S"}
    assert routing.route(names, "quality") == {"split": "N", "see": "C", "judge": "U", "ask": "N"}
    assert routing.route(names, "budget")["judge"] == "S"
    assert routing.route({"cosmos": "C", "ultra": "U"}, "budget") == {"split": "U", "see": "C", "judge": "U", "ask": "U"}
    assert routing.route(names, "nonsense") == routing.route(names, "quality")


def test_frame_filter_drops_static_frames(clip, static_clip):
    kept, dropped = routing.drop_near_duplicates(sample_frames(static_clip, max_frames=12))
    assert len(kept) == 2 and len(dropped) >= 8                  # only first and last survive
    moving = sample_frames(clip, max_frames=12)
    kept, dropped = routing.drop_near_duplicates(moving)
    assert len(kept) >= 10 and kept[0] is moving[0] and kept[-1] is moving[-1]


# --- act: evidence request --------------------------------------------------

def test_compose_request_fallback_and_nothing_pending(tmp_path):
    client = TokenFactoryClient(Settings(offline=True, cache_dir=tmp_path))
    verdicts = [{"claim_id": "c1", "claim": "A", "verdict": "supported"},
                {"claim_id": "c2", "claim": "The car went left", "verdict": "cant_tell", "needed_evidence": "rear camera"},
                {"claim_id": "c3", "claim": "It rained", "verdict": "cant_tell", "needed_evidence": ""}]
    req = act.compose_request(client, "", verdicts)
    assert [i["claim_id"] for i in req["items"]] == ["c2", "c3"]
    assert req["items"][0]["ask"] == "rear camera" and req["items"][1]["ask"] == act.DEFAULT_ASK
    assert "The car went left" in req["message"]
    assert act.compose_request(client, "", verdicts[:1]) is None


# --- pipeline: run + live progress + cost -----------------------------------

def test_run_reports_routing_request_cost_and_progress(clip, tmp_path):
    settings, client, fake = live(tmp_path)
    events = []
    report = pipeline.run(clip, STORY, settings=settings, client=client, max_frames=8, on_progress=events.append)

    assert report["routing"]["policy"] == "quality"
    assert report["routing"]["routes"] == {"split": "nvidia/Nemotron-3-Super-120B", "see": "nvidia/Cosmos3-Reasoner",
                                           "judge": "nvidia/Nemotron-3-Ultra", "ask": "nvidia/Nemotron-3-Super-120B"}
    assert [i["claim_id"] for i in report["request"]["items"]] == ["c4"]
    assert report["request"]["items"][0]["ask"] == "footage from 10 seconds later"
    assert set(report["cost"]["by_task"]) == {"see", "split", "judge", "ask"}
    assert report["cost"]["total_usd"] > 0 and report["cost"]["unpriced_models"] == []
    assert report["rounds"][0]["kind"] == "initial" and report["sources"][0]["id"] == "main"

    stages = [e["stage"] for e in events]
    for s in ("sampling", "seeing", "splitting", "judging", "asking", "done"):
        assert s in stages
    assert stages.index("seeing") < stages.index("judging") < stages.index("done")
    tokens = [e["cost"]["tokens"] for e in events]
    assert tokens == sorted(tokens) and tokens[-1] == report["cost"]["tokens"]      # meter only goes up


def test_budget_policy_sends_judging_to_super(clip, tmp_path):
    settings, client, fake = live(tmp_path, policy="budget")
    report = pipeline.run(clip, STORY, settings=settings, client=client, max_frames=6)
    assert report["routing"]["routes"]["judge"] == "nvidia/Nemotron-3-Super-120B"
    assert not any("Ultra" in r["model"] for r in fake.requests)


# --- act loop: re-judge -----------------------------------------------------

def test_rejudge_settles_cant_tell_with_extra_footage(clip, tmp_path):
    settings, client, fake = live(tmp_path)
    report = pipeline.run(clip, STORY, settings=settings, client=client, max_frames=8, out_dir=tmp_path / "run")
    before = {v["claim_id"]: v for v in report["verdicts"]}
    first_cost = report["cost"]["tokens"]

    settings2, client2, _ = live(tmp_path, fake)
    out = pipeline.rejudge(json.loads(json.dumps(report)), clip, "This is from my rear dashcam.",
                           settings=settings2, client=client2, max_frames=6, out_dir=tmp_path / "run")

    rejudge_call = [r for r in fake.requests if "Ultra" in r["model"]][-1]["messages"][1]["content"]
    assert "rear dashcam" in rejudge_call and '"id": "c4"' in rejudge_call and '"id": "c1"' not in rejudge_call
    assert [s["id"] for s in out["sources"]] == ["main", "e1"]
    after = {v["claim_id"]: v for v in out["verdicts"]}
    assert after["c4"]["verdict"] == "supported" and after["c4"]["round"] == 1
    assert after["c4"]["jump"] == {"source": "main", "t": 9.0}
    e1_frames = [f for f in after["c4"]["evidence_frames"] if f["source"] == "e1"]
    assert e1_frames and (tmp_path / "run" / "frames" / e1_frames[0]["file"]).is_file()
    for cid in ("c1", "c2", "c3"):
        assert after[cid] == before[cid]                               # settled verdicts untouched
    last = out["rounds"][-1]
    assert last["changes"] == [{"claim_id": "c4", "claim": before["c4"]["claim"], "before": "cant_tell", "after": "supported"}]
    assert last["asked"][0]["claim_id"] == "c4" and last["rejudged"] == ["c4"]
    assert out["request"] is None and out["summary"] == {"supported": 3, "contradicted": 1, "cant_tell": 0}
    assert out["cost"]["tokens"] > first_cost                         # totals span both rounds
    assert json.loads((tmp_path / "run" / "report.json").read_text())["summary"] == out["summary"]


def test_rejudge_upgrades_phase_two_report(clip, tmp_path):
    settings, client, fake = live(tmp_path)
    report = pipeline.run(clip, STORY, settings=settings, client=client, max_frames=6)
    for key in ("sources", "rounds"):
        report.pop(key)
    settings2, client2, _ = live(tmp_path, fake)
    out = pipeline.rejudge(report, clip, settings=settings2, client=client2, max_frames=6)
    assert [s["id"] for s in out["sources"]] == ["main", "e1"] and len(out["rounds"]) == 2


# --- store + web API --------------------------------------------------------

def test_evidence_upload_through_api(clip, tmp_path):
    store = RunStore(tmp_path / "claims", Settings(offline=True, cache_dir=tmp_path / "c"))
    with TestClient(create_app(store=store)) as http:
        rid = store.create(clip, STORY, wait=True)
        meta = store.meta(rid)
        assert meta["progress"]["stage"] == "done" and meta["rounds"] == 1 and "cost" in meta
        assert store.report(rid)["request"]["items"]           # offline: everything is Can't tell -> asks

        with open(clip, "rb") as fh:
            r = http.post(f"/api/claims/{rid}/evidence", files={"video": ("rear.mp4", fh, "video/mp4")},
                          data={"note": "rear camera"})
        assert r.status_code == 202 and r.json()["source"] == "e1"
        for _ in range(100):
            body = http.get(f"/api/claims/{rid}").json()
            if body["meta"]["status"] == "done":
                break
            time.sleep(0.1)
        assert body["meta"]["status"] == "done" and body["meta"]["rounds"] == 2 and not body["meta"]["error"]
        assert [s["id"] for s in body["report"]["sources"]] == ["main", "e1"]
        assert body["report"]["rounds"][-1]["note"] == "rear camera"

        assert http.get(f"/api/claims/{rid}/video?source=e1", headers={"Range": "bytes=0-9"}).status_code == 206
        assert http.get(f"/api/claims/{rid}/video?source=e7").status_code == 404
        assert http.get(f"/api/claims/{rid}/video?source=../video").status_code == 404
        e1_frame = body["report"]["sources"][1]["frames"][0]["file"]
        assert e1_frame.startswith("e1_") and http.get(f"/api/claims/{rid}/frames/{e1_frame}").status_code == 200
        bad = http.post(f"/api/claims/{rid}/evidence", files={"video": ("x.txt", b"x", "text/plain")})
        assert bad.status_code == 400
        assert http.post("/api/claims/000000000000/evidence", files={"video": ("x.mp4", b"x", "video/mp4")}).status_code == 404


def test_failed_rejudge_keeps_claim_usable(clip, tmp_path):
    store = RunStore(tmp_path / "claims", Settings(offline=True, cache_dir=tmp_path / "c"))
    rid = store.create(clip, STORY, wait=True)

    def boom(*a, **k):
        raise RuntimeError("Token Factory 402")
    store._rejudger = boom
    store.add_evidence(rid, clip, wait=True)
    meta = store.meta(rid)
    assert meta["status"] == "done" and "402" in meta["error"]
    assert len(store.report(rid)["sources"]) == 1
    with pytest.raises(KeyError):
        store.video_path(rid, "e1")                                  # failed clip removed
    store.shutdown()
