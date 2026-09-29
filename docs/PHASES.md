# Build phases

Plan dated 2026-09-29. Hard deadline Oct 30, 2026, 10:00am PDT; target submission Oct 28.

## Phase 1: Kill-switch checks + core pipeline (Sep 29 - Oct 7)

- [x] Repo scaffold: Apache 2.0, pyproject, Dockerfile, `.env.example`
- [x] `physics-witness checks`: runs the five Day 1-2 checks and prints PASS / FAIL / SKIP / MANUAL
  - 1 Cosmos callable (discovers the model name from `/models`, sends one frame)
  - 2 Video works (one window of up to 8 timestamped frames -> timed events)
  - 3 Ultra callable with a ~20K-token prompt (timed)
  - 4 Tavily weather call
  - 5 Sandbox: **manual for now**. The Sandbox API isn't wired in yet, so run `physics-witness frames` inside a Sandbox by hand. A local ffmpeg check runs as check 0.
- [x] Frame sampler: real source frames with true timestamps, motion ranking, timestamp band
- [x] Overlapping windows sized to the 4-8 image limit
- [x] Token Factory client: OpenAI-compatible, disk cache, usage ledger, clear 402/403 message
- [x] Tavily client
- [x] Claims splitter (Nano/Super, with a sentence-split fallback)
- [x] Cosmos window reader, Ultra cross-examiner (three verdicts, abstains on missing or invalid output)
- [x] CLI `run` producing `report.json` + evidence frames
- [x] Offline stub mode + smoke tests with a mocked API
- [ ] **Needs keys:** run `checks` against the live API and make the track decision (end of Day 2)
- [ ] **Needs keys:** core pipeline on 3 real clips

## Phase 2: Verdicts, Tavily, UI (Oct 8 - 14)
- Evidence frames per verdict, click-to-jump
- Tavily weather/place lookup from claim date and place -> Ultra context
- One-screen UI: video + timeline left, story sentences right, tagged verdicts
- Frame extraction inside a Token Factory Sandbox (Serverless Job fallback)

## Phase 3: Act loop, routing, cost meter (Oct 15 - 20)
- Can't tell -> agent requests one specific extra piece of evidence -> re-judge
- Nano/Super filter frames before Cosmos; cost per claim shown live (from `UsageLedger`)

## Phase 4: Liar Test (Oct 21 - 24)
- 30-40 clips, each with a true story and an altered one (direction, order, time of day)
- Batch as Serverless Jobs; report catch rate, false-alarm rate, abstain rate

## Phase 5: Ship (Oct 25 - 28)
- Hosted demo on a Serverless Endpoint using cached runs; README; 3-minute video; feedback; submit
