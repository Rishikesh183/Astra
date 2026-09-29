"""Phase 1 smoke tests. No network, no API keys: live calls go through a mock transport."""

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from physics_witness import cli, pipeline
from physics_witness.cache import ResponseCache
from physics_witness.claims import heuristic_claims
from physics_witness.clients.tavily import TavilyClient
from physics_witness.clients.token_factory import TokenFactoryClient, TokenFactoryError, UsageLedger
from physics_witness.config import Settings
from physics_witness.crossexam import parse_verdicts
from physics_witness.frames import sample_frames, select_indices, video_duration, windows
from physics_witness.jsonutil import extract_json
from physics_witness.models import discover

STORY = ("I was stopped at the red light. The other car hit my rear bumper. "
         "It was raining heavily. The other driver drove off to the left.")


# --- frames -----------------------------------------------------------------

def test_sample_frames_have_timestamps_and_order(clip):
    assert 9.5 <= video_duration(clip) <= 10.5
    frames = sample_frames(clip, max_frames=8, fps=4)
    assert len(frames) == 8
    times = [f.t for f in frames]
    assert times == sorted(times) and times[0] == 0.0 and times[-1] >= 9.0
    # Timestamp band adds height below the original 360p-derived frame.
    w, h = frames[0].image.size
    assert w == 768 and h > 432
    decoded = Image.open(io.BytesIO(base64.b64decode(frames[0].to_b64_jpeg())))
    assert decoded.format == "JPEG"


def test_frame_times_are_real_source_frame_times(clip):
    # The sample clip is 25 fps, so every true frame time is a multiple of 0.04 s.
    for f in sample_frames(clip, max_frames=16, fps=4):
        assert abs(f.t / 0.04 - round(f.t / 0.04)) < 1e-3, f.t


def test_select_indices_keeps_ends_and_budget():
    scores = [0, 5, 1, 9, 0, 0, 7, 0, 0, 2]
    idx = select_indices(scores, max_frames=4)
    assert idx[0] == 0 and idx[-1] == 9 and len(idx) == 4 and 3 in idx
    assert len(select_indices([0.0] * 20, max_frames=6)) == 6
    assert select_indices([1, 2], max_frames=8) == [0, 1]


def test_windows_respect_limit_and_overlap():
    items = list(range(16))
    ws = windows(items, size=6, overlap=1)
    assert all(len(w) <= 6 for w in ws)
    assert ws[0][-1] == ws[1][0]            # overlap of one frame
    assert ws[-1][-1] == 15                 # nothing dropped
    assert windows(items[:4], size=6) == [items[:4]]


# --- parsing ----------------------------------------------------------------

def test_extract_json_handles_reasoning_wrappers():
    assert extract_json('<think>hmm {"no": 1}</think>\n```json\n{"a": [1]}\n```') == {"a": [1]}
    assert extract_json("<answer>[1, 2]</answer>") == [1, 2]
    assert extract_json('Sure! {"events": []} done') == {"events": []}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_verdicts_normalises_and_abstains_on_missing():
    claims = heuristic_claims(STORY)
    text = json.dumps({"verdicts": [
        {"claim_id": "c1", "verdict": "Supported", "evidence_times": [1.0, "2.5", 99, "x"], "reasoning": "r"},
        {"claim_id": "c2", "verdict": "cannot tell", "needed_evidence": "rear camera"},
        {"claim_id": "c3", "verdict": "banana"},
    ]})
    v = {x.claim_id: x for x in parse_verdicts(text, claims, duration=10.0)}
    assert v["c1"].verdict == "supported" and v["c1"].evidence_times == [1.0, 2.5]
    assert v["c2"].verdict == "cant_tell" and v["c2"].needed_evidence == "rear camera"
    assert v["c3"].verdict == "cant_tell"
    assert v["c4"].verdict == "cant_tell"   # model skipped it
    assert all(x.verdict == "cant_tell" for x in parse_verdicts("garbage", claims, 10.0))


def test_model_discovery():
    listing = ["meta-llama/Llama-3.3-70B", "nvidia/Cosmos-Reason3-Reasoner", "nvidia/Nemotron-3-Ultra",
               "nvidia/Nemotron-3-Nano-30B"]
    assert discover(listing) == {"cosmos": "nvidia/Cosmos-Reason3-Reasoner",
                                 "ultra": "nvidia/Nemotron-3-Ultra", "splitter": "nvidia/Nemotron-3-Nano-30B"}


# --- pipeline ---------------------------------------------------------------

def test_offline_pipeline_end_to_end(clip, tmp_path):
    report = pipeline.run(clip, STORY, settings=Settings(offline=True, cache_dir=tmp_path / "c"),
                          max_frames=12, out_dir=tmp_path / "out")
    assert report["mode"] == "offline-stub"
    assert len(report["claims"]) == 4
    assert len(report["windows"]) == 3      # 12 frames, windows of 6 overlapping by 1
    assert report["summary"] == {"supported": 0, "contradicted": 0, "cant_tell": 4}
    assert (tmp_path / "out" / "report.json").is_file()
    assert len(list((tmp_path / "out" / "frames").glob("*.jpg"))) == 12


