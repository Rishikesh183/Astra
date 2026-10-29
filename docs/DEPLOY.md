# Deploying Physics Witness

Goal: one public HTTPS URL that judges can open, try the three samples on, and upload their own clip to.

## Where to host

The hackathon rule (as summarised on the listing pages; check the official Devpost rules) is that the project must **run on Nebius Token Factory or Nebius AI Cloud** and use at least one NVIDIA open model. Every model call already goes through Token Factory to NVIDIA Nemotron models, so the web server itself can be hosted anywhere. Nebius is not mandatory for the host.

| | Nebius Serverless endpoint (recommended) | Modal |
|---|---|---|
| Story for judges | Fully on Nebius: Token Factory for models, AI Cloud for the app | Models on Nebius, app on Modal |
| Cost while idle | Bills **every hour from create to delete**, idle or not | Scales to zero: pay only while requests run |
| Setup | Docker image + one CLI command (below) | A small `modal_app.py` wrapper (not written yet) |
| Uploads and runs | Normal disk, a long-running server | Needs a volume; cold starts |

**Recommendation: Nebius Serverless endpoint on a small CPU preset.** It's the stronger story for a Nebius x NVIDIA hackathon. No GPU is needed, because all models run on Token Factory. Judging runs Dec 1–15, so the endpoint has to stay up until then. Check the hourly price of `cpu-d3 / 4vcpu-16gb` at <https://nebius.com/prices> and multiply by about 1,150 hours (Oct 29 – Dec 15). If that's too much, Modal's scale-to-zero is the cheaper fallback.

Everything persistent lives in **Supabase** (claims, videos, frames, the model cache), so the container's own disk can be thrown away at any time.

---

## 0. Before you deploy (on your laptop)

1. `git pull` on `develop`. `.env` must have `PW_STORE=supabase` and the `SUPABASE_*` values.
2. Restart the server and check that the header shows **Storage: Supabase**.
3. Fill the shared model cache so the samples answer instantly on the deployed site:
   ```powershell
   physics-witness supabase migrate --cache
   ```
   Then run **each of the three samples once** in the web app. A second run of any sample should finish in seconds, which proves it's cached.
4. `physics-witness samples` should print `ok` for all three.

> Cache hits need the **same model settings** on the server as on your laptop: `PW_COSMOS_MODEL`, `PW_ULTRA_MODEL`, `PW_SPLITTER_MODEL`, `PW_SUPER_MODEL` and `PW_POLICY`. Copy the exact values from your `.env`.

## 1. Build the Docker image (on your laptop)

Install Docker Desktop. Build from the repo folder, so the image includes `demo/videos/dallas.mp4`, which is on your disk but not in git:

```powershell
docker build --platform linux/amd64 -t physics-witness:latest .
```

Test it locally first:

```powershell
docker run --rm -p 8000:8000 --env-file .env physics-witness:latest
# open http://127.0.0.1:8000: three pills green, three samples listed, your old claims on the home page
```

The image holds `dallas.mp4`, which comes from a YouTube compilation, so push it to a **private** registry only (Nebius Container Registry below), never to a public Docker Hub repo.

## 2. Install and log in to the Nebius CLI

The CLI's install script is for macOS/Linux. On Windows, run these steps in **WSL** (Ubuntu). Install Docker Desktop's WSL integration so `docker` works there too.

```bash
curl -sSL https://artifacts.nebius.cloud/cli/install.sh | bash
nebius profile create            # opens a browser to log in; pick your project
nebius config get parent-id      # project-...   (used below as $PROJECT)
export PROJECT=$(nebius config get parent-id)
```

The Serverless commands need CLI version 0.12.277 or newer (`nebius version`; update with `nebius update`).

## 3. Push the image to Nebius Container Registry

1. In the Nebius console, open **Container Registry**, create a registry (for example `physics-witness`) in region `eu-north1`, and copy its ID (looks like `e00...`).
2. Push:
   ```bash
   nebius registry configure-helper            # lets docker log in with your Nebius session
   export REG=cr.eu-north1.nebius.cloud/<registry-id>
   docker tag physics-witness:latest $REG/physics-witness:v1
   docker push $REG/physics-witness:v1
   ```

