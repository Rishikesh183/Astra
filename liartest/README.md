# Liar Test

Each clip runs twice: once with the **true story** and once with an **altered story** that changes one physical detail (direction, order of events, time of day, weather, contact point, or who was there). We measure:

- **Catch rate**: how often the altered sentence gets marked *Contradicted*.
- **False alarms**: how often a true story gets anything marked *Contradicted*.
- **Abstain rate**: how often the answer is *Can't tell*.

Every rate comes with a 95% interval. With 30–40 clips the intervals are wide, and the report says so.

## Where things go

```
liartest/
  clips/              <- put the Liar Test videos here (git-ignored)
  cases.csv           <- one row per clip (copy cases.example.csv to start)
  results/            <- written by `liar run` (git-ignored)
```

- The `clip` column is the file name inside `liartest/clips/`, e.g. `dash-001.mp4`.
- Formats: `.mp4 .mov .m4v .webm .mkv .avi`. 5–30 s clips work best, since the frame budget is 16 per clip by default.
- Only use **openly licensed** clips (put the licence, e.g. `CC-BY-4.0`, in `license` and the page in `source_url`) or footage **you filmed yourself** (`license` = `self-filmed`). Avoid clips where faces or plates are the subject.
- Other videos:
  - Demo claims for the web app: upload them in the UI, or run `physics-witness add <video> "<story>"`. They are stored in `data/claims/`.
  - Quick one-off CLI tries: `samples/`.

## Steps

```bash
cp liartest/cases.example.csv liartest/cases.csv   # then replace the example rows with yours
# drop the videos into liartest/clips/

physics-witness liar check                 # file names, licences, altered sentences; fix every ERROR
physics-witness liar alter --dry-run       # draft altered stories for rows with an alteration but no altered_story
physics-witness liar alter                 # write them (backup in cases.csv.bak), then READ every draft
physics-witness liar run --limit 3         # try a few first; runs are cached and resumable
physics-witness liar run                   # everything -> liartest/results/report.html
```

Re-running `liar run` skips pairs that already have a report (`--force` redoes them). The altered story reuses the cached Cosmos readings of the same clip, so each extra pair costs little more than one cross-examination.

## Writing good pairs

- Change **one** sentence and **one** detail, and make it something the camera can show: "hit my rear bumper" → "hit my front bumper", "turned left" → "turned right", "it was raining" → "it was dry".
- `altered_sentence` must be copied exactly from `altered_story`; that's how a catch is scored.
- For `order`, swap two events ("I braked, then the van stopped" → "the van stopped, then I braked"). `altered_sentence` is the one that moved earlier.
- Mix the types, and keep some clips where the footage *can't* settle the change (a dark or blurry clip). Abstaining there is correct behaviour.

## Running it as a batch job

The Docker image runs the same command, so the batch can run as a Nebius Serverless Job:

```bash
docker run --rm --env-file .env -v "$PWD/liartest:/app/liartest" -w /app physics-witness liar run --workers 4
```

Keep `--workers` well under 50 (the Sandbox concurrency limit).
