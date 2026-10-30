# Deploying Physics Witness to Nebius (runbook for a coding agent)

> **How to use this file:** open the repo on your laptop in a coding agent (for example Claude Code) and say:
> *"Follow docs/DEPLOY.md and deploy Physics Witness."*
> The agent runs the pre-checks, asks before anything that costs money, and reports the public URL at the end.

Goal: one public HTTPS URL where judges can try the three samples and upload their own clip.

**What gets deployed:** a Nebius Serverless endpoint running the stock `python:3.11-slim` image, on a small CPU machine. **No Docker build is needed.** The repo's [`deploy/nebius/start.sh`](../deploy/nebius/start.sh) is injected into the container. On every start it downloads the code from GitHub, installs it and serves on port 8000. All models run on Nebius Token Factory. Claims, videos and the model cache live in Supabase, so the container's disk is only scratch space.

---

## Rules for the agent (read first)

1. **Never print, echo, log or commit secret values.** These are `NEBIUS_API_KEY`, `TAVILY_API_KEY`, `SUPABASE_SERVICE_KEY` and any Nebius token. Read them from `.env` into variables or files that are never displayed. When you show a command, put `<from .env>` in place of the value.
2. **Ask the user before every billable or hard-to-undo step.** That means `nebius ai endpoint create / start / restart`, and creating registries or secrets. Show the full command (with secrets as placeholders), say what it costs per hour, and wait for a clear "yes". Run each one **once**. Never retry after an unclear failure: check the state first.
3. **Never run `delete`.** Print the command for the user to run.
4. **Never use `--follow`, `-i` / `--interactive`, `edit`** or any other Nebius CLI flag that waits for input. Add `--format json` to Nebius reads and filter them with `jq`.
5. **Never print raw `nebius ai endpoint get/list` output.** It contains the spec, and the spec contains plain `--env` values. Always project fields with `jq` (see step 6).
6. If any pre-check fails, **stop**, tell the user exactly what's missing and how to fix it, and continue only once it passes.
7. On Windows, the Nebius CLI is installed through a bash script, so run the `nebius` steps inside **WSL** (Ubuntu). The repo, `.env` and `jq` must be reachable from there (for example `/mnt/c/Users/<user>/Downloads/astra`).

---

## Step 1: Pre-checks (local, free)

Run each one and report a ✅/❌ table to the user.

