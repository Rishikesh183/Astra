"""Web app: one screen with the video and timeline on the left and the story's
claims with verdicts on the right. `physics-witness serve` starts it."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .config import Settings
from .store import VIDEO_EXTS, RunStore

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024


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

    @app.get("/api/health")
    def health():
        s = store.settings
        return {"ok": True, "nebius": bool(s.nebius_api_key) and not s.offline,
                "tavily": bool(s.tavily_api_key) and not s.offline}

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
                    raise HTTPException(413, "video larger than 200 MB")
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

    @app.get("/api/claims/{run_id}/video")
    def get_video(run_id: str, source: str = Query("main")):
        return FileResponse(_get(store.video_path, run_id, source))

    @app.get("/api/claims/{run_id}/frames/{name}")
    def get_frame(run_id: str, name: str):
        return FileResponse(_get(store.frame_path, run_id, name), media_type="image/jpeg")

    return app
