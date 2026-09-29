import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_sample_clip import make_clip  # noqa: E402


@pytest.fixture(scope="session")
def clip(tmp_path_factory) -> Path:
    return make_clip(tmp_path_factory.mktemp("clips") / "sample.mp4", 10.0)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    for k in ("NEBIUS_API_KEY", "TAVILY_API_KEY", "PW_COSMOS_MODEL", "PW_ULTRA_MODEL",
              "PW_SPLITTER_MODEL", "PW_SUPER_MODEL", "PW_OFFLINE", "NEBIUS_BASE_URL", "PW_POLICY",
              "PW_PRICES_FILE", "PW_CACHE_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)  # keep .cache/ and .env lookups out of the repo