| # | Check | How | Pass when |
|---|---|---|---|
| 1 | On `develop`, up to date | `git fetch origin develop && git status -sb` | Branch is `develop`, not behind `origin/develop` |
| 2 | Deploy files are on GitHub | `git ls-tree -r origin/develop --name-only \| grep -E "^deploy/nebius/start.sh$"` | Found. If not, PR #15 isn't merged: ask the user to merge it |
| 3 | Repo is public | `curl -s -o /dev/null -w "%{http_code}" https://codeload.github.com/Rishikesh183/Astra/zip/develop` | `200`. The container downloads the code anonymously |
| 4 | `.env` has the values | Check that these keys exist and are **non-empty**, **without printing them**: `NEBIUS_API_KEY`, `TAVILY_API_KEY`, `PW_STORE` (must be `supabase`), `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `PW_COSMOS_MODEL`, `PW_ULTRA_MODEL`, `PW_SPLITTER_MODEL`, `PW_SUPER_MODEL`, `PW_POLICY` | All present. Model names can't be empty: cache hits on the server need the exact names used locally |
| 5 | App installed locally | `physics-witness --help` (inside the repo's `.venv`) | Exit 0 |
| 6 | Models and Tavily reachable | `physics-witness checks` | Token Factory and Tavily checks PASS |
| 7 | Supabase ready | `physics-witness supabase check` | Every line PASS |
| 8 | Samples available | `physics-witness samples` | `crossing-true` and `crossing-liar` are `ok` (see the note on the third sample below) |
| 9 | Tests pass | `python -m pytest -q` | No failures |
| 10 | Nebius CLI | `nebius version` (0.12.277 or newer), `nebius profile list`, `nebius config get parent-id` | Version OK, a profile exists, a `project-…` ID comes back |
| 11 | `jq` installed | `jq --version` | Exit 0 |

If check 10 fails, give the user these commands to run themselves. Don't run them yourself: they install software and open a browser to log in.

```bash
curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
nebius profile create            # logs in through the browser and picks the project
nebius update                    # if the version is older than 0.12.277
```

**The third sample (`intersection-true`):** its video `demo/videos/dallas.mp4` is git-ignored (it comes from a YouTube compilation), so it isn't on GitHub. Run `physics-witness supabase samples` locally. It uploads every local sample clip to the private Supabase bucket under `samples/`, and the deployed server downloads any missing clip from there when it starts (it needs `PW_STORE=supabase`). Without this, the deployed site shows that sample as "Video not added yet".

## Step 2: Warm the shared cache (local, uses some tokens)

The deployed server starts with an empty disk. It answers the samples instantly only if their model replies are already in Supabase.

1. `physics-witness supabase migrate --cache`: copies the local `.cache/responses/` into Supabase. Report the "N cached model replies copied" line.
2. Start the app locally (`physics-witness serve`), and ask the user to click **No video? Try a sample** and run each available sample **once**, checking that the header says **Storage: Supabase**. Or run them through the API:
   ```bash
   curl -s -X POST http://127.0.0.1:8000/api/samples/crossing-true/try
   curl -s -X POST http://127.0.0.1:8000/api/samples/crossing-liar/try
   ```
   Each returns `{"id": ...}`. Poll `GET /api/claims/<id>` until `.meta.status` is `done`; the verdicts are in `.report.verdicts`.
3. Run one sample again. It must finish in a few seconds. That proves it's served from the cache.
4. Stop the local server.

## Step 3: Secrets in MysteryBox (ask first)

Secrets must not go in plain `--env`, because those values are readable in the endpoint spec. Create **one MysteryBox secret per key**, with the payload key named after the environment variable:

| Secret name | Payload key | Value from `.env` |
|---|---|---|
| `pw-nebius-key` | `NEBIUS_API_KEY` | `NEBIUS_API_KEY` |
| `pw-tavily-key` | `TAVILY_API_KEY` | `TAVILY_API_KEY` |
| `pw-supabase-key` | `SUPABASE_SERVICE_KEY` | `SUPABASE_SERVICE_KEY` |

- First check whether they already exist (`nebius mysterybox secret list --parent-id $PROJECT --format json | jq '.items[]? | .metadata.name'`). If they exist, reuse them.
- To create them, check `nebius mysterybox secret create --help` for how to pass the payload. Feed the value from a variable or a temporary file (deleted right after), never as a literal in the command line or the transcript.
- If the CLI route is unclear, ask the user to create them in the Nebius console (**MysteryBox → Create secret**) with the names and keys above, then confirm they exist.

## Step 4: Create the endpoint (billable, ask first)

```bash
export PROJECT=$(nebius config get parent-id)
```

**4a. Is there one already?** Never create a duplicate, because each one is a second bill:

```bash
nebius ai endpoint get-by-name --name physics-witness --parent-id $PROJECT --format json \
  | jq '{id: .metadata.id, state: .status.state, urls: .status.public_endpoints}'
```

If it exists, report its state and URL and skip to step 5. To change it, see "Updating" below.

**4b. Platform check:** `nebius compute platform list --parent-id $PROJECT --format json | jq -r '.items[]?.metadata.name'` must include `cpu-d3`. If it doesn't, pick the smallest CPU platform listed and tell the user.

**4c. Dry run.** Fill the `--env` values from `.env`. Model names and the Supabase URL aren't secret.

```bash
nebius ai endpoint create \
  --parent-id $PROJECT \
  --name physics-witness \
  --image python:3.11-slim \
  --platform cpu-d3 --preset 4vcpu-16gb \
  --container-port 8000/http \
  --auth none \
  --inject-file deploy/nebius/start.sh:/start.sh \
  --container-command sh \
  --args /start.sh \
  --env PW_GIT_REF=develop \
  --env PW_STORE=supabase \
  --env SUPABASE_URL=<from .env> \
  --env SUPABASE_BUCKET=claims \
  --env PW_POLICY=<from .env> \
  --env PW_COSMOS_MODEL=<from .env> \
  --env PW_ULTRA_MODEL=<from .env> \
  --env PW_SPLITTER_MODEL=<from .env> \
  --env PW_SUPER_MODEL=<from .env> \
  --env-secret NEBIUS_API_KEY=pw-nebius-key \
  --env-secret TAVILY_API_KEY=pw-tavily-key \
  --env-secret SUPABASE_SERVICE_KEY=pw-supabase-key \
  --dry-run
```

- `--auth none` is on purpose: judges open the URL in a browser, which can't send a bearer token. The service keys stay on the server.
- `--container-port 8000/http` gives a managed `https://` URL. Don't add `--public`.
- Run the command from the repo root, so `deploy/nebius/start.sh` resolves.
- If the dry run rejects a flag, check `nebius ai endpoint create --help` and adjust. Don't invent flags.

