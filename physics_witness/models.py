"""Pick model names from the Token Factory /models listing when not set in env."""

from __future__ import annotations

from .clients.token_factory import TokenFactoryClient


def _find(models: list[str], *must: str, any_of: tuple[str, ...] = ()) -> str:
    for m in models:
        low = m.lower()
        if all(k in low for k in must) and (not any_of or any(k in low for k in any_of)):
            return m
    return ""


def discover(models: list[str]) -> dict[str, str]:
    return {
        "cosmos": _find(models, "cosmos", any_of=("reason",)) or _find(models, "cosmos"),
        "ultra": _find(models, "nemotron", "ultra"),
        "splitter": _find(models, "nemotron", "nano") or _find(models, "nemotron", "super"),
        "super": _find(models, "nemotron", "super"),
    }


def resolve(client: TokenFactoryClient) -> dict[str, str]:
    s = client.settings
    chosen = {"cosmos": s.cosmos_model, "ultra": s.ultra_model, "splitter": s.splitter_model,
              "super": s.super_model}
    if client.offline or all(chosen.values()):
        return chosen
    found = discover(client.list_models())
    return {k: v or found[k] for k, v in chosen.items()}
