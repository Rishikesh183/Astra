"""Command line: `physics-witness checks|frames|run|add|evidence|liar|serve`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings


def _cmd_checks(args) -> int:
    from .checks import run_checks
    results = run_checks(Settings.from_env(), clip=args.clip, date=args.date, place=args.place)
    for r in results:
        label = "0 (setup)" if r.number == 0 else str(r.number)
        print(f"[{r.status:6}] {label:9} {r.name} ({r.seconds}s)\n          {r.detail}")
    hard = [r for r in results if r.number in (1, 2, 3, 4) and r.status == "FAIL"]
    print(f"\n{len(hard)} of checks 1-4 failed. Plan rule: switch ideas if two or more of the five fail.")
    return 1 if len(hard) >= 2 else 0


def _cmd_frames(args) -> int:
    from .frames import sample_frames, save_frames, windows
    frames = sample_frames(args.video, max_frames=args.max_frames, fps=args.fps)
    paths = save_frames(frames, args.out)
    print(f"{len(frames)} frames -> {args.out}  ({len(windows(frames))} windows of <=6)")
    for f, p in zip(frames, paths):
        print(f"  {f.label():>10}  motion={f.motion:6.2f}  {p.name}")
    return 0


def _cmd_run(args) -> int:
    from .pipeline import run
    story = Path(args.story).read_text() if Path(args.story).is_file() else args.story
    report = run(args.video, story, max_frames=args.max_frames, fps=args.fps, out_dir=args.out,
                 place=args.place, date=args.date)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    marks = {"supported": "SUPPORTED   ", "contradicted": "CONTRADICTED", "cant_tell": "CAN'T TELL  "}
    print(f"{report['video']}  ({report['duration_s']}s, {len(report['frames'])} frames, mode={report['mode']})\n")
    for v in report["verdicts"]:
        times = ", ".join(f"{t:.2f}s" for t in v["evidence_times"]) or "-"
        print(f"{marks[v['verdict']]}  {v['claim']}\n              at: {times}\n              why: {v['reasoning']}")
        if v["needed_evidence"]:
            print(f"              need: {v['needed_evidence']}")
    if report.get("request"):
        print(f"\nevidence request to the claimant:\n{report['request']['message']}")
    cost = report["cost"]
    usd = f"${cost['total_usd']:.4f}" if cost["total_usd"] is not None and cost["priced"] else "price not set"
    print(f"\nsummary: {report['summary']}  tokens: {cost['tokens']} ({cost['saved_tokens']} saved by cache)  cost: {usd}")
    if args.out:
        print(f"report written to {Path(args.out) / 'report.json'}")
    return 0


def _cmd_add(args) -> int:
    from .store import RunStore
    story = Path(args.story).read_text() if Path(args.story).is_file() else args.story
    store = RunStore(args.data)
    try:
        run_id = store.create(args.video, story, title=args.title, place=args.place or "", date=args.date or "", wait=True)
    finally:
        store.shutdown()
    meta = store.meta(run_id)
    print(f"{run_id}  status={meta['status']}  summary={meta.get('summary')}  {meta.get('error') or ''}".rstrip())
    return 0 if meta["status"] == "done" else 1


def _cmd_evidence(args) -> int:
    from .store import RunStore
    store = RunStore(args.data)
    try:
        sid = store.add_evidence(args.claim, args.video, args.note, wait=True)
    finally:
        store.shutdown()
    meta = store.meta(args.claim)
    report = store.report(args.claim)
    for c in report["rounds"][-1]["changes"]:
        print(f"{c['claim_id']}: {c['before']} -> {c['after']}  {c['claim']}")
    print(f"{args.claim} +{sid}  summary={meta.get('summary')}  {meta.get('error') or ''}".rstrip())
    return 0 if not meta.get("error") else 1


def _liar_cases(args):
    from .liar.cases import load_cases
    if not Path(args.cases).is_file():
        raise SystemExit(f"{args.cases} not found. Start from the template: cp liartest/cases.example.csv {args.cases}")
    cases = load_cases(args.cases)
    if getattr(args, "only", None):
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    if getattr(args, "limit", None):
        cases = cases[: args.limit]
    return cases


def _cmd_liar_check(args) -> int:
    from .liar.cases import validate
    cases = _liar_cases(args)
    errors, warnings = validate(cases, args.clips)
    for w in warnings:
        print(f"warning  {w}")
    for e in errors:
        print(f"ERROR    {e}")
    pairs = sum(1 for c in cases if c.altered_story)
    print(f"\n{len(cases)} clips, {pairs} with an altered story, {len(errors)} errors, {len(warnings)} warnings")
    if len(cases) < 30:
        print(f"note: the plan targets 30-40 clips; {len(cases)} so far")
    return 1 if errors else 0


def _cmd_liar_alter(args) -> int:
    import shutil

    from . import models
    from .clients.token_factory import TokenFactoryClient
    from .liar.alter import alter
    from .liar.cases import save_cases
    cases = _liar_cases(args)
    client = TokenFactoryClient(Settings.from_env())
    names = models.resolve(client)
    model = names.get("super") or names.get("ultra") or names.get("splitter") or ""
    changed = 0
    for c in cases:
        if c.altered_story and not args.overwrite:
            continue
        if not c.alteration:
            print(f"[{c.id}] skipped: set the alteration column first")
            continue
        got = alter(c.true_story, c.alteration, client, model)
        if not got:
            print(f"[{c.id}] no {c.alteration} change found; write this one by hand")
            continue
        c.altered_story, c.altered_sentence, method = got
        changed += 1
        print(f"[{c.id}] {c.alteration} ({method}): {c.altered_sentence}")
    if changed and not args.dry_run:
        shutil.copyfile(args.cases, f"{args.cases}.bak")
        save_cases(cases, args.cases)
        print(f"\n{changed} drafts written to {args.cases} (backup: {args.cases}.bak). Read every one before running.")
    elif changed:
        print(f"\n{changed} drafts (dry run, nothing written)")
    return 0


def _cmd_liar_run(args) -> int:
    from .liar.cases import validate
    from .liar.runner import run_all
    cases = _liar_cases(args)
    errors, _ = validate(cases, args.clips)
    if errors:
        for e in errors:
            print(f"ERROR    {e}")
        print("fix these first (see `physics-witness liar check`)")
        return 1
    settings = Settings.from_env()
    if settings.offline or not settings.nebius_api_key:
        print("warning: no NEBIUS_API_KEY (or PW_OFFLINE=1): runs will be offline stubs, not a real measurement\n")

    def show(row):
        extra = f" caught={row['caught']}" if row["kind"] == "altered" else f" flagged={row['flagged']}"
        print(f"  {row['id']:<16} {row['kind']:<8} S{row['supported']} C{row['contradicted']} ?{row['cant_tell']}"
              f"{extra}{'  ERROR ' + row['error'] if row['error'] else ''}")

    result = run_all(cases, args.clips, args.out, workers=args.workers, force=args.force, settings=settings,
                     on_row=show, max_frames=args.max_frames)
    m = result["metrics"]
    fmt = lambda r: "–" if r["rate"] is None else f"{r['rate'] * 100:.0f}% ({r['k']}/{r['n']}, 95% CI {r['ci95'][0] * 100:.0f}-{r['ci95'][1] * 100:.0f}%)"
    print(f"\ncatch rate    {fmt(m['catch_rate'])}")
    print(f"false alarms  {fmt(m['false_alarm_rate'])}")
    print(f"abstained     {fmt(m['abstain_rate'])}")
    print(f"per claim     {'$%.4f' % m['usd_per_claim'] if m['usd_per_claim'] is not None else str(m['tokens_per_claim']) + ' tokens'}")
    print(f"\nreport: {args.out / 'report.html'}")
    return 1 if m["errors"] else 0


def _cmd_liar_report(args) -> int:
    import json

    from .liar.runner import metrics, write_outputs
    result = json.loads((args.out / "results.json").read_text())
    result["metrics"] = metrics(result["rows"])
    write_outputs(result, args.out)
    print(args.out / "report.html")
    return 0


def _cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app
    uvicorn.run(create_app(args.data), host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="physics-witness", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("checks", help="run the five Day 1-2 kill-switch checks")
    c.add_argument("--clip", type=Path, help="short test clip used for checks 1-2")
    c.add_argument("--date", default="2026-09-01")
    c.add_argument("--place", default="Bengaluru, India")
    c.set_defaults(fn=_cmd_checks)

    f = sub.add_parser("frames", help="sample motion-ranked, timestamped frames from a clip")
    f.add_argument("video", type=Path)
    f.add_argument("--out", type=Path, default=Path("out/frames"))
    f.add_argument("--max-frames", type=int, default=16)
    f.add_argument("--fps", type=float, default=4.0)
    f.set_defaults(fn=_cmd_frames)

    r = sub.add_parser("run", help="cross-examine a story against a clip")
    r.add_argument("video", type=Path)
    r.add_argument("story", help="story text, or a path to a .txt file")
    r.add_argument("--out", type=Path, help="directory for report.json and frames")
    r.add_argument("--max-frames", type=int, default=16)
    r.add_argument("--fps", type=float, default=4.0)
    r.add_argument("--place", help="where it happened, for the Tavily weather/place lookup")
    r.add_argument("--date", help="when it happened (YYYY-MM-DD)")
    r.add_argument("--json", action="store_true", help="print the full report as JSON")
    r.set_defaults(fn=_cmd_run)

    a = sub.add_parser("add", help="run a claim and save it where the web app shows it")
    a.add_argument("video", type=Path)
    a.add_argument("story", help="story text, or a path to a .txt file")
    a.add_argument("--title", default="")
    a.add_argument("--place")
    a.add_argument("--date")
    a.add_argument("--data", type=Path, default=Path("data/claims"))
    a.set_defaults(fn=_cmd_add)

    e = sub.add_parser("evidence", help="attach extra footage to a saved claim and re-judge Can't tell claims")
    e.add_argument("claim", help="claim id (see data/claims/)")
    e.add_argument("video", type=Path)
    e.add_argument("--note", default="", help="what the claimant says the clip shows")
    e.add_argument("--data", type=Path, default=Path("data/claims"))
    e.set_defaults(fn=_cmd_evidence)

    liar = sub.add_parser("liar", help="the Liar Test: true vs altered stories on the same clips")
    lsub = liar.add_subparsers(dest="liar_cmd", required=True)

    def liar_common(sp, out=False):
        sp.add_argument("--cases", type=Path, default=Path("liartest/cases.csv"))
        sp.add_argument("--clips", type=Path, default=Path("liartest/clips"))
        if out:
            sp.add_argument("--out", type=Path, default=Path("liartest/results"))

    lc = lsub.add_parser("check", help="validate the case file and clips")
    liar_common(lc)
    lc.add_argument("--only", help="comma-separated case ids")
    lc.set_defaults(fn=_cmd_liar_check)

    la = lsub.add_parser("alter", help="draft altered stories for rows that have none")
    liar_common(la)
    la.add_argument("--overwrite", action="store_true", help="redo rows that already have an altered story")
    la.add_argument("--dry-run", action="store_true")
    la.set_defaults(fn=_cmd_liar_alter)

    lr = lsub.add_parser("run", help="run every clip with both stories and score them")
    liar_common(lr, out=True)
    lr.add_argument("--workers", type=int, default=2)
    lr.add_argument("--force", action="store_true", help="re-run pairs that already have a report")
    lr.add_argument("--only", help="comma-separated case ids")
    lr.add_argument("--limit", type=int, help="first N cases only")
    lr.add_argument("--max-frames", type=int, default=16)
    lr.set_defaults(fn=_cmd_liar_run)

    lp = lsub.add_parser("report", help="rebuild metrics and report.html from results.json")
    lp.add_argument("--out", type=Path, default=Path("liartest/results"))
    lp.set_defaults(fn=_cmd_liar_report)

    w = sub.add_parser("serve", help="start the web app")
    w.add_argument("--data", type=Path, default=Path("data/claims"))
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(fn=_cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
