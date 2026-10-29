"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so the CLI works without extra dependencies."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]           # KEY="v1.abc" and KEY='v1.abc' both work
        os.environ.setdefault(key.strip(), value)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _samples_file(configured: str) -> Path:
    """PW_SAMPLES if set; otherwise demo/samples.json in the current folder,
    falling back to the one next to the package (a checkout started elsewhere)."""
    if configured:
        return Path(configured)
    local = Path("demo/samples.json")
    bundled = PACKAGE_ROOT / "demo" / "samples.json"
    return local if local.is_file() or not bundled.is_file() else bundled


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


@dataclass
class Settings:
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1"
    cosmos_model: str = ""
    ultra_model: str = ""
    splitter_model: str = ""
    super_model: str = ""
    policy: str = "quality"
    prices_file: Path = field(default_factory=lambda: Path("pricing.json"))
    samples_file: Path = field(default_factory=lambda: Path("demo/samples.json"))
    store: str = "files"                 # files | supabase
    supabase_url: str = ""
    supabase_key: str = ""               # the service_role key (server side only)
    supabase_bucket: str = "claims"
    tavily_api_key: str = ""
    offline: bool = False
    cache_dir: Path = field(default_factory=lambda: Path(".cache/responses"))
    request_timeout: float = 120.0

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        env = os.environ.get
        return cls(
            nebius_api_key=env("NEBIUS_API_KEY", ""),
            nebius_base_url=env("NEBIUS_BASE_URL", "") or cls.nebius_base_url,
            cosmos_model=env("PW_COSMOS_MODEL", ""),
            ultra_model=env("PW_ULTRA_MODEL", ""),
            splitter_model=env("PW_SPLITTER_MODEL", ""),
            super_model=env("PW_SUPER_MODEL", ""),
            policy=(env("PW_POLICY", "") or "quality").strip().lower(),
            prices_file=Path(env("PW_PRICES_FILE", "") or "pricing.json"),
            samples_file=_samples_file(env("PW_SAMPLES", "")),
            store=(env("PW_STORE", "") or "files").strip().lower(),
            supabase_url=env("SUPABASE_URL", ""),
            supabase_key=env("SUPABASE_SERVICE_KEY", ""),
            supabase_bucket=env("SUPABASE_BUCKET", "") or "claims",
            tavily_api_key=env("TAVILY_API_KEY", ""),
            offline=_flag("PW_OFFLINE"),
            cache_dir=Path(env("PW_CACHE_DIR", "") or ".cache/responses"),
        )
