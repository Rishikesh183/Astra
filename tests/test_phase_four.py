"""Phase 4: Liar Test tooling (cases, alterations, runner, scoring, report) and .env loading."""

import csv
import json
import shutil
from pathlib import Path

import pytest

from physics_witness import cli
from physics_witness.config import Settings
from physics_witness.liar.alter import alter, rule_alter
from physics_witness.liar.cases import Case, load_cases, save_cases, validate
from physics_witness.liar.runner import claim_matches_sentence, metrics, run_all, score, wilson

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "liartest" / "cases.example.csv"
TRUE = "I was waiting at the red light. The car behind me hit my rear bumper. It was a sunny afternoon."
ALTERED = "I was waiting at the red light. The car behind me hit my front bumper. It was a sunny afternoon."


def make_cases(tmp_path, clip, n=4):
    clips = tmp_path / "clips"
    clips.mkdir()
    cases = []
    for i in range(n):
        shutil.copy(clip, clips / f"c{i}.mp4")
        kind = ["contact", "weather", "direction", "contact"][i % 4]
        cases.append(Case(id=f"case-{i}", clip=f"c{i}.mp4", true_story=TRUE, altered_story=ALTERED,
                          alteration=kind, altered_sentence="The car behind me hit my front bumper.",
                          license="self-filmed"))
    return cases, clips


def fake_report(story, contradict=(), cant=()):
    sentences = [s.strip() for s in story.replace(". ", ".\n").splitlines() if s.strip()]
    claims = [{"id": f"c{i}", "text": s, "kind": "other", "source": s} for i, s in enumerate(sentences, 1)]
    verdicts = []
    for c in claims:
        v = "contradicted" if any(w in c["text"] for w in contradict) else "cant_tell" if any(w in c["text"] for w in cant) else "supported"
        verdicts.append({"claim_id": c["id"], "verdict": v, "claim": c["text"]})
    return {"claims": claims, "verdicts": verdicts, "mode": "live", "cost": {"tokens": 100, "total_usd": 0.01}}


# --- cases -------------------------------------------------------------------

def test_example_case_file_parses_and_validates(tmp_path):
    cases = load_cases(EXAMPLE)
    assert [c.id for c in cases] == ["dash-001", "dash-002", "dash-003"]
    assert cases[0].altered_sentence in cases[0].altered_story
    clips = tmp_path / "clips"
    clips.mkdir()
    for c in cases:
        (clips / c.clip).write_bytes(b"x")
    errors, warnings = validate(cases, clips)
    assert errors == []
    assert any("dash-002" in w and "altered_story" in w for w in warnings)
    assert any("source_url" in w for w in warnings) is False      # example rows are complete


