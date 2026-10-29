"""Joined clips, camera jolts, frame picking and the judge's evidence rules.

Built from a live failure: a dashcam compilation (Dallas crash, then a
Lansing crash) where the sampler skipped the Dallas impact and the judge used
the Lansing collision to mark a true Dallas story "contradicted"."""

import json
import subprocess

import httpx

from physics_witness import pipeline
from physics_witness.cache import ResponseCache
from physics_witness.claims import heuristic_claims
from physics_witness.clients.token_factory import TokenFactoryClient, UsageLedger
from physics_witness.config import Settings
from physics_witness.crossexam import SYSTEM_PROMPT, build_user_prompt
from physics_witness.events import COSMOS_PROMPT, _marks, read_window
from physics_witness.frames import Frame, ffmpeg_exe, sample_frames, select_indices, video_duration
from physics_witness.pipeline import segments_from_cuts
from PIL import Image

DALLAS = ("I was driving straight through the intersection on a sunny afternoon. A grey car turned left across my lane. "
          "It crossed in front of my car from my left to my right. The front of my car hit the side of the grey car. "
          "The crash knocked my dashcam so it pointed at the sky.")


def frames_at(*times):
    return [Frame(index=i, t=t, image=Image.new("RGB", (8, 8))) for i, t in enumerate(times)]


# --- frame picking ------------------------------------------------------------------------

def test_spike_keeps_the_frames_just_before_it():
    # Steady driving (~5), a jolt at index 14 (camera knocked), a cut at 25.
    scores = [0.0] + [5.0] * 39
    scores[14], scores[25] = 15.0, 21.0
    idx = select_indices(scores, max_frames=16)
    assert len(idx) == 16 and idx[0] == 0 and idx[-1] == 39
    for spike in (14, 25):
        assert {spike, spike - 1, spike - 2} <= set(idx), (spike, idx)
    gaps = [b - a for a, b in zip(idx, idx[1:])]
    assert max(gaps) <= 6                                   # the even spread leaves no long blind stretch


def test_flat_and_tiny_inputs():
    assert select_indices([0.0] * 20, max_frames=6) == sorted(set(select_indices([0.0] * 20, max_frames=6)))
    assert len(select_indices([0.0] * 20, max_frames=6)) == 6
    assert select_indices([1.0, 2.0], max_frames=8) == [0, 1]


def test_joined_clip_sampling_covers_the_join(tmp_path):
    a, b, joined = tmp_path / "a.mp4", tmp_path / "b.mp4", tmp_path / "joined.mp4"
    for out, src in ((a, "testsrc2=duration=5:size=320x180:rate=25"), (b, "smptebars=duration=5:size=320x180:rate=25")):
        subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i", src, "-pix_fmt", "yuv420p", str(out)], check=True)
    (tmp_path / "list.txt").write_text(f"file '{a}'\nfile '{b}'\n", encoding="utf-8")
    subprocess.run([ffmpeg_exe(), "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(tmp_path / "list.txt"),
                    "-c", "copy", str(joined)], check=True)
    assert abs(video_duration(joined) - 10) < 0.3
    times = [f.t for f in sample_frames(joined, max_frames=12)]
    assert any(4.4 <= t < 5.0 for t in times) and any(5.0 <= t <= 5.3 for t in times), times   # both sides of the join


# --- frame reader output -------------------------------------------------------------------

def test_marks_snap_to_frames_and_skip_the_first_frame():
    fr = frames_at(3.47, 4.27, 5.87, 6.13, 6.40, 8.80)
    assert _marks([{"t": 6.3, "what": "cut to Lansing"}], fr) == [{"t": 6.40, "what": "cut to Lansing"}]
    assert _marks([{"t": 3.47, "what": "x"}], fr) == []                  # can't be seen at a window's first frame
    assert _marks([{"t": 30}, {"t": "abc"}, "nonsense", 4.3], fr) == [{"t": 4.27, "what": ""}]
    assert _marks(None, fr) == [] and _marks("x", fr) == []


def test_frame_prompt_asks_for_jolts_and_cuts():
    assert "camera_jolts" in COSMOS_PROMPT and "scene_cuts" in COSMOS_PROMPT and "A jolt is not a cut" in COSMOS_PROMPT
    text = COSMOS_PROMPT.format(n=2, labels="t=0.00s, t=1.00s")
    assert text.split("(")[1].split(")")[0] == "t=0.00s, t=1.00s"        # the mock API parses this


