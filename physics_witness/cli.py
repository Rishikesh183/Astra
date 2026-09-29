"""Command line: `physics-witness checks|frames|run|add|evidence|serve`."""

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

    w = sub.add_parser("serve", help="start the web app")
    w.add_argument("--data", type=Path, default=Path("data/claims"))
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(fn=_cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