**4d. Tell the user the cost.** It bills **every hour from create to delete**, idle or not. Look up the `cpu-d3 / 4vcpu-16gb` hourly price (nebius.com/prices or `nebius billing` / the console calculator), and state the hourly rate, the daily rate and the total until Dec 15, 2026 (end of judging). Wait for "yes".

**4e. Create** it: the same command, without `--dry-run`, plus `--async`. Then poll every 20–30 s until the state is `RUNNING` (or it fails):

```bash
nebius ai endpoint get-by-name --name physics-witness --parent-id $PROJECT --format json \
  | jq '{id: .metadata.id, state: .status.state, urls: .status.public_endpoints}'
```

The first start takes about 1–3 minutes, because `start.sh` downloads the repo and runs `pip install`.

## Step 5: Smoke test the live site (free)

`URL` is the first entry of `.status.public_endpoints`.

| # | Check | Pass when |
|---|---|---|
| 1 | `curl -s $URL/api/health` | JSON shows `"nebius": true`, `"tavily": true`, `"store": "supabase"` |
| 2 | `curl -s $URL/api/samples` | The samples are listed, with `crossing-true` and `crossing-liar` available |
| 3 | `curl -s $URL/api/claims` | The claims already in Supabase are listed |
| 4 | `curl -s -X POST $URL/api/samples/crossing-liar/try`, then poll `GET $URL/api/claims/<id>` | `.meta.status` is `done` within about 30 s (cache hit), and in `.report.verdicts` the claim for "A train came from the left." is `contradicted` |
| 5 | `curl -s -o /dev/null -w "%{http_code}" $URL/` | `200` |

If something fails, read the logs (never with `--follow`):

```bash
nebius ai endpoint logs <endpoint-id>
```

Common causes:
- `start.sh` couldn't download the code: the repo is private, or `PW_GIT_REF` is wrong.
- `pip install` failed.
- A secret didn't reach the container: `/api/health` shows `nebius: false`, or the Supabase check fails. Check the `--env-secret` names, and that each payload key matches the variable name.

## Step 6: Report to the user

- The public URL (it goes in the Devpost form)
- Endpoint ID, name and state
- The cost line (per hour and per day, billing until deleted)
- Which samples are live

## Updating, pausing, removing

- **New code:** merge to `develop`, then restart the endpoint (`nebius ai endpoint restart <id>`, ask first). `start.sh` downloads the latest `develop` on every start. To pin a version, create the endpoint with `--env PW_GIT_REF=<tag or commit>`.
- **Changing flags, env values or secrets:** there's no in-place update. Create a new endpoint under a new name, smoke test it, then the **user** deletes the old one.
- **Pause billing:** `nebius ai endpoint stop <id>`. Resume with `nebius ai endpoint start <id>`. Both need the user's confirmation.
- **Remove after judging:** print this for the user, don't run it: `nebius ai endpoint delete <id>`.
- Afterwards, rotate any API key that was shared during development.

## Fallback: a Docker image (only if the stock-image route fails)

The repo's `Dockerfile` builds the same app, with the samples (and any local `demo/videos/` clips, such as `dallas.mp4`) baked in. Only use it if the user has Docker Desktop:

```bash
docker build --platform linux/amd64 -t physics-witness:latest .
nebius registry configure-helper
export REG=cr.eu-north1.nebius.cloud/<registry-id>      # Container Registry in the console, region eu-north1
docker tag physics-witness:latest $REG/physics-witness:v1 && docker push $REG/physics-witness:v1
```

Then use step 4 with `--image $REG/physics-witness:v1`, and **without** `--inject-file`, `--container-command` and `--args`. The image is private: push it only to the Nebius registry, never to a public one. If the endpoint can't pull it, add `--registry-secret` with a MysteryBox secret holding `REGISTRY_USERNAME` and `REGISTRY_PASSWORD`.

## Submission checklist

- [ ] Public repo with LICENSE (Apache 2.0) and README
- [ ] Demo URL (the endpoint) in the Devpost form
- [ ] Demo video ([VIDEO_SCRIPT.md](VIDEO_SCRIPT.md)) on YouTube, public or unlisted
- [ ] NVIDIA models listed (Nemotron 3.5 Super VL, Nemotron 3 Ultra, Nemotron 3 Nano, Nemotron 3 Super) and Nebius services (Token Factory, Serverless)
- [ ] Submitted before **Oct 30, 2026, 10:00 am PDT** (10:30 pm IST)