def test_segments_from_cuts():
    assert segments_from_cuts([], 15.33) == [{"index": 0, "t_start": 0.0, "t_end": 15.33, "cut_note": ""}]
    segs = segments_from_cuts([{"t": 6.4, "what": "cut to Lansing"}, {"t": 6.9, "what": "dup"}, {"t": 0.3, "what": "edge"}], 15.33)
    assert [(g["t_start"], g["t_end"]) for g in segs] == [(0.0, 6.4), (6.4, 15.33)]
    assert segs[1]["cut_note"] == "cut to Lansing"


# --- judge input + rules ---------------------------------------------------------------------

def test_judge_rules_cover_absence_segments_jolts_and_timing():
    for needle in ("Not seeing something is not evidence", "never use an event from another segment",
                   "camera_jolts", "Cite the frames at the moment the claim describes"):
        assert needle in SYSTEM_PROMPT


def test_single_segment_prompt_stays_small():
    src = [{"id": "main", "label": "Original video", "duration_s": 10.0,
            "segments": [{"index": 0, "t_start": 0, "t_end": 10.0}], "camera_jolts": [],
            "windows": [{"t_start": 0, "t_end": 5, "events": [{"t_start": 1, "t_end": 2, "what": "a car"}], "conditions": {}}]}]
    prompt = build_user_prompt(heuristic_claims("A car passed."), src)
    assert '"segment"' not in prompt and "camera_jolts" not in prompt


# --- the live failure, replayed with the frame reader's real observations ----------------------

REAL_WINDOWS = [   # what Nemotron-3.5-Super-VL reported for the merged clip (now with the new structured fields)
    {"events": [{"t_start": 0.0, "t_end": 3.47, "what": "The camera vehicle travels down a multi-lane road in Dallas, Texas. "
                 "Suddenly, the camera view pitches upward sharply, showing only the sky, suggesting a sudden jerk or impact."}],
     "camera_jolts": [{"t": 3.47, "what": "view pitches up to the sky"}], "scene_cuts": [{"t": 6.4, "what": "cut to Lansing, Michigan"}]},
    {"events": [{"t_start": 13.07, "t_end": 13.6, "what": "The grey sedan enters the intersection and collides with the white sedan."}],
     "camera_jolts": [], "scene_cuts": []},
]


