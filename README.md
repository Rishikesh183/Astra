# Physics Witness

Physics Witness checks a claimant's accident story against the video footage, sentence by sentence, and points to the exact seconds that back up each answer.

## What it is

You give it two things: a **video** of the incident (dashcam, CCTV, phone) and the **claimant's written story** ("I was stopped at the light. The other car hit my rear bumper."). It splits the story into short, checkable statements and gives each one a verdict:

- **Supported**: the footage shows it.
- **Contradicted**: the footage clearly shows something else, for example the train comes from the right, not the left.
- **Can't tell**: the footage doesn't settle it, because the camera points the wrong way, the moment is cut off, or it's too dark. The tool then writes the claimant one specific request, such as "the rear camera angle" or "the 10 seconds after the impact", and re-judges when that footage arrives.

Every verdict comes with its reasoning and the evidence frames, each one stamped with its time in the video. Clicking one jumps the video to that moment, so a person can check it in seconds.

The output is evidence for a human, **never a fraud verdict**. When the footage can't settle a statement, the tool says so rather than guess.

## Why it exists

Insurers, fleet operators and claims teams receive more and more claims with video attached. Dashcams are cheap, and every street has a camera. Someone still has to watch each clip and compare it with what the claimant wrote. That's slow, it's tedious, and small mismatches are easy to miss: which side the other car came from, who moved first, whether the light was red, whether it was raining.

Plain video AI can describe a clip ("a car crash on a road"), but describing a clip is not checking a story. Physics Witness asks the vision model only for what physically happened and when (who moved, from which side, the impact, the camera being knocked). A separate reasoning model then cross-examines each statement against those timed observations. It needs positive evidence before it calls a statement Contradicted.

## How it helps in real life

| Who | How they use it |
|---|---|
| **Insurance claims adjusters** | Triage incoming video claims. Clean claims (all Supported) can move fast. A claim with a Contradicted statement goes to a person, with the exact seconds to watch instead of the whole clip. |
| **Fraud / special investigation teams** | A starting point, not a conclusion. The mismatch is shown frame by frame, so the investigator decides, and the claimant gets a fair chance to explain or send more footage. |
| **Fleet and logistics operators** | Check a driver's incident report against the truck's dashcam before the insurer or the other party does. |
| **Claimants and their lawyers** | Show that the footage backs the story. "Supported, see 00:03.4" is easier to argue than "watch this video". |
| **Police, rental and ride-share companies** | The same check for incident reports that come with video. |

What changes in practice:
- **Time:** a reviewer reads a one-screen report and watches only the flagged seconds.
- **Consistency:** every claim is checked the same way, statement by statement, with reasons written down.
- **Fairness:** "Can't tell" is a first-class answer. Missing footage leads to a request for more evidence, not a guess against the claimant.
- **Audit trail:** every verdict keeps its reasoning, evidence frames, the models used and the cost of the run.

Example, from the bundled sample: a level-crossing dashcam clip, with the story "...A train came from the left...". The footage shows the train arriving from the right. That one sentence should come back **Contradicted**, with the frames that show it. The rest come back **Supported**, or **Can't tell** where the clip doesn't show the detail, such as the weather.

