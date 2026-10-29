"""Web app: one screen with the video and timeline on the left and the story's
claims with verdicts on the right. `physics-witness serve` starts it."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .config import Settings
from .samples import load_samples
from .store import VIDEO_EXTS, RunStore

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_MB = 40              # under Supabase's 50 MB free-plan file limit
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def create_app(data_dir: Path = Path("data/claims"), settings: Settings | None = None,
               store: RunStore | None = None) -> FastAPI:
    store = store or RunStore(data_dir, settings)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        store.shutdown()

    app = FastAPI(title="Physics Witness", lifespan=lifespan)
    app.state.store = store

    def _get(fn, *args):
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(404, "not found") from None

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/claims/{run_id}", include_in_schema=False)
    def claim_page(run_id: str):
        # The page itself routes on the URL, so a claim link can be bookmarked or refreshed.
        # Unknown ids still get the app (it shows "Claim not found"), with a 404 status.
        try:
            store.dir(run_id)
        except KeyError:
            return FileResponse(STATIC / "index.html", status_code=404)
        return FileResponse(STATIC / "index.html")

    def _samples():
        return {s.id: s for s in load_samples(store.settings.samples_file)}

    @app.get("/api/samples")
    def list_samples():
        return [s.public() for s in _samples().values()]

    @app.get("/api/samples/{sample_id}/video")
    def sample_video(sample_id: str):
        s = _samples().get(sample_id)
        if s is None or not s.available:
            raise HTTPException(404, "not found")
        return FileResponse(s.video)

    @app.post("/api/samples/{sample_id}/try", status_code=202)
    def try_sample(sample_id: str):
        s = _samples().get(sample_id)
        if s is None or not s.available:
            raise HTTPException(404, "sample not available")
        run_id = store.create(s.video, s.story, title=s.title, place=s.place, date=s.date,
                              sample={"id": s.id, "label": s.label, "lie_sentence": s.lie_sentence})
        return JSONResponse({"id": run_id}, status_code=202)

    @app.get("/api/health")
    def health():
        s = store.settings
        return {"ok": True, "nebius": bool(s.nebius_api_key) and not s.offline,
                "tavily": bool(s.tavily_api_key) and not s.offline, "store": store.backend.kind}

    @app.get("/api/claims")
    def list_claims():
        return store.list()

    async def _save_upload(video: UploadFile) -> Path:
        suffix = Path(video.filename or "").suffix.lower()
        if suffix not in VIDEO_EXTS:
            raise HTTPException(400, f"unsupported video type {suffix!r}")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            size = 0
            while chunk := await video.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    Path(tmp.name).unlink(missing_ok=True)
                    raise HTTPException(413, f"video larger than {MAX_UPLOAD_MB} MB: trim or compress it")
                tmp.write(chunk)
        return Path(tmp.name)

    @app.post("/api/claims", status_code=202)
    async def create_claim(video: UploadFile = File(...), story: str = Form(...), title: str = Form(""),
                           place: str = Form(""), date: str = Form("")):
        tmp = await _save_upload(video)
        try:
            run_id = store.create(tmp, story, title=title, place=place, date=date, move=True)
        except ValueError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, str(exc)) from None
        return JSONResponse({"id": run_id}, status_code=202)

    @app.post("/api/claims/{run_id}/evidence", status_code=202)
    async def add_evidence(run_id: str, video: UploadFile = File(...), note: str = Form("")):
        _get(store.meta, run_id)
        tmp = await _save_upload(video)
        try:
            source = store.add_evidence(run_id, tmp, note, move=True)
        except ValueError as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(409, str(exc)) from None
        return JSONResponse({"id": run_id, "source": source}, status_code=202)

    @app.get("/api/claims/{run_id}")
    def get_claim(run_id: str):
        meta = _get(store.meta, run_id)
        return {"meta": meta, "report": store.report(run_id)}

    @app.post("/api/claims/{run_id}/cancel", status_code=202)
    def cancel_claim(run_id: str):
        if not _get(store.cancel, run_id):
            raise HTTPException(409, "this claim is not running")
        return {"id": run_id, "cancelling": True}

    @app.delete("/api/claims/{run_id}", status_code=204)
    def delete_claim(run_id: str):
        try:
            _get(store.delete, run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return Response(status_code=204)

    @app.get("/api/claims/{run_id}/video")
    def get_video(run_id: str, source: str = Query("main")):
        try:
            return FileResponse(store.video_path(run_id, source))
        except KeyError:
            url = _get(store.video_url, run_id, source)      # kept in the shared store
            if not url:
                raise HTTPException(404, "not found") from None
            return RedirectResponse(url, status_code=307)

    @app.get("/api/claims/{run_id}/frames/{name}")
    def get_frame(run_id: str, name: str):
        try:
            return FileResponse(store.frame_path(run_id, name), media_type="image/jpeg")
        except KeyError:
            url = _get(store.frame_url, run_id, name)
            if not url:
                raise HTTPException(404, "not found") from None
            return RedirectResponse(url, status_code=307)

    return app
