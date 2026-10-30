"""Where claims live.

FilesBackend (default): one folder per claim with meta.json and report.json,
next to the video and frames. Fine on one machine.

SupabaseBackend (PW_STORE=supabase): claim metadata and reports in Postgres,
one row per verdict in a `verdicts` table for SQL queries, and videos and
frames in a private Storage bucket, served through short-lived signed URLs.
The local claim folder is then only a working copy: any server instance can
list, show and delete any claim, and a fresh container loses nothing.

Both talk plain HTTP (PostgREST + Storage API), so no extra dependency.
Set up the tables and bucket once with deploy/supabase/schema.sql.
"""

from __future__ import annotations

import json
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

SIGNED_URL_TTL_S = 3600


class StoreError(RuntimeError):
    pass


class FilesBackend:
    kind = "files"

    def __init__(self, root: Path):
        self.root = Path(root)

    def _dir(self, run_id: str) -> Path:
        return self.root / run_id

    def exists(self, run_id: str) -> bool:
        return (self._dir(run_id) / "meta.json").is_file()

    def read_meta(self, run_id: str) -> dict[str, Any] | None:
        p = self._dir(run_id) / "meta.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def write_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        path = self._dir(run_id) / "meta.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def list_metas(self) -> list[dict[str, Any]]:
        out = []
        for d in self.root.iterdir() if self.root.is_dir() else []:
            p = d / "meta.json"
            if p.is_file():
                try:
                    out.append(json.loads(p.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
        return out

    def read_report(self, run_id: str) -> dict[str, Any] | None:
        p = self._dir(run_id) / "report.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def write_report(self, run_id: str, report: dict[str, Any]) -> None:
        p = self._dir(run_id) / "report.json"
        p.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    def clear_report(self, run_id: str) -> None:
        (self._dir(run_id) / "report.json").unlink(missing_ok=True)

    def publish_files(self, run_id: str, paths: list[tuple[str, Path]]) -> list[str]:
        return []          # the files already live in the claim folder

    def file_url(self, run_id: str, rel: str) -> str | None:
        return None

    def delete(self, run_id: str, meta: dict[str, Any] | None) -> None:
        shutil.rmtree(self._dir(run_id), ignore_errors=True)


class SupabaseBackend:
    kind = "supabase"

    def __init__(self, url: str, key: str, bucket: str = "claims", timeout: float = 60.0,
                 transport: httpx.BaseTransport | None = None):
        if not url or not key:
            raise StoreError("PW_STORE=supabase needs SUPABASE_URL and SUPABASE_SERVICE_KEY")
        self.url = url.rstrip("/")
        self.bucket = bucket
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout, transport=transport)

    # --- helpers --------------------------------------------------------------------

    def _check(self, resp: httpx.Response, what: str) -> httpx.Response:
        if resp.is_error:
            hint = ""
            if resp.status_code in (401, 403):
                hint = " (check SUPABASE_SERVICE_KEY: it must be the service_role key, not the anon key)"
            elif resp.status_code == 404 and "/rest/v1/" in str(resp.request.url):
                hint = " (have you run deploy/supabase/schema.sql?)"
            raise StoreError(f"Supabase {what} failed: HTTP {resp.status_code}{hint}: {resp.text[:300]}")
        return resp

    def _object(self, path: str) -> str:
        return quote(f"{self.bucket}/{path}", safe="/")

    # --- claims table ------------------------------------------------------------------

    def exists(self, run_id: str) -> bool:
        return self.read_meta(run_id) is not None

    def read_meta(self, run_id: str) -> dict[str, Any] | None:
        r = self._check(self._http.get("/rest/v1/claims", params={"select": "meta", "id": f"eq.{run_id}"}), "read claim")
        rows = r.json()
        return rows[0]["meta"] if rows else None

    def write_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        row = {"id": run_id, "meta": meta, "status": meta.get("status"), "title": meta.get("title"),
               "created_at": _iso(meta.get("created")), "updated_at": _iso(time.time())}
        self._check(self._http.post("/rest/v1/claims", json=row,
                                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"}), "save claim")

    def list_metas(self) -> list[dict[str, Any]]:
        r = self._check(self._http.get("/rest/v1/claims", params={"select": "meta", "order": "created_at.desc"}), "list claims")
        return [row["meta"] for row in r.json()]

    def read_report(self, run_id: str) -> dict[str, Any] | None:
        r = self._check(self._http.get("/rest/v1/claims", params={"select": "report", "id": f"eq.{run_id}"}), "read report")
        rows = r.json()
        return rows[0]["report"] if rows else None

    def write_report(self, run_id: str, report: dict[str, Any]) -> None:
        self._check(self._http.patch("/rest/v1/claims", params={"id": f"eq.{run_id}"}, json={"report": report},
                                     headers={"Prefer": "return=minimal"}), "save report")
        # One row per verdict, so outcomes can be queried with plain SQL.
        self._check(self._http.delete("/rest/v1/verdicts", params={"claim_id": f"eq.{run_id}"}), "clear verdicts")
        rows = [{"claim_id": run_id, "claim_key": v.get("claim_id"), "statement": v.get("claim"),
                 "verdict": v.get("verdict"), "round": v.get("round", 0), "reasoning": v.get("reasoning"),
                 "evidence": v.get("evidence") or []} for v in report.get("verdicts") or []]
        if rows:
            self._check(self._http.post("/rest/v1/verdicts", json=rows, headers={"Prefer": "return=minimal"}), "save verdicts")

    def clear_report(self, run_id: str) -> None:
        self._check(self._http.patch("/rest/v1/claims", params={"id": f"eq.{run_id}"}, json={"report": None},
                                     headers={"Prefer": "return=minimal"}), "clear report")

    # --- storage ----------------------------------------------------------------------

    def publish_files(self, run_id: str, paths: list[tuple[str, Path]]) -> list[str]:
        """Upload (object name relative to the claim, local file) pairs. Returns the object paths."""
        done = []
        for rel, local in paths:
            obj = f"{run_id}/{rel}"
            ctype = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
            with open(local, "rb") as fh:
                self._check(self._http.post(f"/storage/v1/object/{self._object(obj)}", content=fh.read(),
                                            headers={"Content-Type": ctype, "x-upsert": "true"}), f"upload {rel}")
            done.append(obj)
        return done

    def put_file(self, name: str, local: Path) -> None:
        """Upload one file to the bucket under `name` (e.g. samples/dallas.mp4)."""
        ctype = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
        with open(local, "rb") as fh:
            self._check(self._http.post(f"/storage/v1/object/{self._object(name)}", content=fh.read(),
                                        headers={"Content-Type": ctype, "x-upsert": "true"}), f"upload {name}")

    def get_file(self, name: str, dest: Path) -> bool:
        """Download `name` from the bucket to `dest`. False if there is no such object."""
        with self._http.stream("GET", f"/storage/v1/object/{self._object(name)}") as r:
            if r.status_code in (400, 404):
                return False
            if r.is_error:
                r.read()
                self._check(r, f"download {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes():
                    fh.write(chunk)
        tmp.replace(dest)
        return True

    def file_url(self, run_id: str, rel: str) -> str | None:
        r = self._http.post(f"/storage/v1/object/sign/{self._object(f'{run_id}/{rel}')}",
                            json={"expiresIn": SIGNED_URL_TTL_S})
        if r.status_code in (400, 404):
            return None
        self._check(r, f"sign {rel}")
        signed = r.json().get("signedURL") or r.json().get("signedUrl")
        return f"{self.url}/storage/v1{signed}" if signed and signed.startswith("/") else signed

    def delete(self, run_id: str, meta: dict[str, Any] | None) -> None:
        objects = list((meta or {}).get("objects") or [])
        if objects:
            self._check(self._http.request("DELETE", f"/storage/v1/object/{quote(self.bucket)}", json={"prefixes": objects}),
                        "delete files")
        self._check(self._http.delete("/rest/v1/claims", params={"id": f"eq.{run_id}"}), "delete claim")

    # --- model cache (used by cache.ResponseCache) ------------------------------------------

    def cache_get(self, namespace: str, key: str) -> Any | None:
        r = self._check(self._http.get("/rest/v1/model_cache", params={"select": "value", "namespace": f"eq.{namespace}",
                                                                       "key": f"eq.{key}"}), "read cache")
        rows = r.json()
        return rows[0]["value"] if rows else None

    def cache_put(self, namespace: str, key: str, value: Any) -> None:
        self._check(self._http.post("/rest/v1/model_cache", json={"namespace": namespace, "key": key, "value": value},
                                    headers={"Prefer": "resolution=merge-duplicates,return=minimal"}), "save cache")

    # --- setup check ------------------------------------------------------------------------

    def check(self) -> list[tuple[str, bool, str]]:
        """(step, ok, detail) for `physics-witness supabase check`."""
        steps = []

        def step(name, fn):
            try:
                steps.append((name, True, fn() or ""))
            except Exception as exc:   # report every step, keep going
                steps.append((name, False, str(exc)[:300]))
        for table in ("claims", "verdicts", "model_cache"):
            step(f"table {table}", lambda t=table: self._check(
                self._http.get(f"/rest/v1/{t}", params={"select": "*", "limit": "1"}), f"read {t}") and "reachable")
        step(f"bucket {self.bucket}", lambda: self._check(
            self._http.get(f"/storage/v1/bucket/{quote(self.bucket)}"), "read bucket").json().get("public") is False
            and "private" or "exists, but it is PUBLIC: make it private")
        probe = f"_check_{int(time.time())}"

        def roundtrip():
            self.write_meta(probe, {"id": probe, "status": "done", "title": "setup check", "created": time.time()})
            assert self.read_meta(probe)["title"] == "setup check"
            self.delete(probe, None)
            assert self.read_meta(probe) is None
            return "write, read and delete work"
        step("claims round trip", roundtrip)
        return steps


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def make_backend(settings, root: Path):
    if (settings.store or "files") == "supabase":
        return SupabaseBackend(settings.supabase_url, settings.supabase_key, settings.supabase_bucket)
    return FilesBackend(root)
