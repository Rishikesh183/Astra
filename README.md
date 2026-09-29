# Physics Witness

**License: [Apache 2.0](LICENSE)**

Physics Witness checks a claimant's story against what the video physically shows, and points to the exact seconds where they disagree.

> Insurers get thousands of claims with a video and a story. Physics Witness reads the video like an accident investigator and tells you which parts of the story the footage supports, contradicts, or can't settle.

The output is evidence for a human adjuster, **never a fraud verdict**. Every claim gets one of three verdicts (Supported, Contradicted, Can't tell), and the tool abstains when the footage can't settle the claim.

Built for the Nebius x NVIDIA Global AI Hackathon (deadline Oct 30, 2026).

## Status: Phase 2 (verdicts, Tavily, UI)

| Phase | Scope | Status |
|---|---|---|
| 1 | Kill-switch checks, frame sampling with burned timestamps, Token Factory + Tavily clients, response cache, core pipeline (claims -> Cosmos events -> Ultra verdicts), CLI, smoke tests | done |
| 2 | Evidence frames and Cosmos observations per verdict, Tavily weather/place context, one-screen web UI | **this branch** |
| 3 | Act loop (Can't tell -> ask for evidence -> re-judge), model routing, cost meter | planned |
| 4 | Liar Test: 30-40 clips x (true story, altered story), catch rate and false-alarm rate | planned |
| 5 | Hosted demo on Nebius Serverless, submission | planned |

See [docs/PHASES.md](docs/PHASES.md) for details.

## How it works

```
claim video ──> frame sampler ──> Cosmos (windows of <=6 frames) ──> timed events ─┐
                (motion-ranked,                                                     ├──> Nemotron Ultra ──> verdict per claim
                 timestamp burned in)                                               │    cross-examination    + evidence seconds
written story ──> Nemotron Nano/Super ──> atomic claims ────────────────────────────┘
```

1. **Frame sampling** (`physics_witness/frames.py`): ffmpeg decodes real source frames about 4 per second, the highest-motion ones are kept (first and last are always kept), and each gets its true timestamp printed in a band at the bottom. Cosmos reads timestamps from the bottom of frames.
2. **Windows**: the Nebius Cosmos endpoint takes 4-8 images per request, so frames are sent in overlapping windows of 6.
3. **Cosmos** (`events.py`) reports events with start and end times, actors, lighting, weather and visibility. It reports ordinal facts only, with no numeric speeds.
4. **Nano/Super** (`claims.py`) split the story into atomic, checkable claims.
5. **Tavily** (`context.py`): when the claim has a place (and a date), it looks up the weather that day and what the place is like. Ultra gets the answers and snippets and is told they count for less than the footage. The report keeps the source links.
6. **Ultra** (`crossexam.py`) merges the window observations and gives each claim a verdict, the evidence timestamps, its reasoning, and (for Can't tell) the one extra piece of evidence that would settle it.

7. **Evidence** (`evidence.py`): each verdict is linked to the nearest sampled frames and the Cosmos observations covering its evidence times, so the UI can jump to them.

Every model response is cached on disk (`.cache/responses`), so re-runs and recorded demos cost nothing.

## Quick start

```bash
pip install -e ".[dev]"
cp .env.example .env            # add NEBIUS_API_KEY (starts with v1.) and TAVILY_API_KEY
python scripts/make_sample_clip.py samples/synthetic.mp4

physics-witness checks --clip samples/synthetic.mp4     # the five Day 1-2 kill-switch checks
physics-witness frames samples/synthetic.mp4 --out out/frames
physics-witness run samples/synthetic.mp4 "I was stopped at the light. The other car hit my rear bumper." \
    --place "MG Road, Bengaluru" --date 2026-08-02 --out out/run

# Web app: video + timeline on the left, the story's claims and verdicts on the right
physics-witness add samples/synthetic.mp4 "The truck reversed into my door." --title "Sample"   # optional: pre-load a claim
physics-witness serve            # http://127.0.0.1:8000
```

In the web app you can upload a clip with a story (plus optional place and date). Claim runs happen in the background. Clicking a claim, a timeline marker or an evidence frame jumps the video to that moment. Claims are stored under `data/claims/<id>/` (video, story, `report.json`, evidence frames).

With no API key (or `PW_OFFLINE=1`) everything runs with deterministic stub replies: the video is really sampled, but every verdict is Can't tell.

Docker (one command):

```bash
docker build -t physics-witness . && docker run --rm -p 8000:8000 --env-file .env -v "$PWD/data:/data" physics-witness
```

Tests (no network or keys needed; the live code path is exercised through a mock HTTP transport):

```bash
pytest
```

## Configuration

| Variable | Purpose |
|---|---|
| `NEBIUS_API_KEY` | Token Factory key |
| `NEBIUS_BASE_URL` | Token Factory OpenAI-compatible base URL (default `https://api.tokenfactory.nebius.com/v1`) |
| `PW_COSMOS_MODEL`, `PW_ULTRA_MODEL`, `PW_SPLITTER_MODEL` | Model IDs. Leave empty to auto-discover from `/models` |
| `TAVILY_API_KEY` | Tavily search |
| `PW_OFFLINE` | `1` = never call the network |
| `PW_CACHE_DIR` | Response cache directory |

## NVIDIA models and Nebius services used

- **NVIDIA Cosmos reasoner (on Nebius Token Factory)**: reads windows of timestamped frames and reports physical events.
- **NVIDIA Nemotron Ultra (on Token Factory)**: cross-examines each claim against the merged observations.
- **NVIDIA Nemotron Nano / Super (on Token Factory)**: splits the story into atomic claims. In Phase 3 it also filters frames.
- **Nebius Token Factory**: all model calls go through its OpenAI-compatible API. 402/403 responses are reported as credit or access problems.
- **Nebius Sandboxes / Serverless Jobs / Serverless Endpoints**: planned for frame extraction, Liar Test batches and hosting (phases 2-5).
- **Tavily**: weather on the claim date and facts about the place, passed to Ultra as outside context (at runtime, whenever a claim has a place).

## What it does not claim

It does not detect deepfakes, estimate exact speeds, or replace an adjuster. A wrong "Contradicted" could hurt a real claimant, so the tool prefers to abstain, and the demo uses only openly licensed or self-filmed clips.
