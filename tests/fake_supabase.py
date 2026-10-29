"""In-memory stand-in for the slice of Supabase that backends.SupabaseBackend
uses: PostgREST (GET/POST upsert/PATCH/DELETE with eq. filters, select, order,
limit) and the Storage API (upload, signed URLs, delete, bucket info). Plugged
in through httpx.MockTransport, so tests never touch the network."""

from __future__ import annotations

import json
from urllib.parse import unquote

import httpx

KEYS = {"claims": ("id",), "model_cache": ("namespace", "key")}


class FakeSupabase:
    def __init__(self, key: str = "service-key", bucket: str = "claims", schema: bool = True):
        self.key = key
        self.tables = {"claims": [], "verdicts": [], "model_cache": []} if schema else {}
        self.buckets = {bucket: {"id": bucket, "name": bucket, "public": False}} if schema else {}
        self.objects: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []
        self._next_id = 1

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    # ------------------------------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.headers.get("apikey") != self.key or request.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"message": "Invalid API key"})
        path = request.url.path
        if path.startswith("/rest/v1/"):
            return self._rest(request, path.removeprefix("/rest/v1/"))
        if path.startswith("/storage/v1/"):
            return self._storage(request, unquote(path.removeprefix("/storage/v1/")))
        return httpx.Response(404)

    # --- PostgREST ------------------------------------------------------------------
    def _rest(self, request: httpx.Request, table: str) -> httpx.Response:
        if table not in self.tables:
            return httpx.Response(404, json={"message": f'relation "public.{table}" does not exist'})
        rows = self.tables[table]
        params = dict(request.url.params)
        filters = {k: v.removeprefix("eq.") for k, v in params.items() if k not in ("select", "order", "limit", "on_conflict")}
        match = lambda r: all(str(r.get(k)) == v for k, v in filters.items())
        if request.method == "GET":
            out = [r for r in rows if match(r)]
            if params.get("order", "").startswith("created_at"):
                out.sort(key=lambda r: r.get("created_at") or "", reverse=params["order"].endswith(".desc"))
            if "limit" in params:
                out = out[: int(params["limit"])]
            cols = params.get("select", "*")
            if cols != "*":
                out = [{c: r.get(c) for c in cols.split(",")} for r in out]
            return httpx.Response(200, json=json.loads(json.dumps(out)))
        if request.method == "POST":
            body = json.loads(request.content)
            items = body if isinstance(body, list) else [body]
            upsert = "merge-duplicates" in request.headers.get("prefer", "")
            for item in items:
                key = KEYS.get(table)
                existing = next((r for r in rows if key and all(r.get(k) == item.get(k) for k in key)), None)
                if existing is not None:
                    if not upsert:
                        return httpx.Response(409, json={"message": "duplicate key"})
                    existing.update(item)
                else:
                    if table == "verdicts":
                        item = {"id": self._next_id, **item}
                        self._next_id += 1
                        if not any(c["id"] == item["claim_id"] for c in self.tables["claims"]):
                            return httpx.Response(409, json={"message": "foreign key violation"})
                    rows.append(dict(item))
            return httpx.Response(201)
        if request.method == "PATCH":
            body = json.loads(request.content)
            for r in rows:
                if match(r):
                    r.update(body)
            return httpx.Response(204)
        if request.method == "DELETE":
            gone = [r for r in rows if match(r)]
            self.tables[table] = [r for r in rows if not match(r)]
            if table == "claims":         # on delete cascade
                ids = {r["id"] for r in gone}
                self.tables["verdicts"] = [v for v in self.tables["verdicts"] if v["claim_id"] not in ids]
            return httpx.Response(204)
        return httpx.Response(405)

    # --- Storage --------------------------------------------------------------------
    def _storage(self, request: httpx.Request, path: str) -> httpx.Response:
        if path.startswith("bucket/"):
            b = self.buckets.get(path.removeprefix("bucket/"))
            return httpx.Response(200, json=b) if b else httpx.Response(404, json={"error": "Bucket not found"})
        if path.startswith("object/sign/"):
            obj = path.removeprefix("object/sign/")
            if request.method == "POST":
                if obj not in self.objects:
                    return httpx.Response(400, json={"error": "Object not found"})
                return httpx.Response(200, json={"signedURL": f"/object/sign/{obj}?token=signed-token"})
            return httpx.Response(200, content=self.objects.get(obj, b""))
        if path.startswith("object/"):
            obj = path.removeprefix("object/")
            bucket = obj.split("/", 1)[0]
            if bucket not in self.buckets:
                return httpx.Response(404, json={"error": "Bucket not found"})
            if request.method == "POST":
                if obj in self.objects and request.headers.get("x-upsert") != "true":
                    return httpx.Response(409, json={"error": "Duplicate"})
                self.objects[obj] = request.content
                return httpx.Response(200, json={"Key": obj})
            if request.method == "DELETE":
                for prefix in json.loads(request.content).get("prefixes", []):
                    self.objects.pop(f"{bucket}/{prefix}", None)
                return httpx.Response(200, json=[])
        return httpx.Response(404)

    # --- helpers for assertions -----------------------------------------------------
    def claim(self, run_id: str) -> dict | None:
        return next((r for r in self.tables["claims"] if r["id"] == run_id), None)

    def object_names(self, run_id: str) -> set[str]:
        return {k.split("/", 2)[2] for k in self.objects if k.split("/")[1] == run_id}
