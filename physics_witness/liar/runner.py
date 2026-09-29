"""Run the Liar Test and score it.

Each clip runs twice: once with the true story and once with the altered one.
The altered run reuses the cached Cosmos readings of the same clip, so it
costs roughly one extra claim split + cross-examination.

Scoring
  caught      the altered story got Contradicted on a claim that comes from the
              altered sentence (the lie was found where it was told)
  flagged     any claim in the story got Contradicted
  false alarm a TRUE story was flagged
"""

from __future__ import annotations

import csv
import difflib
import json
import math
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .cases import Case

KINDS = ("true", "altered")
MATCH_RATIO = 0.6


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", (s or "").lower())).strip()


def claim_matches_sentence(claim: dict[str, Any], sentence: str) -> bool:
    """Did this claim come from `sentence`? Uses the splitter's source sentence,
    falling back to fuzzy text similarity (the splitter may reword)."""
    target = _norm(sentence)
    if not target:
        return False
    for text in (claim.get("source", ""), claim.get("text", "")):
        t = _norm(text)
        if t and (t == target or t in target or target in t
                  or difflib.SequenceMatcher(None, t, target).ratio() >= MATCH_RATIO):
            return True
    return False


def score(case: Case, kind: str, report: dict[str, Any] | None, error: str | None = None,
          elapsed: float = 0.0) -> dict[str, Any]:
    row = {"id": case.id, "kind": kind, "alteration": case.alteration if kind == "altered" else "",
           "claims": 0, "supported": 0, "contradicted": 0, "cant_tell": 0,
           "flagged": False, "caught": None, "altered_claims": "", "tokens": 0, "usd": None,
           "elapsed_s": round(elapsed, 1), "mode": "", "error": error or ""}
    if report is None:
        return row
    verdicts = report["verdicts"]
    claims = {c["id"]: c for c in report["claims"]}
    row.update(claims=len(verdicts), mode=report.get("mode", ""),
               tokens=report.get("cost", {}).get("tokens", 0), usd=report.get("cost", {}).get("total_usd"),
               **{k: sum(v["verdict"] == k for v in verdicts) for k in ("supported", "contradicted", "cant_tell")})
    row["flagged"] = row["contradicted"] > 0
    if kind == "altered":
        hit = [v for v in verdicts if claim_matches_sentence(claims.get(v["claim_id"], {}), case.altered_sentence)]
        row["altered_claims"] = ";".join(f"{v['claim_id']}={v['verdict']}" for v in hit)
        row["caught"] = any(v["verdict"] == "contradicted" for v in hit)
    return row


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson interval: honest error bars for small n (30-40 clips)."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def _rate(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None, "ci95": wilson(k, n)}


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if not r["error"]]
    true = [r for r in ok if r["kind"] == "true"]
    altered = [r for r in ok if r["kind"] == "altered"]
    claims = sum(r["claims"] for r in ok)
    by_type: dict[str, dict[str, Any]] = {}
    for kind in sorted({r["alteration"] for r in altered}):
        sub = [r for r in altered if r["alteration"] == kind]
        by_type[kind] = {"caught": _rate(sum(bool(r["caught"]) for r in sub), len(sub)),
                         "flagged": _rate(sum(r["flagged"] for r in sub), len(sub))}
    priced = [r["usd"] for r in ok if r["usd"] is not None]
    return {
        "runs": len(rows), "errors": len(rows) - len(ok),
        "clips": len({r["id"] for r in ok}),
        "catch_rate": _rate(sum(bool(r["caught"]) for r in altered), len(altered)),
        "catch_rate_anywhere": _rate(sum(r["flagged"] for r in altered), len(altered)),
        "false_alarm_rate": _rate(sum(r["flagged"] for r in true), len(true)),
        "abstain_rate": _rate(sum(r["cant_tell"] for r in ok), claims),
        "abstain_rate_true": _rate(sum(r["cant_tell"] for r in true), sum(r["claims"] for r in true)),
        "by_alteration": by_type,
        "tokens": sum(r["tokens"] for r in ok),
        "usd_total": round(sum(priced), 6) if priced and len(priced) == len(ok) else None,
        "usd_per_claim": round(sum(priced) / claims, 6) if priced and len(priced) == len(ok) and claims else None,
        "tokens_per_claim": round(sum(r["tokens"] for r in ok) / claims, 1) if claims else None,
        "offline_runs": sum(r["mode"] == "offline-stub" for r in ok),
    }


Runner = Callable[..., dict[str, Any]]


def run_case(case: Case, clips_dir: Path, out_dir: Path, runner: Runner, *, force: bool = False,
             settings=None, **run_kwargs) -> list[dict[str, Any]]:
    """True story first, then altered, sequentially so the altered run hits the Cosmos cache."""
    rows = []
    for kind in KINDS:
        story = case.story(kind)
        if not story:
            continue
        run_dir = out_dir / "runs" / f"{case.id}-{kind}"
        report_path = run_dir / "report.json"
        started = time.monotonic()
        if report_path.is_file() and not force:
            rows.append(score(case, kind, json.loads(report_path.read_text())))
            continue
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            report = runner(Path(clips_dir) / case.clip, story, settings=settings, out_dir=run_dir,
                            place=case.place or None, date=case.date or None, **run_kwargs)
            rows.append(score(case, kind, report, elapsed=time.monotonic() - started))
        except Exception as exc:
            (run_dir / "error.log").write_text(traceback.format_exc())
            rows.append(score(case, kind, None, error=f"{type(exc).__name__}: {exc}",
                              elapsed=time.monotonic() - started))
    return rows


def run_all(cases: list[Case], clips_dir: Path, out_dir: Path, *, runner: Runner | None = None,
            workers: int = 2, force: bool = False, settings=None,
            on_row: Callable[[dict[str, Any]], None] | None = None, **run_kwargs) -> dict[str, Any]:
    if runner is None:
        from ..pipeline import run as runner
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    order = {c.id: i for i, c in enumerate(cases)}
    rows: list[dict[str, Any]] = []
    # Workers stay well under the 50-concurrent-operation Sandbox limit.
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        futures = [pool.submit(run_case, c, clips_dir, out_dir, runner, force=force, settings=settings, **run_kwargs)
                   for c in cases]
        for fut in as_completed(futures):
            for row in fut.result():
                rows.append(row)
                if on_row:
                    on_row(row)
    rows.sort(key=lambda r: (order[r["id"]], KINDS.index(r["kind"])))
    result = {"metrics": metrics(rows), "rows": rows,
              "cases": {c.id: {"clip": c.clip, "alteration": c.alteration, "altered_sentence": c.altered_sentence,
                               "true_story": c.true_story, "altered_story": c.altered_story,
                               "license": c.license, "source_url": c.source_url} for c in cases},
              "generated": time.strftime("%Y-%m-%d %H:%M:%S")}
    write_outputs(result, out_dir)
    return result


def write_outputs(result: dict[str, Any], out_dir: Path) -> None:
    from .report import render_html
    rows = result["rows"]
    with open(out_dir / "results.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["id"])
        w.writeheader()
        w.writerows(rows)
    (out_dir / "metrics.json").write_text(json.dumps(result["metrics"], indent=2))
    (out_dir / "results.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    (out_dir / "report.html").write_text(render_html(result))