## 4. Put the secrets in MysteryBox

Plain `--env` values are stored in the endpoint spec, readable by anyone in the project. Secrets go in **MysteryBox** (console → MysteryBox → Create secret) and reach the container as environment variables.

Create one secret per value, naming each payload key after its environment variable:

| Secret name | Payload key | Value |
|---|---|---|
| `pw-nebius-key` | `NEBIUS_API_KEY` | your Token Factory key |
| `pw-tavily-key` | `TAVILY_API_KEY` | your Tavily key |
| `pw-supabase-key` | `SUPABASE_SERVICE_KEY` | the Supabase service_role key |

If the registry is private to your project and the endpoint can't pull the image, also create `pw-registry` with keys `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` and add `--registry-secret pw-registry` below.

## 5. Create the endpoint

Check it first with `--dry-run`, then run the same command without it:

```bash
nebius ai endpoint create \
  --parent-id $PROJECT \
  --name physics-witness \
  --image $REG/physics-witness:v1 \
  --platform cpu-d3 --preset 4vcpu-16gb \
  --container-port 8000/http \
  --auth none \
  --env PW_STORE=supabase \
  --env SUPABASE_URL=https://<your-project-ref>.supabase.co \
  --env SUPABASE_BUCKET=claims \
  --env PW_POLICY=quality \
  --env PW_COSMOS_MODEL=<same as your .env> \
  --env PW_ULTRA_MODEL=<same as your .env> \
  --env PW_SPLITTER_MODEL=<same as your .env> \
  --env PW_SUPER_MODEL=<same as your .env> \
  --env-secret NEBIUS_API_KEY=pw-nebius-key \
  --env-secret TAVILY_API_KEY=pw-tavily-key \
  --env-secret SUPABASE_SERVICE_KEY=pw-supabase-key \
  --dry-run
```

- `--auth none` is on purpose: judges open the URL in a browser, which can't send a bearer token. The service keys stay on the server and the app never exposes them.
- `--container-port 8000/http` gives a managed `https://` URL. Don't add `--public`.
- There is no in-place update. To ship a new version, push `:v2`, create a new endpoint, check it, then delete the old one.

Wait for it to start, then get the URL:

```bash
nebius ai endpoint get-by-name --name physics-witness --parent-id $PROJECT --format json \
  | jq '{id: .metadata.id, state: .status.state, urls: .status.public_endpoints}'
```

Always use that `jq` filter. Raw `get` output includes the spec, which holds your plain `--env` values.

## 6. Check the deployed site

1. Open the URL. The header should show **Token Factory: live**, **Tavily: live** and **Storage: Supabase**.
2. The home page lists the claims already in Supabase.
3. "No video? Try a sample": all three are listed. Run the liar sample. It should finish in seconds (cache hit) with "A train came from the left." marked **Contradicted**.
4. Upload a short clip (under 40 MB) with a story. It runs live, which takes about a minute.
5. `curl -s <url>/api/health` returns JSON with `"store": "supabase"`.

If it won't start, check the logs (no `--follow`; it never returns):

```bash
nebius ai endpoint logs <endpoint-id>
```

## 7. Keep it running, then tear down

- It bills until you **delete** it. `nebius ai endpoint stop <id>` pauses billing, and `start` resumes.
- Keep it up until judging ends (Dec 15). Afterwards: `nebius ai endpoint delete <id>`.
- Rotate the Nebius API key you shared during development before going public.

## Submission checklist

- [ ] Public repo with LICENSE (Apache 2.0) and README
- [ ] Demo URL (the endpoint) in the Devpost form
- [ ] Demo video (see [VIDEO_SCRIPT.md](VIDEO_SCRIPT.md)), uploaded to YouTube as public or unlisted
- [ ] List the NVIDIA models used (Nemotron 3.5 Super VL, Nemotron 3 Ultra, Nemotron 3 Nano, Nemotron 3 Super) and the Nebius services (Token Factory, Serverless)
- [ ] Submit before **Oct 30, 2026, 10:00 am PDT** (10:30 pm IST)
