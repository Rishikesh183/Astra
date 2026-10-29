#!/bin/sh
# Start Physics Witness on a stock python:3.11-slim container, with no image to build.
# Nebius Serverless injects this file (--inject-file) and runs it (--container-command sh --args /start.sh).
# It downloads the repo from GitHub, installs it and serves on port 8000. Claims, videos and the
# model cache live in Supabase (PW_STORE=supabase), so the container's disk is only scratch space.
set -eu

REPO="${PW_GIT_REPO:-Rishikesh183/Astra}"
REF="${PW_GIT_REF:-develop}"                 # branch, tag or commit to run
APP_DIR="${PW_APP_DIR:-/app}"
DATA_DIR="${PW_DATA_DIR:-/data}"
PORT="${PORT:-8000}"

echo "physics-witness: downloading $REPO@$REF"
python - "$REPO" "$REF" "$APP_DIR" <<'PY'
import io, shutil, sys, urllib.request, zipfile
from pathlib import Path
repo, ref, app = sys.argv[1], sys.argv[2], Path(sys.argv[3])
data = urllib.request.urlopen(f"https://codeload.github.com/{repo}/zip/{ref}", timeout=120).read()
tmp = app.with_name(app.name + ".download")
shutil.rmtree(tmp, ignore_errors=True)
zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
(top,) = list(tmp.iterdir())                 # GitHub zips hold one top folder, e.g. Astra-develop
shutil.rmtree(app, ignore_errors=True)
top.rename(app)
tmp.rmdir()
PY

echo "physics-witness: installing"
pip install --no-cache-dir --quiet --disable-pip-version-check "$APP_DIR"

mkdir -p "$DATA_DIR"
cd "$APP_DIR"
export PW_SAMPLES="${PW_SAMPLES:-$APP_DIR/demo/samples.json}"
export PW_CACHE_DIR="${PW_CACHE_DIR:-$DATA_DIR/cache}"
echo "physics-witness: serving on port $PORT"
exec physics-witness serve --host 0.0.0.0 --port "$PORT" --data "$DATA_DIR/claims"
