"""REST API for the web UI (/api) and the Home Assistant status endpoint (/api/v1/ha/status)."""
from __future__ import annotations

import hmac
import os
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, folders, ha, state, stats, waveform
from .config import tool_path
from .media import EXTENSIONS, MEDIA
from .schedule import in_window, next_change

router = APIRouter()


def get_engine(request: Request):
    return request.app.state.engine


Engine = Depends(get_engine)


# ------------------------------------------------------------ models

class TypesBody(BaseModel):
    audio: bool | None = None
    video: bool | None = None


class FolderToggle(BaseModel):
    media: Literal["audio", "video"]
    relpath: str
    enabled: bool


class FolderAll(BaseModel):
    media: Literal["audio", "video"]
    enabled: bool


class ScanBody(BaseModel):
    media: Literal["audio", "video"] | None = None


class MeasureBody(BaseModel):
    media: Literal["audio", "video"]
    relpath: str


class FilesBatchBody(BaseModel):
    media: Literal["audio", "video"]
    ids: list[int] = Field(default_factory=list)
    dir: str | None = None


class EditBody(BaseModel):
    media: Literal["audio"]
    relpath: str
    fade_in: float = 0.0            # seconds
    fade_out: float = 0.0           # seconds
    cut_start: float | None = None  # seconds; both or neither
    cut_end: float | None = None


class AdjustBody(BaseModel):
    value: float | None = None      # new change needed, dB
    delta: float | None = None      # change of the current value, dB


class BulkBody(BaseModel):
    ids: list[int] = Field(default_factory=list)
    action: Literal["approve", "skip", "adjust"]
    delta: float | None = None


class ApproveAllBody(BaseModel):
    media: Literal["audio", "video"]
    reason: Literal["large_change", "lossy_no_gain_tool"]


class HaBody(BaseModel):
    url: str | None = None
    token: str | None = None
    sensors: list[dict] | None = None


SORTS = {
    "order": "id ASC",
    "change_asc": "(gain_db + adjust_db) ASC",
    "change_desc": "(gain_db + adjust_db) DESC",
    "size_desc": "ABS(gain_db + adjust_db) DESC",
}


def _media_check(media: str) -> str:
    if media not in MEDIA:
        raise HTTPException(400, "media must be audio or video")
    return media


# ------------------------------------------------------------ general

@router.get("/health")
def health():
    return {"ok": True, "version": __version__}


@router.get("/config")
def config(engine=Engine):
    cfg = engine.cfg
    return {
        "version": __version__,
        "roots": {"audio": cfg.audio_root, "video": cfg.video_root},
        "tools": {
            "ffmpeg": bool(tool_path(cfg.ffmpeg)),
            "mp3gain": "mp3gain" in engine.tools,
            "aacgain": "aacgain" in engine.tools,
        },
        "timezone": cfg.tz,
        "concurrency": cfg.concurrency,
        "port": cfg.port,
    }


@router.get("/dashboard")
def dashboard(engine=Engine):
    db = engine.db
    now = engine.now()
    sched = state.get_schedule(db)
    run = state.get_run(db)
    settings = state.get_settings(db)
    media = {}
    total = checked = 0
    for m in MEDIA:
        s = stats.media_stats(db, m)
        nxt = next_change(sched[m], now)
        media[m] = {
            **s,
            "in_window": in_window(sched[m], now),
            "next_change": nxt.isoformat(timespec="minutes") if nxt else None,
            "target": settings[m]["target"],
            "enabled": bool(run["types"].get(m)),
        }
        if run["types"].get(m):
            total += s["files_total"]
            checked += s["files_checked"]
    approved_pending, approved_file = engine.approved_status()
    st_now = engine.status(media)
    current = engine.current_file() or approved_file
    if not current and st_now == "running":
        current = engine.next_pending_file(run["types"])
    return {
        "status": st_now,
        "mode": settings["general"]["mode"],      # off / bulk / force / auto: what the app does on its own
        "run_mode": run["mode"],                  # the run engine: active / stopped
        "paused": run["paused"],
        "types": run["types"],
        "last_finished": run["lastFinished"],
        "scan_every": sched["scanEvery"],         # minutes between scans in Automatic mode
        "last_scan": run.get("lastScan"),
        "next_scan": engine.next_scan(),          # None unless Automatic mode is selected
        "progress_percent": int(round(100 * checked / total)) if total else 0,
        "current_file": current,
        "approved_pending": approved_pending,
        "scanning": engine.scanning(),
        "dry_run": settings["general"]["dryRun"],
        "media": media,
        "log": db.log_list(60),
    }


@router.post("/run/start")
def run_start(engine=Engine):
    engine.start()
    return {"status": engine.status()}


