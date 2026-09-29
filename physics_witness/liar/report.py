"""Self-contained HTML report for a Liar Test run (open it in any browser;
it doubles as the Liar Test scene in the demo video)."""

from __future__ import annotations

from html import escape
from typing import Any


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def _rate_card(title: str, r: dict[str, Any], tone: str, blurb: str) -> str:
    """tone: good (higher is better), bad (lower is better) or neutral."""
    ci = r.get("ci95")
    bar = ""
    if r["rate"] is not None:
        lo, hi = ci
        bar = (f'<div class="bar"><div class="fill {tone}" style="width:{r["rate"] * 100:.1f}%"></div>'
               f'<div class="ci" style="left:{lo * 100:.1f}%;width:{(hi - lo) * 100:.1f}%"></div></div>'
               f'<div class="muted small">95% interval {_pct(lo)}–{_pct(hi)}</div>')
    return (f'<div class="card"><div class="label">{escape(title)}</div><div class="big">{_pct(r["rate"])}</div>'
            f'<div class="muted">{r["k"]} of {r["n"]}</div>{bar}<p class="small">{escape(blurb)}</p></div>')


def _verdicts(row: dict[str, Any] | None) -> str:
    if row is None:
        return '<span class="muted">not run</span>'
    if row["error"]:
        return f'<span class="err">error: {escape(row["error"][:120])}</span>'
    return (f'<span class="v s">{row["supported"]} supported</span> <span class="v c">{row["contradicted"]} contradicted</span> '
            f'<span class="v u">{row["cant_tell"]} can\'t tell</span>')