Built for the Nebius x NVIDIA Global AI Hackathon (Physical AI track, deadline Oct 30, 2026). **License: [Apache 2.0](LICENSE)**

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Checks that the models and services are reachable, frame sampling with burned-in timestamps, Token Factory + Tavily clients, response cache, core pipeline, CLI, smoke tests | done |
| 2 | Evidence frames and observations per verdict, weather/place context, web UI | done |
| 3 | Act loop (Can't tell -> ask for evidence -> re-judge), model routing, live cost meter | done |
| 4 | Liar Test tooling: case file, batch runner, catch / false-alarm / abstain rates with 95% intervals, HTML report | done |
| + | Scene-cut segments, cancel/delete, home page of past claims, sample claims, Supabase storage, 40 MB upload cap | done |
| 5 | Hosted demo on Nebius, submission | in progress |

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

8. **Act** (`act.py`, `pipeline.rejudge`): for each Can't tell claim, the agent writes the claimant one specific request (for example "footage from 10 seconds later" or "the rear camera angle"). When the claimant sends the clip, it is added as a new source (`e1`, `e2`, ...). Cosmos reads it, and Ultra re-judges **only** the Can't tell claims using every source. Settled verdicts are left alone, and each round records which verdicts changed.

### Model routing and cost meter

| Task | Model (quality policy) | Budget policy |
|---|---|---|
| Split story into claims | Nemotron Nano (else Super) | same |
| Read frames | Cosmos (the only vision model) | same |
| Cross-examine | Nemotron Ultra | Nemotron Super |
| Ask for evidence | Nemotron Nano (else Super) | same |

Set the policy with `PW_POLICY=quality|budget`. Before Cosmos, a cheap pixel filter (`routing.drop_near_duplicates`) skips frames that barely differ from the last frame kept, so Cosmos only reads frames where something changed. The text models can't see images, so this filter is pixel-based rather than done by Nano.

Every call is logged by task and model. The UI shows a live cost meter while a claim runs, and the total cost for the claim (all rounds) when it's done. Tokens served from the cache count as saved. Prices are **not** hard-coded: copy `pricing.example.json` to `pricing.json` and fill in USD per 1M tokens from the [Token Factory price page](https://tokenfactory.nebius.com/organization/prices). Without it, the meter shows exact token counts and "price not set".

Every model response is cached on disk (`.cache/responses`), and also in Supabase when `PW_STORE=supabase`, so re-runs and recorded demos cost nothing.

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
physics-witness evidence <claim-id> rear.mp4 --note "rear dashcam"   # act loop from the CLI
physics-witness trim long-clip.webm --start 1:05 --length 20         # cut the 20 s around the incident
physics-witness serve            # http://127.0.0.1:8000
```

Short clips (5–30 s around the incident) give the best results. Longer clips work: frame sampling is capped at 120 candidate frames and clips over 90 s are sampled by seeking, so a 5-minute 1080p clip samples in well under a minute. Use `physics-witness trim` to cut out the part that matters. A running claim can be **cancelled** from the progress screen, and any claim can be **deleted**. If the server stops mid-run, the claim is marked as interrupted on the next start instead of staying "running".

In the web app you can upload a clip with a story (plus optional place and date). Claim runs happen in the background. Clicking a claim, a timeline marker or an evidence frame jumps the video to that moment, switching to the extra clip if that's where the evidence is. When some claims are Can't tell, the "Ask the claimant" card shows the request, and you can upload the clip the claimant sends to re-judge. Claims are stored under `data/claims/<id>/` (video, story, `report.json`, evidence frames).

With no API key (or `PW_OFFLINE=1`) everything runs with deterministic stub replies: the video is really sampled, but every verdict is Can't tell.

Hosting it publicly (Nebius Serverless + Supabase): see [docs/DEPLOY.md](docs/DEPLOY.md). Demo video script: [docs/VIDEO_SCRIPT.md](docs/VIDEO_SCRIPT.md).

Docker (one command):

```bash
docker build -t physics-witness . && docker run --rm -p 8000:8000 --env-file .env -v "$PWD/data:/data" physics-witness
```

Tests (no network or keys needed; the live code path is exercised through a mock HTTP transport):

```bash
pytest
```

## Storage: local files or Supabase

By default everything is stored as files on the server: claims in `data/claims/<id>/` and cached model replies in `.cache/responses/`. That's fine on one machine. On a hosted deployment, where containers are replaced and can run side by side, switch to **Supabase**:

| What | `PW_STORE=files` (default) | `PW_STORE=supabase` |
|---|---|---|
| Claim status, card stats, full report | `meta.json`, `report.json` | Postgres table `claims` (`meta`, `report` as JSONB; status, title, dates as columns) |
| Verdicts | inside the report | also table `verdicts`, one row per statement (query with SQL; see the `claim_verdict_summary` view) |
| Videos, extra clips, evidence frames | the claim folder | private Storage bucket `claims`, served through short-lived signed URLs |
| Cached model replies | `.cache/responses/` | local files first, then the shared table `model_cache` |

Setup:

1. Create a Supabase project. In **SQL Editor**, run [`deploy/supabase/schema.sql`](deploy/supabase/schema.sql). It creates the tables (with Row Level Security on and no public policies), the private bucket and the summary view.
2. In `.env`, set `PW_STORE=supabase`, `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` (the **service_role** key from Settings → API; it stays on the server).
3. `physics-witness supabase check` should print PASS for every step.
4. Optional: `physics-witness supabase migrate --cache` copies your existing local claims and cached model replies into Supabase. The cached replies matter for the sample claims, so a fresh deployment answers them instantly.

Any server instance can then list, show, cancel and delete any claim. A run executes on the instance that started it; a claim that stops reporting progress for 10 minutes is marked as interrupted. Uploads are capped at 40 MB per video (in the page and on the server), below the free plan's 50 MB file limit.

## Sample claims ("No video? Try a sample")

The home page lists every claim as a card (thumbnail, verdict counts, length, cost), and each claim has its own link, `/claims/<id>`. For visitors without footage, **No video? Try a sample** opens ready-made claims: real dashcam clips with a true story or a story with one lie in it (highlighted).

Samples are data, not code: edit `demo/samples.json` (or point `PW_SAMPLES` at another file). Each entry has an `id`, `title`, `label` (`real` or `liar`), `video` (relative to the file), `story`, and for liar stories the exact `lie_sentence`. Videos that aren't committed (e.g. `demo/videos/`, git-ignored) show as "not added yet" until the file is there. Check the file with:

```bash
physics-witness samples
```

Only state what the footage clearly shows in sample stories. Running a sample once caches its model replies, so later runs are instant and free.

## Liar Test

Every clip runs twice: once with the claimant's true story and once with one physical detail changed (direction, order, time of day, weather, contact point, presence). The report shows how often the altered sentence is caught, how often true stories are wrongly flagged, and how often the tool abstains, each with a 95% interval.

```bash
cp liartest/cases.example.csv liartest/cases.csv    # one row per clip
# videos go in liartest/clips/
physics-witness liar check        # validate before spending credit
physics-witness liar alter        # draft altered stories (review every one)
physics-witness liar run          # -> liartest/results/report.html
```

Full instructions, including where videos go and how to write good pairs, are in [liartest/README.md](liartest/README.md).

## Configuration

| Variable | Purpose |
|---|---|
| `NEBIUS_API_KEY` | Token Factory key |
| `NEBIUS_BASE_URL` | Token Factory OpenAI-compatible base URL (default `https://api.tokenfactory.nebius.com/v1`) |
| `PW_COSMOS_MODEL`, `PW_ULTRA_MODEL`, `PW_SPLITTER_MODEL` | Model IDs. Leave empty to auto-discover from `/models` |
| `TAVILY_API_KEY` | Tavily search |
| `TAVILY_API_KEY_FALLBACK` | Second Tavily key, used when the first is out of credit or rejected |
| `PW_POLICY` | `quality` (Ultra judges) or `budget` (Super judges) |
| `PW_PRICES_FILE` | Prices for the cost meter (default `pricing.json`) |
| `PW_OFFLINE` | `1` = never call the network |
| `PW_CACHE_DIR` | Response cache directory |
| `PW_STORE` | `files` (default) or `supabase` |
| `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_BUCKET` | Supabase project, service_role key and bucket (see [Storage](#storage-local-files-or-supabase)) |

## NVIDIA models and Nebius services used

- **Vision model on Token Factory** (`PW_COSMOS_MODEL`): reads windows of timestamped frames and reports physical events. The pipeline is built for NVIDIA Cosmos. Where Cosmos isn't enabled on the account, NVIDIA Nemotron 3.5 Super VL fills the same role.
- **NVIDIA Nemotron Ultra (on Token Factory)**: cross-examines each claim against the merged observations.
- **NVIDIA Nemotron Nano / Super (on Token Factory)**: splits the story into claims and writes the evidence request to the claimant. Under the budget policy, Super also does the cross-examination.
- **Nebius Token Factory**: all model calls go through its OpenAI-compatible API. 402/403 responses are reported as credit or access problems.
- **Nebius Serverless**: planned for hosting the demo (phase 5).
- **Supabase** (optional): Postgres + Storage for claims, videos and the shared model cache on a hosted deployment.
- **Tavily**: weather on the claim date and facts about the place, passed to Ultra as outside context (at runtime, whenever a claim has a place).

## What it does not claim

It does not detect deepfakes, estimate exact speeds, or replace an adjuster. A wrong "Contradicted" could hurt a real claimant, so the tool prefers to abstain, and the demo uses only openly licensed or self-filmed clips.
