"""Cost meter: turns ledger rows into dollars using prices you supply.

Prices are not hard-coded: copy pricing.example.json to pricing.json and fill
it in from https://tokenfactory.nebius.com/organization/prices (USD per 1M
tokens). Keys match model IDs case-insensitively by substring; the longest
matching key wins. Models without a price show tokens only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "saved_prompt_tokens", "saved_completion_tokens", "calls", "cached_calls")


def load_prices(path: Path | None) -> dict[str, dict[str, float]]:
    if not path or not Path(path).is_file():
        return {}
    raw = json.loads(Path(path).read_text())
    out = {}
    for key, val in raw.items():
        if key.startswith("_") or not isinstance(val, dict):
            continue
        if isinstance(val.get("input"), (int, float)) and isinstance(val.get("output"), (int, float)):
            out[key.lower()] = {"input": float(val["input"]), "output": float(val["output"])}
    return out


def price_for(model: str, prices: dict[str, dict[str, float]]) -> dict[str, float] | None:
    low = (model or "").lower()
    matches = [k for k in prices if k in low]
    return prices[max(matches, key=len)] if matches else None


def merge_rows(*row_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for rows in row_lists:
        for r in rows or []:
            m = merged.setdefault((r["task"], r["model"]), {"task": r["task"], "model": r["model"], **{f: 0 for f in TOKEN_FIELDS}})
            for f in TOKEN_FIELDS:
                m[f] += int(r.get(f, 0) or 0)
    return list(merged.values())


def _usd(p: dict[str, float] | None, prompt: int, completion: int) -> float | None:
    if p is None:
        return None
    return (prompt * p["input"] + completion * p["output"]) / 1_000_000


def estimate(rows: list[dict[str, Any]], prices: dict[str, dict[str, float]]) -> dict[str, Any]:
    """Spend and savings per task and in total. total_usd is None only when
    tokens were spent on a model with no price."""
    by_task: dict[str, dict[str, Any]] = {}
    total, saved, unpriced = 0.0, 0.0, set()
    tokens = saved_tokens = 0
    for r in rows:
        p = price_for(r["model"], prices)
        spent = r["prompt_tokens"] + r["completion_tokens"]
        spent_usd = _usd(p, r["prompt_tokens"], r["completion_tokens"])
        saved_usd = _usd(p, r["saved_prompt_tokens"], r["saved_completion_tokens"])
        if p is None and spent and r["model"] != "offline-stub":
            unpriced.add(r["model"])
        t = by_task.setdefault(r["task"], {"calls": 0, "cached_calls": 0, "tokens": 0, "usd": 0.0, "models": []})
        t["calls"] += r["calls"]
        t["cached_calls"] += r["cached_calls"]
        t["tokens"] += spent
        if spent_usd is None:
            t["usd"] = None if spent else t["usd"]
        elif t["usd"] is not None:
            t["usd"] += spent_usd
        if r["model"] not in t["models"]:
            t["models"].append(r["model"])
        tokens += spent
        saved_tokens += r["saved_prompt_tokens"] + r["saved_completion_tokens"]
        total += spent_usd or 0.0
        saved += saved_usd or 0.0
    return {
        "priced": bool(prices),
        "total_usd": None if unpriced else round(total, 6),
        "known_usd": round(total, 6),
        "saved_usd": round(saved, 6),
        "tokens": tokens,
        "saved_tokens": saved_tokens,
        "unpriced_models": sorted(unpriced),
        "by_task": by_task,
    }