def render_html(result: dict[str, Any]) -> str:
    m = result["metrics"]
    rows = result["rows"]
    by = {(r["id"], r["kind"]): r for r in rows}
    cases = result.get("cases", {})

    cost = (f"${m['usd_per_claim']:.4f}" if m["usd_per_claim"] is not None
            else f"{m['tokens_per_claim'] or 0:,.0f} tokens")
    cost_note = ("per claim checked" if m["usd_per_claim"] is not None
                 else "per claim checked (add pricing.json for dollars)")
    warn = ""
    if m["offline_runs"]:
        warn = (f'<div class="warn">{m["offline_runs"]} of {m["runs"] - m["errors"]} runs were offline stubs '
                f'(no NEBIUS_API_KEY): every verdict is Can\'t tell, so these numbers are not a real measurement.</div>')
    if m["errors"]:
        warn += f'<div class="warn bad">{m["errors"]} run(s) failed; see the table and each run\'s error.log.</div>'

    type_rows = "".join(
        f"<tr><td>{escape(k.replace('_', ' '))}</td><td class=num>{v['caught']['n']}</td>"
        f"<td class=num>{_pct(v['caught']['rate'])}</td><td class='num muted'>{_pct(v['caught']['ci95'][0]) if v['caught']['ci95'] else '–'}–{_pct(v['caught']['ci95'][1]) if v['caught']['ci95'] else '–'}</td>"
        f"<td class=num>{_pct(v['flagged']['rate'])}</td></tr>"
        for k, v in m["by_alteration"].items())

    case_rows = []
    for cid, c in cases.items():
        t, a = by.get((cid, "true")), by.get((cid, "altered"))
        if a is None or a["error"]:
            caught = '<span class="muted">–</span>'
        else:
            caught = '<span class="yes">caught</span>' if a["caught"] else '<span class="no">missed</span>'
        alarm = ('<span class="no">false alarm</span>' if t and not t["error"] and t["flagged"]
                 else '<span class="muted">ok</span>' if t and not t["error"] else "")
        links = " · ".join(f'<a href="runs/{escape(cid)}-{k}/report.json">{k}</a>' for k in ("true", "altered") if by.get((cid, k)))
        case_rows.append(
            f"<tr><td><strong>{escape(cid)}</strong><div class='small muted'>{escape(c.get('clip', ''))} · {escape(c.get('license', ''))}</div></td>"
            f"<td>{escape((c.get('alteration') or '–').replace('_', ' '))}</td>"
            f"<td class=small>{escape(c.get('altered_sentence') or '')}"
            f"{'<div class=small muted>altered claims: ' + escape(a['altered_claims']) + '</div>' if a and a['altered_claims'] else ''}</td>"
            f"<td>{caught}<div class=small>{_verdicts(a)}</div></td>"
            f"<td>{alarm}<div class=small>{_verdicts(t)}</div></td>"
            f"<td class=small>{links}</td></tr>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Liar Test Results</title>
<style>
:root {{ --bg:#f5f6f8; --panel:#fff; --ink:#16181d; --muted:#5d6470; --line:#e2e5ea; --soft:#eef0f3;
  --ok:#1f8a4c; --ok-bg:#e3f4ea; --bad:#c2352b; --bad-bg:#fbe6e4; --unk:#9a6a00; --unk-bg:#fbf1d9; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --panel:#171a21; --ink:#e8eaee; --muted:#9aa3b2; --line:#2a2f3a;
  --soft:#1f232c; --ok:#4cc27f; --ok-bg:#16301f; --bad:#ff7a70; --bad-bg:#3a1a18; --unk:#e2b44d; --unk-bg:#33280f; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:24px 16px 40px; }}
h1 {{ margin:0 0 4px; font-size:22px; }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); margin:28px 0 10px; }}
.muted {{ color:var(--muted); }} .small {{ font-size:12px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-top:16px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
.label {{ font-size:12px; text-transform:uppercase; letter-spacing:.5px; color:var(--muted); font-weight:600; }}
.big {{ font-size:34px; font-weight:700; font-variant-numeric:tabular-nums; }}
.bar {{ position:relative; height:8px; background:var(--soft); border-radius:4px; margin:10px 0 4px; }}
.fill {{ position:absolute; left:0; top:0; bottom:0; border-radius:4px; }} .fill.good {{ background:var(--ok); }} .fill.bad {{ background:var(--bad); }} .fill.neutral {{ background:var(--unk); }}
.ci {{ position:absolute; top:-3px; height:14px; border:2px solid var(--ink); border-top:0; border-bottom:0; opacity:.5; }}
.warn {{ background:var(--unk-bg); color:var(--unk); border-radius:10px; padding:10px 14px; margin-top:16px; }}
.warn.bad {{ background:var(--bad-bg); color:var(--bad); }}
.tablewrap {{ overflow-x:auto; background:var(--panel); border:1px solid var(--line); border-radius:12px; }}
table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ font-size:12px; color:var(--muted); font-weight:600; }} td.num,th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.yes {{ color:var(--ok); font-weight:700; }} .no {{ color:var(--bad); font-weight:700; }} .err {{ color:var(--bad); }}
.v {{ white-space:nowrap; }} .v.s {{ color:var(--ok); }} .v.c {{ color:var(--bad); }} .v.u {{ color:var(--unk); }}
a {{ color:inherit; }}
</style></head><body><main>
<h1>Liar Test</h1>
<div class="muted">{m['clips']} clips · {m['runs']} runs · generated {escape(result.get('generated', ''))}</div>
{warn}
<div class="cards">
{_rate_card("Catch rate", m["catch_rate"], "good", "Altered stories where the changed sentence was marked Contradicted.")}
{_rate_card("False alarms", m["false_alarm_rate"], "bad", "True stories with any claim marked Contradicted. Lower is better.")}
{_rate_card("Abstained", m["abstain_rate"], "neutral", "Claims marked Can't tell. Abstaining on footage that can't settle a claim is intended.")}
<div class="card"><div class="label">Cost</div><div class="big">{cost}</div><div class="muted">{cost_note}</div>
<p class="small">{m['tokens']:,} tokens in total. Altered runs reuse the cached Cosmos readings of the same clip.</p></div>
</div>
<p class="small muted">Also flagged anywhere in an altered story: {_pct(m['catch_rate_anywhere']['rate'])} ({m['catch_rate_anywhere']['k']} of {m['catch_rate_anywhere']['n']}). Intervals are 95% Wilson intervals; with a few dozen clips they are wide, and that is the honest picture.</p>

<h2>By alteration</h2>
<div class="tablewrap"><table><tr><th>Alteration</th><th class=num>Clips</th><th class=num>Caught</th><th class=num>95% interval</th><th class=num>Flagged anywhere</th></tr>
{type_rows or '<tr><td colspan=5 class=muted>No altered stories yet.</td></tr>'}</table></div>

<h2>Every clip</h2>
<div class="tablewrap"><table><tr><th>Clip</th><th>Alteration</th><th>Changed sentence</th><th>Altered story</th><th>True story</th><th>Reports</th></tr>
{''.join(case_rows)}</table></div>
<p class="small muted">Physics Witness gives evidence to a human adjuster. It is not a fraud detector.</p>
</main></body></html>"""
