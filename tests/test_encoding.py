"""Windows regression: files and console output must not depend on the system
code page (cp1252 on Windows), since model replies can contain any script."""

import json
import os
import subprocess
import sys

STORY = "Машина стояла на переезде. Поезд ударил машину → слева.\n"


def test_cli_run_works_under_a_legacy_locale(clip, tmp_path):
    story = tmp_path / "story.txt"
    story.write_text(STORY, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("LANG", "LC_ALL", "PYTHONUTF8", "PYTHONIOENCODING")}
    env.update(LC_ALL="C", PYTHONUTF8="0", PYTHONIOENCODING="ascii", PW_OFFLINE="1",
               PW_CACHE_DIR=str(tmp_path / "cache"))
    proc = subprocess.run([sys.executable, "-m", "physics_witness", "run", str(clip), str(story), "--out", str(tmp_path / "out")],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))
    assert report["claims"][1]["text"] == "Поезд ударил машину → слева."
    assert (tmp_path / "out" / "report.json").read_bytes().decode("utf-8")      # valid UTF-8 on disk