@router.post("/run/stop")
def run_stop(engine=Engine):
    engine.stop()
    return {"status": engine.status()}


@router.post("/run/pause")
def run_pause(engine=Engine):
    engine.toggle_pause()
    return {"status": engine.status()}


@router.post("/run/types")
def run_types(body: TypesBody, engine=Engine):
    engine.set_types(body.model_dump())
    return {"status": engine.status()}


@router.post("/log/clear")
def log_clear(engine=Engine):
    engine.db.clear_log()
    return {"ok": True}


@router.post("/scan")
def scan(body: ScanBody | None = None, engine=Engine):
    media = body.media if body else None
    return engine.scan_now(media)


# ------------------------------------------------------------ folders

@router.get("/folders/tree")
def folder_tree(media: str = "audio", engine=Engine):
    """Every folder of one media type in one call (flat list; the UI builds the tree)."""
    _media_check(media)
    db = engine.db
    dirs = db.query("SELECT relpath, parent, enabled FROM dirs WHERE media = ? AND relpath != '.' "
                    "ORDER BY relpath", (media,))
    direct = {r["dir"]: r["n"] for r in db.query(
        "SELECT dir, COUNT(*) AS n FROM files WHERE media = ? GROUP BY dir", (media,))}
    subtree: dict[str, int] = {}
    for rel, n in direct.items():
        if rel == "." or not n:
            continue
        here = rel
        while True:                       # the file counts every folder above it, too
            subtree[here] = subtree.get(here, 0) + n
            if "/" not in here:
                break
            here = here.rsplit("/", 1)[0]
    out = [{"relpath": d["relpath"], "name": d["relpath"].rsplit("/", 1)[-1], "parent": d["parent"],
            "enabled": bool(d["enabled"]), "direct_files": direct.get(d["relpath"], 0),
            "files": subtree.get(d["relpath"], 0)} for d in dirs]
    return {"media": media, "dirs": out, "root_files": direct.get(".", 0)}


@router.get("/folders")
def list_folders(media: str = "audio", parent: str = ".", engine=Engine):
    _media_check(media)
    return {"media": media, "parent": parent, "children": folders.children(engine.db, media, parent),
            "info": folders.info(engine.db, media, parent)}


@router.post("/folders/toggle")
def toggle_folder(body: FolderToggle, engine=Engine):
    folders.set_enabled(engine.db, _media_check(body.media), body.relpath, body.enabled)
    return {"ok": True}


@router.post("/folders/all")
def toggle_all(body: FolderAll, engine=Engine):
    folders.set_all(engine.db, _media_check(body.media), body.enabled)
    return {"ok": True}


# ------------------------------------------------------------ files

# Formats a browser can play as they are; a check in the Directories tab streams the file
# unchanged (no transcoding, so what you hear is exactly what will be normalized).
PLAYABLE = {
    "audio": ("mp3", "flac", "wav", "aif", "aiff", "m4a", "aac", "ogg", "oga", "opus"),
    "video": ("mp4", "m4v", "mov", "webm"),
}

STREAM_TYPES = {
    "mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav", "aif": "audio/aiff", "aiff": "audio/aiff",
    "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
}


def _stream_path(engine, media: str, rel: str) -> str:
    """The file for a preview, or an HTTP error. Nothing outside the base location is served."""
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        raise HTTPException(400, "invalid path")
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
    if ext not in PLAYABLE[media] or ext not in EXTENSIONS[media]:
        raise HTTPException(404, "this format cannot be played in a browser")
    root = os.path.realpath(engine.cfg.root(media))
    path = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        raise HTTPException(404, "file not found")
    return path


@router.get("/files")
def list_files(media: str = "audio", dir: str = ".", engine=Engine):
    _media_check(media)
    rows = engine.db.query(
        "SELECT id, name, relpath, ext, lufs, true_peak, status, reason, gain_db, adjust_db, applied_db, "
        "method, measured_at, changed_at, silent_sections, silent_duration, silence_trimmed "
        "FROM files WHERE media = ? AND dir = ? ORDER BY name LIMIT 500",
        (media, dir))
    for row in rows:
        row["playable"] = row["ext"] in PLAYABLE[media]
    return {"files": rows, "target": state.get_settings(engine.db)[media]["target"]}


