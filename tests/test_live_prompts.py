"""Fixes from the first live run (train-crossing dashcam clip)."""

import json

from physics_witness import act
from physics_witness.claims import heuristic_claims
from physics_witness.clients.token_factory import TokenFactoryClient
from physics_witness.config import Settings
from physics_witness.crossexam import SYSTEM_PROMPT, build_user_prompt
from physics_witness.events import COSMOS_PROMPT


class Reply:
    """A client whose model always answers with `text` (online, not cached)."""

    offline = False

    def __init__(self, text):
        self.text = text

    def chat(self, *a, **k):
        return type("R", (), {"text": self.text})()


PENDING = [
    {"claim_id": "c2", "claim": "The car ahead of me was on the crossing.", "verdict": "cant_tell",
     "needed_evidence": "footage showing which vehicle is ahead"},
    {"claim_id": "c5", "claim": "It was a sunny morning.", "verdict": "cant_tell", "needed_evidence": "the time of day"},
]


def test_request_message_always_lists_every_ask():
    # What the live model actually returned: a polite intro with no list.
    intro_only = json.dumps({"message": "Dear claimant, to help resolve your claim, could you please provide the following specific evidence?",
                             "items": [{"claim_id": "c2", "ask": "A photo of both cars at the crossing"},
                                       {"claim_id": "c5", "ask": "The dashcam file with its original timestamp"}]})
    req = act.compose_request(Reply(intro_only), "m", PENDING)
    assert req["message"].startswith("Dear claimant")
    assert "- A photo of both cars at the crossing" in req["message"]
    assert "- The dashcam file with its original timestamp" in req["message"]

    # A message that already lists the asks is left alone (no duplicates).
    full = json.dumps({"message": "Please send: a photo of both cars at the crossing, and the dashcam file with its original timestamp.",
                       "items": [{"claim_id": "c2", "ask": "A photo of both cars at the crossing"},
                                 {"claim_id": "c5", "ask": "The dashcam file with its original timestamp"}]})
    req = act.compose_request(Reply(full), "m", PENDING)
    assert "\n- " not in req["message"]

    # Offline fallback still lists everything.
    off = act.compose_request(TokenFactoryClient(Settings(offline=True)), "", PENDING)
    assert all(v["needed_evidence"] in off["message"] for v in PENDING)


def test_camera_viewpoint_and_overlay_reach_the_judge():
    assert "dashcam" in COSMOS_PROMPT and "camera_overlay" in COSMOS_PROMPT and "on-screen text" in COSMOS_PROMPT
    assert "claimant's own vehicle" in SYSTEM_PROMPT and "Never make this assumption for CCTV" in SYSTEM_PROMPT
    sources = [{"id": "main", "label": "Original video", "duration_s": 22.0, "windows": [{
        "t_start": 0, "t_end": 5, "events": [{"t_start": 3.4, "t_end": 3.9, "what": "locomotive hits the car ahead"}],
        "conditions": {"camera": "dashcam", "camera_motion": "stationary", "camera_overlay": "GARMIN 2017/09/19 08:12:39 000KM/H"}}]}]
    prompt = build_user_prompt(heuristic_claims("The car ahead of me was on the crossing."), sources)
    assert '"camera": "dashcam"' in prompt and "08:12:39" in prompt and '"camera_motion": "stationary"' in prompt


def test_cosmos_prompt_still_lists_frame_times_first():
    # The frame-time list must stay the first parenthesis (the mock API in test_smoke parses it).
    text = COSMOS_PROMPT.format(n=2, labels="t=0.00s, t=1.00s")
    assert text.split("(")[1].split(")")[0] == "t=0.00s, t=1.00s"
