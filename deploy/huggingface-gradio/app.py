"""Hugging Face Space (Gradio SDK, free CPU) entry point for Physics Witness.

The Space only runs `python app.py`; Gradio itself isn't used. This downloads the repo from
GitHub (for the demo samples and price table), then serves the FastAPI app on port 7860.
The package itself is installed from requirements.txt. Claims, videos and the model cache
live in Supabase (PW_STORE=supabase), so /tmp is only scratch space.
"""

import io
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = os.environ.get("PW_GIT_REPO", "Rishikesh183/Astra")
REF = os.environ.get("PW_GIT_REF", "develop")
APP = Path("/tmp/pw-app")
DATA = Path("/tmp/pw-data")

print(f"physics-witness: downloading {REPO}@{REF}", flush=True)
data = urllib.request.urlopen(f"https://codeload.github.com/{REPO}/zip/{REF}", timeout=120).read()
tmp = APP.with_name(APP.name + ".download")
shutil.rmtree(tmp, ignore_errors=True)
zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
(top,) = list(tmp.iterdir())
shutil.rmtree(APP, ignore_errors=True)
top.rename(APP)
tmp.rmdir()

DATA.mkdir(parents=True, exist_ok=True)
os.chdir(APP)
os.environ.setdefault("PW_SAMPLES", str(APP / "demo" / "samples.json"))
os.environ.setdefault("PW_CACHE_DIR", str(DATA / "cache"))

from physics_witness.cli import main  # noqa: E402  (installed from requirements.txt)

print("physics-witness: serving on port 7860", flush=True)
sys.exit(main(["serve", "--host", "0.0.0.0", "--port", "7860", "--data", str(DATA / "claims")]))
