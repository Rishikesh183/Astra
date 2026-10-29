"""The Supabase backend (Postgres + Storage), run against an in-memory fake of
the Supabase HTTP APIs (tests/fake_supabase.py)."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fake_supabase import FakeSupabase
from physics_witness import cli
from physics_witness.backends import StoreError, SupabaseBackend
from physics_witness.cache import ResponseCache, make_cache
from physics_witness.config import Settings
from physics_witness.frames import Cancelled
from physics_witness.server import create_app
from physics_witness.store import STALE_S, RunStore

URL = "https://demo-project.supabase.co"
STORY = "The car was on the crossing. A train hit the car."


def backend(fake, key="service-key"):
    return SupabaseBackend(URL, key, "claims", transport=fake.transport)


def store(fake, root, **kw):
    return RunStore(root, Settings(offline=True, cache_dir=root.parent / "cache"), backend=backend(fake), **kw)


def wait_for(fn, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


# --- setup check ------------------------------------------------------------------------------

def test_check_passes_on_a_set_up_project():
    results = backend(FakeSupabase()).check()
    assert all(ok for _, ok, _ in results), results
    assert [name for name, _, _ in results] == ["table claims", "table verdicts", "table model_cache",
                                                 "bucket claims", "claims round trip"]


def test_check_explains_missing_schema_and_wrong_key():
    missing = dict((n, d) for n, ok, d in backend(FakeSupabase(schema=False)).check() if not ok)
    assert "schema.sql" in missing["table claims"] and "Bucket not found" in missing["bucket claims"]
    wrong = dict((n, d) for n, ok, d in backend(FakeSupabase(), key="anon-key").check() if not ok)
    assert "service_role" in wrong["table claims"]
    with pytest.raises(StoreError, match="SUPABASE_URL"):
        SupabaseBackend("", "")


# --- a claim's whole life in Supabase ---------------------------------------------------------------

def test_claim_lives_in_postgres_and_storage(clip, tmp_path):
    fake = FakeSupabase()
    a = store(fake, tmp_path / "server-a")
    rid = a.create(clip, STORY, title="Crossing", wait=True)

    row = fake.claim(rid)
    assert row["status"] == "done" and row["title"] == "Crossing" and row["meta"]["thumb"]
    assert row["report"]["verdicts"] and row["created_at"].endswith("Z")
    assert len(fake.tables["verdicts"]) == len(row["report"]["verdicts"]) == 2
    assert {v["verdict"] for v in fake.tables["verdicts"]} == {"cant_tell"}          # offline run
    names = fake.object_names(rid)
    assert "video.mp4" in names and any(n.startswith("frames/frame_") for n in names)
    assert sorted(row["meta"]["objects"]) == sorted(f"{rid}/{n}" for n in names)

    # A fresh container (empty disk, same database) sees everything.
    b = store(fake, tmp_path / "server-b")
    assert [m["id"] for m in b.list()] == [rid]
    assert b.report(rid)["claims"] == row["report"]["claims"]
    with pytest.raises(KeyError):
        b.video_path(rid)                                          # not on this disk...
    assert b.video_url(rid).startswith(f"{URL}/storage/v1/object/sign/claims/{rid}/video.mp4?token=")
    with TestClient(create_app(store=b)) as http:
        assert http.get("/api/health").json()["store"] == "supabase"
        video = http.get(f"/api/claims/{rid}/video", follow_redirects=False)
        assert video.status_code == 307 and "/object/sign/claims/" in video.headers["location"]
        frame = row["meta"]["thumb"]
        img = http.get(f"/api/claims/{rid}/frames/{frame}", follow_redirects=False)
        assert img.status_code == 307 and img.headers["location"].endswith("?token=signed-token")
        assert http.get(f"/api/claims/{rid}/frames/frame_0099.00s.jpg", follow_redirects=False).status_code == 404
        assert http.get(f"/claims/{rid}").status_code == 200       # the claim page works on any instance
        assert http.delete(f"/api/claims/{rid}").status_code == 204
    assert fake.claim(rid) is None and fake.tables["verdicts"] == [] and fake.object_names(rid) == set()
    a.shutdown(), b.shutdown()


def test_extra_footage_is_uploaded_and_rejudged(clip, tmp_path):
    fake = FakeSupabase()
    a = store(fake, tmp_path / "a")
    rid = a.create(clip, STORY, wait=True)
    assert a.add_evidence(rid, clip, "rear camera", wait=True) == "e1"
    row = fake.claim(rid)
    assert row["meta"]["status"] == "done" and row["meta"]["videos"]["e1"] == "e1.mp4"
    assert "e1.mp4" in fake.object_names(rid) and any(n.startswith("frames/e1_") for n in fake.object_names(rid))
    assert [s["id"] for s in row["report"]["sources"]] == ["main", "e1"]
    b = store(fake, tmp_path / "b")
    assert b.video_url(rid, "e1").endswith("e1.mp4?token=signed-token")
    a.shutdown(), b.shutdown()


# --- several server instances ------------------------------------------------------------------------

def stepping_runner(started):
    def runner(video, story, *, cancel, on_progress, **kw):
        started.set()
        for _ in range(400):                # reports progress like a real run, so it can see remote cancels
            on_progress({"stage": "seeing", "detail": "working"})
            if cancel.is_set():
                raise Cancelled()
            time.sleep(0.02)
        raise AssertionError("never cancelled")
    return runner


def test_cancel_from_another_instance(clip, tmp_path):
    fake = FakeSupabase()
    started = threading.Event()
    a = store(fake, tmp_path / "a", runner=stepping_runner(started))
    rid = a.create(clip, STORY)
    assert started.wait(10)
    b = store(fake, tmp_path / "b")                                 # does not touch a live claim at start-up
    assert b.meta(rid)["status"] == "running"
    assert b.cancel(rid) is True and b.meta(rid)["cancel_requested"] is True
    assert wait_for(lambda: fake.claim(rid)["meta"]["status"] == "cancelled")
    assert fake.claim(rid)["meta"]["cancel_requested"] is False and fake.claim(rid)["report"] is None
    a.shutdown(), b.shutdown()


def test_quiet_claims_are_marked_interrupted(clip, tmp_path):
    fake = FakeSupabase()
    a = store(fake, tmp_path / "a")
    rid = a.create(clip, STORY, wait=True)
    a.shutdown()
    meta = fake.claim(rid)["meta"]
    fake.claim(rid)["report"] = None
    fake.claim(rid)["meta"] = {**meta, "status": "running", "updated": time.time() - STALE_S - 5}
    b = store(fake, tmp_path / "b")
    assert b.meta(rid)["status"] == "error" and "Interrupted" in b.meta(rid)["error"]
    # A claim that reported progress recently is left alone (another instance is running it).
    fake.claim(rid)["meta"] = {**meta, "status": "running", "updated": time.time()}
    c = store(fake, tmp_path / "c")
    assert c.meta(rid)["status"] == "running"
    b.shutdown(), c.shutdown()


# --- shared model cache -------------------------------------------------------------------------------

def test_model_replies_are_shared_between_containers(tmp_path):
    fake = FakeSupabase()
    calls = []

    def ask(cache_dir, answer="an answer"):
        calls.append(1)
        return {"choices": [{"message": {"content": answer}}]}
    first = ResponseCache(tmp_path / "c1", remote=backend(fake))
    value, hit = first.get_or_call("chat", {"q": 1}, lambda: ask("c1"))
    assert not hit and len(fake.tables["model_cache"]) == 1
    fresh = ResponseCache(tmp_path / "c2", remote=backend(fake))          # new container, empty disk
    value2, hit2 = fresh.get_or_call("chat", {"q": 1}, lambda: ask("c2"))
    assert hit2 and value2 == value and len(calls) == 1
    assert list((tmp_path / "c2").rglob("*.json"))                      # now also on local disk

    keep = lambda v: bool(v["choices"][0]["message"]["content"])
    fresh.get_or_call("chat", {"q": 2}, lambda: ask("c2", answer=""), keep=keep)
    assert len(fake.tables["model_cache"]) == 1                          # empty replies are not shared either

    broken = ResponseCache(tmp_path / "c3", remote=backend(FakeSupabase(schema=False)))
    assert broken.get_or_call("chat", {"q": 3}, lambda: ask("c3"))[0]    # a broken shared cache never blocks a run


def test_make_cache_uses_supabase_only_when_configured(tmp_path):
    assert make_cache(Settings(cache_dir=tmp_path)).remote is None
    supa = make_cache(Settings(cache_dir=tmp_path, store="supabase", supabase_url=URL, supabase_key="k"))
    assert isinstance(supa.remote, SupabaseBackend)
    assert make_cache(Settings(cache_dir=tmp_path, store="supabase")).remote is None    # no credentials


def test_settings_read_supabase_env(tmp_path, monkeypatch):
    for k, v in {"PW_STORE": "supabase", "SUPABASE_URL": URL, "SUPABASE_SERVICE_KEY": "k"}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert (s.store, s.supabase_url, s.supabase_key, s.supabase_bucket) == ("supabase", URL, "k", "claims")


# --- moving local claims into Supabase -----------------------------------------------------------------

def test_migrate_local_claims(clip, tmp_path, monkeypatch, capsys):
    local = RunStore(tmp_path / "claims", Settings(offline=True, cache_dir=tmp_path / "cache"))
    rid = local.create(clip, STORY, title="Old local claim", wait=True)
    local.shutdown()
    (tmp_path / "cache" / "chat").mkdir(parents=True)
    (tmp_path / "cache" / "chat" / "abc.json").write_text(json.dumps({"choices": []}), encoding="utf-8")
    fake = FakeSupabase()
    monkeypatch.setattr(cli, "_supabase_backend", lambda: backend(fake))
    monkeypatch.setenv("PW_CACHE_DIR", str(tmp_path / "cache"))
    assert cli.main(["supabase", "migrate", "--data", str(tmp_path / "claims"), "--cache"]) == 0
    out = capsys.readouterr().out
    assert "moved " + rid in out and "1 claims moved, 1 cached model replies copied" in out
    row = fake.claim(rid)
    assert row["title"] == "Old local claim" and row["report"] and row["meta"]["videos"] == {"main": "video.mp4"}
    assert "video.mp4" in fake.object_names(rid) and fake.tables["model_cache"][0]["key"] == "abc"
    assert cli.main(["supabase", "migrate", "--data", str(tmp_path / "claims")]) == 0
    assert "skip " + rid in capsys.readouterr().out                      # second run does not overwrite


def test_check_cli(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_supabase_backend", lambda: backend(FakeSupabase()))
    assert cli.main(["supabase", "check"]) == 0
    assert "Supabase is ready" in capsys.readouterr().out
    monkeypatch.setattr(cli, "_supabase_backend", lambda: backend(FakeSupabase(schema=False)))
    assert cli.main(["supabase", "check"]) == 1
    assert "schema.sql" in capsys.readouterr().out