def test_validate_catches_bad_rows(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    good = dict(clip="a.mp4", true_story=TRUE, altered_story=ALTERED, alteration="contact",
                altered_sentence="The car behind me hit my front bumper.", license="self-filmed")
    cases = [
        Case(id="ok", **good),
        Case(id="ok", **good),                                                        # duplicate id
        Case(id="bad id!", **good),
        Case(id="nofile", **{**good, "clip": "missing.mp4"}),
        Case(id="txt", **{**good, "clip": "notes.txt"}),
        Case(id="same", **{**good, "altered_story": TRUE}),
        Case(id="type", **{**good, "alteration": "vibes"}),
        Case(id="sent", **{**good, "altered_sentence": "Something else entirely."}),
        Case(id="lic", **{**good, "license": ""}),
        Case(id="url", **{**good, "license": "CC-BY-4.0"}),
    ]
    errors, warnings = validate(cases, tmp_path)
    text = "\n".join(errors)
    for needle in ("[ok] duplicate id", "[bad id!] id must", "[nofile] clip not found", "[txt] notes.txt: not a video",
                   "[same] altered_story is identical", "[type] alteration must", "[sent] altered_sentence is not one",
                   "[lic] license is empty"):
        assert needle in text, needle
    assert any("[url]" in w and "source_url" in w for w in warnings)


def test_save_and_reload_roundtrip(tmp_path):
    cases = load_cases(EXAMPLE)
    save_cases(cases, tmp_path / "out.csv")
    assert load_cases(tmp_path / "out.csv") == cases
    header = next(csv.reader(open(tmp_path / "out.csv")))
    assert header[:6] == ["id", "clip", "true_story", "altered_story", "alteration", "altered_sentence"]


# --- alterations -------------------------------------------------------------

@pytest.mark.parametrize("kind,sentence", [
    ("contact", "The car behind me hit my front bumper."),
    ("time_of_day", "It was a sunny night."),
])
def test_rule_alter_changes_one_sentence(kind, sentence):
    altered, changed = rule_alter(TRUE, kind)
    assert changed == sentence and altered != TRUE
    assert sum(a != b for a, b in zip(altered.split(". "), TRUE.split(". "))) in (1, 2)   # order swaps two
    assert rule_alter("Nothing to swap here.", "direction") is None


def test_order_swaps_two_actions_only():
    story = "I was at the junction. The van ahead braked hard. I braked a second later. It was sunny."
    altered, moved = rule_alter(story, "order")
    assert moved == "I braked a second later."
    assert altered == "I was at the junction. I braked a second later. The van ahead braked hard. It was sunny."
    assert rule_alter(TRUE, "order") is None         # only one action sentence: write it by hand


def test_alter_prefers_model_and_rejects_bad_model_output(tmp_path):
    class FakeClient:
        offline = False

        def __init__(self, reply):
            self.reply = reply

        def chat(self, *a, **k):
            return type("R", (), {"text": self.reply})()

    good = json.dumps({"altered_story": TRUE.replace("sunny afternoon", "rainy afternoon"),
                       "altered_sentence": "It was a rainy afternoon."})
    assert alter(TRUE, "weather", FakeClient(good), "m") == (TRUE.replace("sunny afternoon", "rainy afternoon"),
                                                             "It was a rainy afternoon.", "model")
    two_changes = json.dumps({"altered_story": ALTERED.replace("sunny", "rainy"), "altered_sentence": "It was a rainy afternoon."})
    got = alter(TRUE, "contact", FakeClient(two_changes), "m")
    assert got[2] == "rules" and got[1] == "The car behind me hit my front bumper."      # fell back to rules
    assert alter(TRUE, "contact", FakeClient("not json"), "m")[2] == "rules"


# --- scoring -------------------------------------------------------------------

def test_claim_matching_is_forgiving_of_rewording():
    s = "The car behind me hit my front bumper."
    assert claim_matches_sentence({"source": s, "text": "x"}, s)
    assert claim_matches_sentence({"source": "", "text": "the car behind me hit my front bumper"}, s)
    assert claim_matches_sentence({"source": "", "text": "Car behind hit my front bumper"}, s)
    assert not claim_matches_sentence({"source": "It was a sunny afternoon.", "text": "sunny"}, s)


def test_score_and_metrics():
    case = Case(id="a", clip="a.mp4", true_story=TRUE, altered_story=ALTERED, alteration="contact",
                altered_sentence="The car behind me hit my front bumper.")
    caught = score(case, "altered", fake_report(ALTERED, contradict=("front",)))
    wrong_place = score(case, "altered", fake_report(ALTERED, contradict=("red light",)))
    clean = score(case, "true", fake_report(TRUE))
    alarm = score(case, "true", fake_report(TRUE, contradict=("sunny",), cant=("red",)))
    failed = score(case, "altered", None, error="RuntimeError: boom")
    assert caught["caught"] is True and caught["altered_claims"] == "c2=contradicted"
    assert wrong_place["caught"] is False and wrong_place["flagged"] is True
    assert clean["caught"] is None and clean["flagged"] is False
    assert alarm["flagged"] is True and alarm["cant_tell"] == 1

    m = metrics([caught, wrong_place, clean, alarm, failed])
    assert m["catch_rate"]["k"] == 1 and m["catch_rate"]["n"] == 2
    assert m["catch_rate_anywhere"]["rate"] == 1.0
    assert m["false_alarm_rate"] == {"k": 1, "n": 2, "rate": 0.5, "ci95": wilson(1, 2)}
    assert m["abstain_rate"]["k"] == 1 and m["abstain_rate"]["n"] == 12
    assert m["errors"] == 1 and m["by_alteration"]["contact"]["caught"]["n"] == 2
    assert m["usd_per_claim"] == pytest.approx(0.04 / 12, abs=1e-6)


def test_wilson_interval():
    assert wilson(0, 0) is None
    lo, hi = wilson(30, 40)
    assert 0.59 < lo < 0.61 and 0.85 < hi < 0.87          # 75% of 40: roughly 60-86%
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == 1.0


# --- runner --------------------------------------------------------------------

def test_run_all_with_scripted_runner_resumes_and_writes_outputs(clip, tmp_path):
    cases, clips = make_cases(tmp_path, clip)
    calls = []

    def runner(video, story, *, settings, out_dir, place, date, **kw):
        calls.append((Path(video).name, story == TRUE))
        if Path(video).name == "c3.mp4" and story == ALTERED:
            raise RuntimeError("Token Factory 402")
        # case-1 fools the tool; everything else is caught; the true story of case-2 gets a false alarm
        name = Path(video).name
        if story == ALTERED:
            report = fake_report(story, contradict=() if name == "c1.mp4" else ("front",))
        else:
            report = fake_report(story, contradict=("sunny",) if name == "c2.mp4" else ())
        (Path(out_dir) / "report.json").write_text(json.dumps(report))
        return report

    out = tmp_path / "results"
    result = run_all(cases, clips, out, runner=runner, workers=3)
    m = result["metrics"]
    assert (m["catch_rate"]["k"], m["catch_rate"]["n"]) == (2, 3)
    assert (m["false_alarm_rate"]["k"], m["false_alarm_rate"]["n"]) == (1, 4)
    assert m["errors"] == 1
    assert [(r["id"], r["kind"]) for r in result["rows"]][:2] == [("case-0", "true"), ("case-0", "altered")]
    # the true story always runs before the altered one for the same clip (Cosmos cache reuse)
    for name in ("c0.mp4", "c1.mp4", "c2.mp4"):
        seq = [t for n, t in calls if n == name]
        assert seq == [True, False]
    for f in ("results.csv", "metrics.json", "results.json", "report.html"):
        assert (out / f).is_file()
    html = (out / "report.html").read_text()
    assert "Catch rate" in html and "67%" in html and "Token Factory 402" in html
    assert "<script" not in html

    # Re-run: finished pairs are read back, only the failed one runs again.
    calls.clear()
    run_all(cases, clips, out, runner=runner)
    assert calls == [("c3.mp4", False)]


def test_liar_cli_end_to_end_offline(clip, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PW_OFFLINE", "1")
    cases, clips = make_cases(tmp_path, clip, n=2)
    cases[1].altered_story = cases[1].altered_sentence = ""
    cases[1].alteration = "time_of_day"
    save_cases(cases, tmp_path / "cases.csv")
    args = ["--cases", str(tmp_path / "cases.csv"), "--clips", str(clips)]

    assert cli.main(["liar", "check", *args]) == 0
    assert cli.main(["liar", "alter", *args]) == 0
    assert "It was a sunny night." in load_cases(tmp_path / "cases.csv")[1].altered_story
    assert (tmp_path / "cases.csv.bak").is_file()

    out = tmp_path / "res"
    assert cli.main(["liar", "run", *args, "--out", str(out), "--max-frames", "6"]) == 0
    text = capsys.readouterr().out
    assert "offline stubs" in text and "catch rate" in text
    metrics_ = json.loads((out / "metrics.json").read_text())
    assert metrics_["offline_runs"] == 4 and metrics_["catch_rate"]["k"] == 0
    assert "not a real measurement" in (out / "report.html").read_text()
    assert cli.main(["liar", "report", "--out", str(out)]) == 0

    bad = tmp_path / "bad.csv"
    bad.write_text("id,clip,true_story\nx,missing.mp4,hi\n")
    assert cli.main(["liar", "run", "--cases", str(bad), "--clips", str(clips), "--out", str(out)]) == 1
    with pytest.raises(SystemExit, match="cases.example.csv"):
        cli.main(["liar", "check", "--cases", str(tmp_path / "nope.csv")])


# --- .env ----------------------------------------------------------------------

def test_env_example_loads(tmp_path, monkeypatch):
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")
    s = Settings.from_env()
    assert s.nebius_api_key == "" and s.offline is False and s.policy == "quality"
    assert s.nebius_base_url == "https://api.tokenfactory.nebius.com/v1"
    assert str(s.prices_file) == "pricing.json"


def test_env_values_may_be_quoted(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('NEBIUS_API_KEY="v1.abc"\nTAVILY_API_KEY=\'tvly-x\'\nPW_POLICY=budget\n')
    s = Settings.from_env()
    assert (s.nebius_api_key, s.tavily_api_key, s.policy) == ("v1.abc", "tvly-x", "budget")