class ReplayAPI:
    """Frame reader replays REAL_WINDOWS (one per call); the judge's request is captured."""

    def __init__(self, jolt_t=3.47, cut_t=6.4, events=None):
        self.calls, self.judge_prompts = 0, []
        self.jolt_t, self.cut_t = jolt_t, cut_t
        self.events = events if events is not None else [e for w in REAL_WINDOWS for e in w["events"]]

    def __call__(self, request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "nvidia/Nemotron-3.5-Super-VL"},
                                                      {"id": "nvidia/Nemotron-3-Ultra-550b-a55b"},
                                                      {"id": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"}]})
        body = json.loads(request.content)
        model = body["model"]
        if "VL" in model:
            # Like the real reader: report the jolt (~3.47 s) and the cut (~6.4 s) only in a
            # window whose frames include them, and the events that fall inside the window.
            labels = body["messages"][0]["content"][0]["text"].split("(")[1].split(")")[0]
            times = [float(x.split("=")[1].rstrip("s,")) for x in labels.split()]
            lo, hi = times[0], times[-1]

            def at(target):   # first frame at/after target, if this window shows the change (not on its first frame)
                after = [ft for ft in times[1:] if ft >= target - 0.05]
                return [round(after[0], 2)] if after and after[0] - target < 1.0 and times[times.index(after[0]) - 1] < target else []
            reply = {"events": [e for e in self.events if e["t_start"] <= hi and e["t_end"] >= lo],
                     "camera_jolts": [{"t": t, "what": "view pitches up to the sky"} for t in at(self.jolt_t)],
                     "scene_cuts": [{"t": t, "what": "cut to Lansing, Michigan"} for t in at(self.cut_t)]}
            self.calls += 1
        elif "Ultra" in model:
            self.judge_prompts.append(body["messages"][1]["content"])
            reply = {"verdicts": []}
        else:
            text = body["messages"][0]["content"]
            reply = {"claims": [c.to_dict() for c in heuristic_claims(text.split('"""')[1])]} if '"""' in text else {"items": []}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def test_joined_footage_reaches_the_judge_as_segments(clip, tmp_path):
    # The real clip had the jolt at 3.47 s, the cut at 6.4 s and the Lansing crash at 13.07 s;
    # the 10 s test clip gets the same story at 3.0 / 6.0 / 7.3 s.
    api = ReplayAPI(jolt_t=3.0, cut_t=6.0, events=[
        {**REAL_WINDOWS[0]["events"][0], "t_end": 3.36},
        {"t_start": 7.28, "t_end": 7.84, "what": "The grey sedan enters the intersection and collides with the white sedan."}])
    settings = Settings(nebius_api_key="v1.test", cache_dir=tmp_path / "cache")
    client = TokenFactoryClient(settings, ResponseCache(settings.cache_dir), UsageLedger(), transport=httpx.MockTransport(api))
    report = pipeline.run(clip, DALLAS, settings=settings, client=client, max_frames=12, window_size=6)

    main = report["sources"][0]
    assert [g["t_start"] for g in main["segments"]][0] == 0.0 and len(main["segments"]) == 2
    assert main["camera_jolts"] and all(j["what"] for j in main["camera_jolts"])
    judge = api.judge_prompts[0]
    assert '"segments"' in judge and '"camera_jolts"' in judge
    lansing = json.loads(judge.split("Observations from the video model, by source and window:")[1].split("\n\nClaims to check:")[0])
    events = [e for w in lansing[0]["windows"] for e in w["events"]]
    assert {e["segment"] for e in events} == {0, 1}                       # Dallas and Lansing tagged apart
    # Claims nobody judged (the mock returns no verdicts) still default to an abstention, never a contradiction.
    assert report["summary"]["contradicted"] == 0 and report["summary"]["cant_tell"] == 5


def test_old_reports_get_one_segment_on_rejudge(clip, tmp_path):
    settings = Settings(offline=True, cache_dir=tmp_path / "c")
    report = pipeline.run(clip, DALLAS, settings=settings, max_frames=6)
    for src in report["sources"]:
        src.pop("segments"), src.pop("camera_jolts")
    out = pipeline.rejudge(report, clip, settings=settings, max_frames=6)
    assert all(len(s["segments"]) >= 1 for s in out["sources"])


# --- cache --------------------------------------------------------------------------------------

def test_empty_replies_are_not_cached(tmp_path):
    replies = iter(["", "", "a real answer", "should not be asked"])
    sent = []

    def api(request):
        sent.append(1)
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}], "usage": {}})
    settings = Settings(nebius_api_key="v1.test", cache_dir=tmp_path / "cache")
    msgs = [{"role": "user", "content": "hi"}]

    def ask():
        return TokenFactoryClient(settings, ResponseCache(settings.cache_dir), transport=httpx.MockTransport(api)).chat("m", msgs)
    assert ask().text == "" and ask().text == ""           # empty: asked again each time
    third = ask()
    assert third.text == "a real answer" and not third.cached
    fourth = ask()
    assert fourth.text == "a real answer" and fourth.cached and len(sent) == 3


def test_discovery_on_the_real_account_listing():
    from physics_witness.models import discover
    listing = sorted(["MiniMaxAI/MiniMax-M3", "Qwen/Qwen3.5-397B-A17B", "google/gemma-3-27b-it", "openbmb/MiniCPM-V-4_5",
                      "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "nvidia/Nemotron-3-Ultra-550b-a55b", "nvidia/Nemotron-3.5-Super-VL",
                      "nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b", "openai/gpt-oss-120b"])
    assert discover(listing) == {"cosmos": "nvidia/Nemotron-3.5-Super-VL", "ultra": "nvidia/Nemotron-3-Ultra-550b-a55b",
                                 "splitter": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", "super": "nvidia/nemotron-3-super-120b-a12b"}
    assert discover(["nvidia/Cosmos-Reason2-8B", "nvidia/Nemotron-3.5-Super-VL"])["cosmos"] == "nvidia/Cosmos-Reason2-8B"