@router.get("/files/stream")
def stream_file(media: str = "audio", relpath: str = "", engine=Engine):
    """The file itself, for the player in the Directories tab. Range requests are supported,
    so the player can seek without downloading the whole file first."""
    _media_check(media)
    path = _stream_path(engine, media, relpath)
    ext = path.rsplit(".", 1)[-1].lower()
    return FileResponse(
        path,
        media_type=STREAM_TYPES.get(ext, "application/octet-stream"),
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/files/measure")
def measure(body: MeasureBody, engine=Engine):
    _media_check(body.media)
    if ".." in body.relpath.split("/") or body.relpath.startswith("/"):
        raise HTTPException(400, "invalid path")
    return engine.measure(body.media, body.relpath)


@router.post("/files/check-batch")
def check_batch(body: FilesBatchBody, engine=Engine):
    _media_check(body.media)
    return engine.check_files(body.media, ids=body.ids or None, dir_rel=body.dir)


@router.post("/files/execute-batch")
def execute_batch(body: FilesBatchBody, engine=Engine):
    _media_check(body.media)
    return engine.execute_files(body.media, ids=body.ids or None, dir_rel=body.dir)


@router.get("/files/info")
def file_info(media: str = "audio", relpath: str = "", engine=Engine):
    """Length and codec of one file; the editor uses it before the levels are read."""
    _media_check(media)
    return waveform.info(engine.cfg.ffmpeg, _stream_path(engine, media, relpath))


@router.get("/files/waveform")
def file_waveform(media: str = "audio", relpath: str = "", start: float = 0.0,
                  duration: float | None = None, buckets: int = 1200, engine=Engine):
    """Levels over time for the timeline of the editor: one (min, max) pair per bucket."""
    _media_check(media)
    return waveform.peaks(engine.cfg.ffmpeg, _stream_path(engine, media, relpath),
                          start=start, duration=duration, buckets=buckets)


@router.post("/files/edit")
def edit_file(body: EditBody, engine=Engine):
    """Fade in/out and/or remove a part of one audio file, in place (with backup when set)."""
    _media_check(body.media)
    if ".." in body.relpath.split("/") or body.relpath.startswith("/"):
        raise HTTPException(400, "invalid path")
    cut = None
    if body.cut_start is not None or body.cut_end is not None:
        if body.cut_start is None or body.cut_end is None:
            raise HTTPException(400, "a cut needs both a start and an end")
        cut = (body.cut_start, body.cut_end)
    return engine.edit_audio(body.media, body.relpath, fade_in=body.fade_in, fade_out=body.fade_out, cut=cut)


# ------------------------------------------------------------ parked files

@router.get("/parked")
def list_parked(media: str = "audio", reason: str | None = None, sort: str = "order", engine=Engine):
    _media_check(media)
    if sort not in SORTS:
        raise HTTPException(400, "unknown sort")
    enabled = folders.enabled_set(engine.db, media)
    settings = state.get_settings(engine.db)[media]
    args: list = [media]
    where = "media = ? AND status = 'parked'"
    if reason:
        where += " AND reason = ?"
        args.append(reason)
    rows = engine.db.query(
        f"SELECT id, name, relpath, dir, ext, lufs, true_peak, gain_db, adjust_db, method, reason "
        f"FROM files WHERE {where} ORDER BY {SORTS[sort]}", args)
    out = []
    for r in rows:
        if r["dir"] not in enabled:
            continue
        effective = round((r["gain_db"] or 0.0) + (r["adjust_db"] or 0.0), 1)
        out.append({**r, "effective": effective,
                    "warn": abs(effective) > settings["threshold"]})
    counts = {"large_change": 0, "lossy_no_gain_tool": 0}
    for r in out:
        counts[r["reason"]] = counts.get(r["reason"], 0) + 1
    return {"files": out, "counts": counts, "threshold": settings["threshold"], "target": settings["target"]}


def _parked_row(engine, fid: int) -> dict:
    row = engine.db.one("SELECT * FROM files WHERE id = ?", (fid,))
    if row is None:
        raise HTTPException(404, "file not found")
    return row


@router.post("/parked/{fid}/approve")
def approve(fid: int, engine=Engine):
    row = _parked_row(engine, fid)
    if row["status"] != "parked":
        raise HTTPException(409, "file is not parked")
    engine.db.execute("UPDATE files SET status = 'approved' WHERE id = ?", (fid,))
    engine.db.log("info", f"{row['name']}: change approved", row["media"])
    engine.wake()
    return {"ok": True}


@router.post("/parked/{fid}/skip")
def skip(fid: int, engine=Engine):
    row = _parked_row(engine, fid)
    if row["status"] != "parked":
        raise HTTPException(409, "file is not parked")
    engine.db.execute("UPDATE files SET status = 'skipped', reason = 'skipped by user' WHERE id = ?", (fid,))
    return {"ok": True}


def _set_adjust(engine, row: dict, body: AdjustBody) -> None:
    if row["gain_db"] is None:
        raise HTTPException(409, "no change stored for this file")
    if body.value is None and body.delta is None:
        raise HTTPException(400, "give value or delta")
    current = (row["gain_db"] or 0.0) + (row["adjust_db"] or 0.0)
    target = body.value if body.value is not None else current + body.delta
    # the range is +/-12 dB, widened when the measured change is already larger, so that stepping
    # never jumps away from the original value
    gain = float(row["gain_db"])
    lo, hi = min(-12.0, gain), max(12.0, gain)
    target = max(lo, min(hi, round(target, 1)))
    adjust = round(target - gain, 2)
    engine.db.execute("UPDATE files SET adjust_db = ? WHERE id = ?", (adjust, row["id"]))


@router.post("/parked/{fid}/adjust")
def adjust(fid: int, body: AdjustBody, engine=Engine):
    row = _parked_row(engine, fid)
    if row["status"] != "parked":
        raise HTTPException(409, "file is not parked")
    _set_adjust(engine, row, body)
    return {"ok": True}


@router.post("/parked/bulk")
def bulk(body: BulkBody, engine=Engine):
    done = 0
    for fid in body.ids:
        row = engine.db.one("SELECT * FROM files WHERE id = ? AND status = 'parked'", (fid,))
        if row is None:
            continue
        if body.action == "approve":
            engine.db.execute("UPDATE files SET status = 'approved' WHERE id = ?", (fid,))
        elif body.action == "skip":
            engine.db.execute("UPDATE files SET status = 'skipped', reason = 'skipped by user' WHERE id = ?", (fid,))
        else:
            _set_adjust(engine, row, AdjustBody(delta=body.delta or 0.0))
        done += 1
    if body.action == "approve" and done:
        engine.wake()
    return {"done": done}


@router.post("/parked/approve-all")
def approve_all(body: ApproveAllBody, engine=Engine):
    enabled = folders.enabled_set(engine.db, body.media)
    rows = engine.db.query("SELECT id, dir FROM files WHERE media = ? AND status = 'parked' AND reason = ?",
                           (body.media, body.reason))
    ids = [r["id"] for r in rows if r["dir"] in enabled]
    for fid in ids:
        engine.db.execute("UPDATE files SET status = 'approved' WHERE id = ?", (fid,))
    if ids:
        engine.db.log("info", f"{len(ids)} parked file(s) approved", body.media)
        engine.wake()
    return {"done": len(ids)}


# ------------------------------------------------------------ settings and schedule

@router.get("/settings")
def get_settings(engine=Engine):
    return state.get_settings(engine.db)


@router.put("/settings")
def put_settings(body: dict, engine=Engine):
    settings = state.put_settings(engine.db, body)
    engine.apply_mode()               # off stops the run, automatic starts it, bulk/force stops an automatic one
    return settings


@router.get("/schedule")
def get_schedule(engine=Engine):
    return state.get_schedule(engine.db)


@router.put("/schedule")
def put_schedule(body: dict, engine=Engine):
    return state.put_schedule(engine.db, body)     # the scan loop picks a new interval up within two seconds


# ------------------------------------------------------------ Home Assistant

def _mask(secret: str) -> str:
    return ("\u2022\u2022\u2022\u2022 " + secret[-4:]) if secret else ""


def _ha_view(engine) -> dict:
    cfg = state.get_ha(engine.db)
    # the Home Assistant access token is never sent back to the browser, only a masked hint
    token = cfg.get("token", "")
    return {
        **cfg,
        "token": _mask(token),
        "token_set": bool(token),
        "port": engine.cfg.port,
        "yaml": ha.yaml(cfg, engine.cfg.port),
        "card_yaml": ha.card_yaml(cfg),
        "enabled_paths": ha.enabled_paths(cfg),
    }


@router.get("/ha")
def get_ha(engine=Engine):
    return _ha_view(engine)


@router.put("/ha")
def put_ha(body: HaBody, engine=Engine):
    state.save_ha(engine.db, body.model_dump(exclude_none=True))
    return _ha_view(engine)


@router.post("/ha/test")
def ha_test(engine=Engine):
    cfg = state.get_ha(engine.db)
    return ha.test_connection(cfg["url"], cfg["token"])


@router.post("/ha/rotate-token")
def ha_rotate(engine=Engine):
    state.rotate_endpoint_token(engine.db)
    return _ha_view(engine)


@router.get("/v1/ha/status")
def ha_status(authorization: str | None = Header(default=None), engine=Engine):
    token = state.get_ha(engine.db)["endpointToken"]
    if not authorization or not hmac.compare_digest(authorization, f"Bearer {token}"):
        raise HTTPException(401, "missing or invalid token")
    return ha.payload(engine)