class FakeTokenFactory:
    """Mock of the OpenAI-compatible Token Factory API that answers per model."""

    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "nvidia/Cosmos3-Reasoner"},
                                                      {"id": "nvidia/Nemotron-3-Ultra"},
                                                      {"id": "nvidia/Nemotron-3-Super-120B"}]})
        body = json.loads(request.content)
        self.requests.append(body)
        model = body["model"]
        if "Cosmos" in model:
            content = body["messages"][0]["content"]
            times = [float(x.split("=")[1].rstrip("s,")) for x in content[0]["text"].split("(")[1].split(")")[0].split()]
            reply = "<think>looking</think>" + json.dumps({"events": [
                {"t_start": times[0], "t_end": times[-1], "what": "grey car rolls forward into white car's rear",
                 "actors": ["grey car", "white car"], "visibility": "clear"}],
                "conditions": {"lighting": "day", "weather": "dry", "road_surface": "dry", "view": "clear"}})
        elif "Super" in model:
            reply = json.dumps({"claims": [{"id": "x", "text": c.text, "kind": "other", "source": c.source}
                                           for c in heuristic_claims(STORY)]})
        else:
            reply = "```json\n" + json.dumps({"verdicts": [
                {"claim_id": "c1", "verdict": "supported", "evidence_times": [0.0], "reasoning": "white car still"},
                {"claim_id": "c2", "verdict": "supported", "evidence_times": [5.0], "reasoning": "contact at rear"},
                {"claim_id": "c3", "verdict": "contradicted", "evidence_times": [2.0], "reasoning": "road is dry"},
                {"claim_id": "c4", "verdict": "cant_tell", "reasoning": "clip ends first",
                 "needed_evidence": "footage from 10 seconds later"},
            ]}) + "\n```"
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}],
                                         "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


def _live_client(tmp_path, handler):
    settings = Settings(nebius_api_key="v1.test", cache_dir=tmp_path / "cache")
    return settings, TokenFactoryClient(settings, ResponseCache(settings.cache_dir), UsageLedger(),
                                        transport=httpx.MockTransport(handler))


def test_live_pipeline_with_mock_api_and_cache(clip, tmp_path):
    fake = FakeTokenFactory()
    settings, client = _live_client(tmp_path, fake)
    report = pipeline.run(clip, STORY, settings=settings, client=client, max_frames=8)

    assert report["mode"] == "live"
    assert report["models"] == {"cosmos": "nvidia/Cosmos3-Reasoner", "ultra": "nvidia/Nemotron-3-Ultra",
                                "splitter": "nvidia/Nemotron-3-Super-120B"}
    assert report["summary"] == {"supported": 2, "contradicted": 1, "cant_tell": 1}
    cosmos_calls = [r for r in fake.requests if "Cosmos" in r["model"]]
    assert len(cosmos_calls) == 2           # 8 frames -> windows of 6 overlapping by 1
    for call in cosmos_calls:
        images = [p for p in call["messages"][0]["content"] if p["type"] == "image_url"]
        assert 1 <= len(images) <= 8
        assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    ultra = [r for r in fake.requests if "Ultra" in r["model"]][0]
    assert "grey car rolls forward" in ultra["messages"][1]["content"]
    assert report["usage"]["by_model"]["nvidia/Nemotron-3-Ultra"]["total_tokens"] == 120

    # Second run is served entirely from the response cache.
    n = len(fake.requests)
    settings2, client2 = _live_client(tmp_path, fake)
    again = pipeline.run(clip, STORY, settings=settings2, client=client2, max_frames=8)
    assert len(fake.requests) == n
    assert again["summary"] == report["summary"] and again["usage"]["cache_hits"] == again["usage"]["calls"]


def test_credit_exhaustion_error_is_explained(tmp_path):
    _, client = _live_client(tmp_path, lambda r: httpx.Response(402, text="insufficient credit"))
    with pytest.raises(TokenFactoryError, match="credit"):
        client.chat("m", [{"role": "user", "content": "hi"}])


def test_tavily_request_shape(tmp_path):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"answer": "light rain", "results": [{"title": "t"}]})

    t = TavilyClient(Settings(tavily_api_key="tvly-x", cache_dir=tmp_path), transport=httpx.MockTransport(handler))
    out = t.weather("Pune, India", "2026-08-02")
    assert out["answer"] == "light rain"
    assert seen["auth"] == "Bearer tvly-x" and "Pune" in seen["body"]["query"]
    assert TavilyClient(Settings(cache_dir=tmp_path)).search("x")["offline"] is True


# --- CLI --------------------------------------------------------------------

def test_cli_run_and_frames_and_checks_offline(clip, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PW_OFFLINE", "1")
    assert cli.main(["frames", str(clip), "--out", str(tmp_path / "f"), "--max-frames", "6"]) == 0
    assert len(list((tmp_path / "f").glob("*.jpg"))) == 6
    assert cli.main(["run", str(clip), STORY, "--out", str(tmp_path / "r")]) == 0
    out = capsys.readouterr().out
    assert "CAN'T TELL" in out and "mode=offline-stub" in out
    assert cli.main(["checks", "--clip", str(clip)]) == 0
    out = capsys.readouterr().out
    assert "[PASS  ] 0 (setup)" in out and "[SKIP  ] 1" in out and "[MANUAL] 5" in out
