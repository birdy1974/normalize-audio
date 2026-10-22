"""Application factory: wires the database, the engine and the web UI together."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, api
from . import ffmpeg as ff
from .config import Config, load_config
from .db import Database
from .engine import Engine, FileBusy


def create_app(cfg: Config | None = None, start_threads: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    db = Database(cfg.db_path)
    engine = Engine(cfg, db)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_threads:
            engine.start_threads()
        yield
        engine.shutdown()

    app = FastAPI(title="Audio Normalizer", version=__version__, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.engine = engine
    app.include_router(api.router, prefix="/api")

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ff.ToolError)
    async def _tool_error(_req: Request, exc: ff.ToolError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(FileBusy)
    async def _file_busy(_req: Request, exc: FileBusy):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    app.mount("/static", StaticFiles(directory=cfg.static_dir), name="static")

    index = os.path.join(cfg.static_dir, "index.html")

    @app.get("/", include_in_schema=False)
    async def index_page():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app
