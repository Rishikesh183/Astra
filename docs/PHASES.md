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
- [x] Evidence per verdict: nearest sampled frames + the Cosmos observations covering each evidence time, `jump_to`
- [x] Tavily weather (place + date) and place facts -> Ultra context (URLs stripped from the prompt, kept in the report)
- [x] Run store: one directory per claim, background thread pool, status (queued/running/done/error), errors logged
- [x] Web app (`physics-witness serve`): upload a claim; video + timeline (sampled-frame ticks, verdict-coloured evidence markers, playhead) on the left; claims grouped by story sentence with Supported / Contradicted / Can't tell on the right; click to jump; evidence frames and "What Cosmos saw"; weather/place card with sources; offline banner; light/dark; phone layout
- [x] `physics-witness add` to pre-load claims (for the hosted demo on cached runs)
- [ ] **Deferred:** frame extraction inside a Token Factory Sandbox. This needs the Sandbox API docs; frame extraction runs in-process until then (Serverless Job is the fallback)
- [ ] **Needs keys:** check the UI with real Cosmos/Ultra/Tavily output on 3 clips

## Phase 3: Act loop, routing, cost meter (Oct 15 - 20)
- [x] Evidence request: for each Can't tell claim, one specific ask plus a message to the claimant (Nano/Super, with a template fallback)
- [x] Re-judge: extra clip becomes source `e1`, `e2`, ...; Cosmos reads it; Ultra re-judges only Can't tell claims across all sources; settled verdicts are kept; each round records its verdict changes
- [x] Ultra cites evidence as `{source, t}`; evidence frames and observations are labelled by source
- [x] Routing table (quality / budget policy) with fallbacks; pixel filter skips near-duplicate frames before Cosmos
- [x] Cost meter: tokens per task and model, cache savings, USD from `pricing.json` (not hard-coded), totals across rounds
- [x] Live progress: stage list + cost so far while a claim runs or is re-judged
- [x] UI: source tabs, "Ask the claimant" card with upload, evidence rounds history, cost and routing card
- [x] CLI: `physics-witness evidence <claim> <clip> --note ...`
- [ ] **Needs keys:** real Token Factory prices in `pricing.json`; check the re-judge on a real two-angle clip

## Phase 4: Liar Test (Oct 21 - 24)
- [x] Case file `liartest/cases.csv` (one row per clip: true story, altered story, alteration type, changed sentence, licence) + example
- [x] `liar check`: ids, clip files, video types, alteration types, changed sentence present, licence and source URL
- [x] `liar alter`: drafts one-sentence alterations (Nemotron when keys are set, word-swap rules otherwise; order swaps two actions only); backup + mandatory human review
- [x] `liar run`: both stories per clip (true first, so the altered run reuses the cached Cosmos readings), parallel workers, resumable, per-run errors kept
- [x] Scoring: caught = the claim from the changed sentence is Contradicted; false alarm = a true story gets any Contradicted; abstain rate; per-type breakdown; 95% Wilson intervals; tokens/cost per claim
- [x] Outputs: `results.csv`, `metrics.json`, `results.json`, self-contained `report.html` (the demo video's Liar Test scene)
- [x] Same command runs in the Docker image, so it can run as a Serverless Job
- [ ] **Needs clips:** 30-40 openly licensed / self-filmed clips in `liartest/clips/`
- [ ] **Needs keys:** the real run and the measured numbers

## Phase 5: Ship (Oct 25 - 28)
- Hosted demo on a Serverless Endpoint using cached runs; README; 3-minute video; feedback; submit
