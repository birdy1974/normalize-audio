"""REST API for the web UI (/api) and the Home Assistant status endpoint (/api/v1/ha/status)."""
from __future__ import annotations

import hmac
import json
import os
import threading
from typing import Literal
import weakref

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import __version__, cover, folders, ha, state, stats, waveform
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


class FolderInvert(BaseModel):
    media: Literal["audio", "video"]


class ScanBody(BaseModel):
    media: Literal["audio", "video"] | None = None
    full: bool = False
    dir: str | None = None


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


class CoverSearchBody(BaseModel):
    artist: str | None = None
    album: str | None = None
    title: str | None = None
    source: Literal["both", "discogs", "open"] | None = None
    query_mode: Literal["auto", "file", "metadata"] | None = None
    force: bool = False


class CoverApplyBody(BaseModel):
    image_url: str
    scope: Literal["file", "album", "dir"] = "file"
    target_ids: list[int] = Field(default_factory=list)
    only_missing: bool = False


class CoverCheckBody(BaseModel):
    ids: list[int] = Field(default_factory=list)
    dir: str | None = None
    enabled_only: bool = False


class CoverIdsBody(BaseModel):
    ids: list[int] = Field(default_factory=list)


class DiscogsTestBody(BaseModel):
    token: str | None = None


class MetadataCheckBody(BaseModel):
    ids: list[int] = Field(default_factory=list)
    dir: str | None = None
    enabled_only: bool = True
    force: bool = False
    background: bool = False
    options: dict | None = None


class FileMetadataCheckBody(BaseModel):
    force: bool = False
    query_mode: Literal["auto", "file", "metadata"] | None = None
    query: dict | None = None
    artist: str | None = None
    album: str | None = None
    title: str | None = None


class MetadataSelectBody(BaseModel):
    ids: list[int] = Field(default_factory=list)
    field: str | None = None
    choice: Literal["current", "path", "discogs", "musicbrainz", "open", "online", "custom"] = "online"
    value: str | None = None
    custom_value: str | None = None
    values: dict[str, str | dict] | None = None


class MetadataApplyBody(BaseModel):
    ids: list[int] = Field(default_factory=list)
    overrides: dict | None = None
    scope: Literal["file", "album"] = "file"
    fields: list[str] | None = None


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
    db_path = engine.db.path
    try:
        db_size = os.path.getsize(db_path) if os.path.isfile(db_path) else 0
    except OSError:
        db_size = 0
    return {
        "version": __version__,
        "roots": {"audio": cfg.audio_root, "video": cfg.video_root},
        "config_dir": cfg.config_dir,
        "persistence": {
            "db_path": db_path,
            "config_dir": cfg.config_dir,
            "db_exists": os.path.isfile(db_path),
            "db_size_bytes": db_size,
            "persistent": True,
        },
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
    approved_done, approved_total = engine.approved_progress(approved_pending)
    st_now = engine.status(media)
    current = engine.current_file() or approved_file
    if not current and st_now == "running":
        current = engine.next_pending_file(run["types"])
    return {
        "status": st_now,
        "mode": settings["general"]["mode"],      # off / bulk / force / auto: what the app does on its own
        "theme": settings["general"].get("theme", "light"),
        "run_mode": run["mode"],                  # the run engine: active / stopped
        "paused": run["paused"],
        "types": run["types"],
        "last_finished": run["lastFinished"],
        "scan_every": sched["scanEvery"],         # minutes between scans in Automatic mode
        "last_scan": run.get("lastScan"),
        "next_scan": engine.next_scan(),          # None unless Automatic mode is selected
        "progress_percent": int(round(100 * checked / total)) if total else 0,
        "progress_done": checked,
        "progress_total": total,
        "current_file": current,
        "approved_pending": approved_pending,
        "approved_done": approved_done,
        "approved_total": approved_total,
        "scanning": engine.scanning(),
        "fs_seq": engine.fs_seq(),
        "watcher_active": bool(engine.watcher.active),
        "meta_checking": engine.is_metadata_checking(),
        "meta_progress": engine.get_metadata_progress(),
        "action_progress": engine.get_action_progress(),
        "search_from": state.get_metadata_options(db)["search_from"],
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


@router.post("/actions/stop")
def actions_stop(engine=Engine):
    return engine.stop_all_actions()


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
    full = bool(body.full) if body else False
    target_dir = body.dir if body else None
    if target_dir and not media:
        raise HTTPException(400, "media is required when scanning a specific folder")
    if not full and target_dir is None:
        return engine.scan_now(media) if media is not None else engine.scan_now()
    return engine.scan_now(media, full=full, target_dir=target_dir)


# ------------------------------------------------------------ folders

_tree_cache_lock = threading.Lock()
_tree_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@router.get("/folders/tree")
def folder_tree(media: str = "audio", engine=Engine):
    """Every folder of one media type in one call (flat list; the UI builds the tree)."""
    _media_check(media)
    db = engine.db
    seq_before = getattr(db, "data_seq", None)
    if seq_before is not None:
        with _tree_cache_lock:
            per_db = _tree_cache.get(db)
            cached = per_db.get(media) if per_db else None
        if cached is not None and cached[0] == seq_before:
            return cached[1]

    with db._lock:
        seq = getattr(db, "data_seq", None)
        dirs = db.query_tuples(
            "SELECT relpath, parent, enabled FROM dirs WHERE media = ? AND relpath != '.' ORDER BY relpath",
            (media,),
        )
        direct = {
            r[0]: r[1]
            for r in db.query_tuples(
                "SELECT dir, COUNT(*) FROM files WHERE media = ? GROUP BY dir",
                (media,),
            )
        }
        seq_after = getattr(db, "data_seq", None)

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
    out = [{"relpath": relp, "name": relp.rsplit("/", 1)[-1], "parent": parent,
            "enabled": bool(en), "direct_files": direct.get(relp, 0),
            "files": subtree.get(relp, 0)} for relp, parent, en in dirs]
    payload = {"media": media, "dirs": out, "root_files": direct.get(".", 0)}
    if seq is not None and seq == seq_after:
        with _tree_cache_lock:
            _tree_cache.setdefault(db, {})[media] = (seq_after, payload)
    return payload


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


@router.post("/folders/invert")
def invert_folders(body: FolderInvert, engine=Engine):
    folders.invert_all(engine.db, _media_check(body.media))
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
        "SELECT id, media, name, relpath, dir, ext, lufs, true_peak, status, reason, gain_db, adjust_db, applied_db, "
        "method, measured_at, changed_at, silent_sections, silent_duration, silence_trimmed, "
        "has_cover, cover_status, cover_candidates, cover_updated_at, tag_artist, tag_album, tag_title, cover_query_source "
        "FROM files WHERE media = ? AND dir = ? ORDER BY name LIMIT 500",
        (media, dir))
    for row in rows:
        row["playable"] = row["ext"] in PLAYABLE[media]
        if media == "audio" and row.get("has_cover") is None:
            inspected = engine.inspect_file_cover(row)
            row["has_cover"] = inspected["has_cover"]
            row["cover_status"] = inspected["cover_status"]
            row["tag_artist"] = inspected["artist"]
            row["tag_album"] = inspected["album"]
            row["tag_title"] = inspected["title"]
            row["cover_query_source"] = inspected["source"]
        cands = []
        if row.get("cover_candidates"):
            try:
                cands = json.loads(row["cover_candidates"])
            except Exception:
                cands = []
        row["cover_choices_count"] = len(cands)
        row.pop("cover_candidates", None)
    return {"files": rows, "target": state.get_settings(engine.db)[media]["target"]}


@router.get("/files/{fid}/cover")
def file_cover(fid: int, engine=Engine):
    row = engine.db.one("SELECT media, relpath FROM files WHERE id = ?", (fid,))
    if not row or row["media"] != "audio":
        raise HTTPException(404, "audio file not found")
    path = engine.abs_path("audio", row["relpath"])
    extracted = cover.extract_cover(path)
    if not extracted:
        raise HTTPException(404, "no embedded cover art")
    data, mime = extracted
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/files/{fid}/local-cover")
def file_local_cover(fid: int, engine=Engine):
    row = engine.db.one("SELECT media, relpath FROM files WHERE id = ?", (fid,))
    if not row or row["media"] != "audio":
        raise HTTPException(404, "audio file not found")
    path = engine.abs_path("audio", row["relpath"])
    local = cover.find_local_cover(path)
    if not local or not local.get("image_url"):
        raise HTTPException(404, "no local folder cover")
    try:
        data, mime = cover.download_cover(local["image_url"])
    except Exception as exc:
        raise HTTPException(404, "could not read local cover") from exc
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.post("/files/{fid}/cover/search")
def file_cover_search(fid: int, body: CoverSearchBody | None = None, engine=Engine):
    b = body or CoverSearchBody()
    try:
        return engine.search_file_covers(
            fid,
            artist=b.artist,
            album=b.album,
            title=b.title,
            source_mode=b.source,
            query_mode=b.query_mode,
            force_refresh=b.force,
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/files/{fid}/cover/apply")
def file_cover_apply(fid: int, body: CoverApplyBody, engine=Engine):
    try:
        return engine.apply_cover(
            fid,
            body.image_url,
            scope=body.scope,
            target_ids=body.target_ids or None,
            only_missing=body.only_missing,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/covers/check")
def covers_check(body: CoverCheckBody, engine=Engine):
    return engine.check_covers_batch(
        ids=body.ids or None,
        dir_rel=body.dir,
        enabled_only=body.enabled_only,
    )


@router.get("/covers/parked")
def covers_parked(engine=Engine):
    """Return audio files with multiple cover choices or missing/not-found covers for the
    separate Cover Art pane on the Parked files tab (never mixed with audio normalization parked files)."""
    enabled = folders.enabled_set(engine.db, "audio")
    rows = engine.db.query(
        "SELECT id, name, relpath, dir, ext, has_cover, cover_status, cover_candidates, "
        "tag_artist, tag_album, tag_title, cover_query_source "
        "FROM files WHERE media = 'audio' AND cover_status IN ('choices', 'not_found') "
        "ORDER BY dir, name"
    )
    choices_files: list[dict] = []
    not_found_files: list[dict] = []
    albums_map: dict[tuple[str, str, str], dict] = {}

    for r in rows:
        if r["dir"] not in enabled:
            continue
        cands = []
        if r.get("cover_candidates"):
            try:
                cands = json.loads(r["cover_candidates"])
            except Exception:
                cands = []
        item = {
            "id": r["id"],
            "name": r["name"],
            "relpath": r["relpath"],
            "dir": r["dir"],
            "ext": r["ext"],
            "cover_status": r["cover_status"],
            "artist": r.get("tag_artist") or "",
            "album": r.get("tag_album") or "",
            "title": r.get("tag_title") or "",
            "query_source": r.get("cover_query_source") or "tags",
            "candidates": cands,
        }
        if r["cover_status"] == "choices" and cands:
            choices_files.append(item)
            key = (
                r["dir"],
                (item["artist"] or "").lower().strip(),
                (item["album"] or "").lower().strip(),
            )
            grp = albums_map.get(key)
            if grp is None:
                grp = {
                    "dir": r["dir"],
                    "artist": item["artist"],
                    "album": item["album"],
                    "query_source": item["query_source"],
                    "primary_file_id": r["id"],
                    "file_ids": [],
                    "files": [],
                    "candidates": cands,
                }
                albums_map[key] = grp
            grp["file_ids"].append(r["id"])
            grp["files"].append({"id": r["id"], "name": r["name"], "title": item["title"], "relpath": r["relpath"]})
        else:
            not_found_files.append(item)

    return {
        "albums": list(albums_map.values()),
        "choices_files": choices_files,
        "not_found_files": not_found_files,
        "counts": {
            "albums": len(albums_map),
            "choices": len(choices_files),
            "not_found": len(not_found_files),
            "total": len(choices_files) + len(not_found_files),
        },
    }


@router.post("/covers/skip")
def covers_skip(body: CoverIdsBody, engine=Engine):
    return engine.skip_cover(body.ids)


@router.post("/covers/apply-first-all")
def covers_apply_first_all(body: CoverIdsBody | None = None, engine=Engine):
    ids = body.ids if body else None
    try:
        return engine.apply_first_cover_all(ids=ids or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/covers/test-discogs")
def covers_test_discogs(body: DiscogsTestBody | None = None, engine=Engine):
    token = body.token if body and body.token is not None else state.get_settings(engine.db)["general"].get("discogsToken", "")
    return cover.test_discogs(token or "")


# ------------------------------------------------------------ metadata tab & per-file popup

@router.get("/metadata")
def get_metadata(engine=Engine):
    return engine.get_metadata_results()


@router.put("/metadata/options")
def put_metadata_options(body: dict, engine=Engine):
    return state.put_metadata_options(engine.db, body)


@router.post("/metadata/check")
def check_metadata(body: MetadataCheckBody | None = None, engine=Engine):
    b = body or MetadataCheckBody()
    if b.background:
        return engine.start_metadata_check(
            ids=b.ids or None,
            dir_rel=b.dir,
            options=b.options,
            force=b.force,
        )
    return engine.check_metadata_batch(
        ids=b.ids or None,
        dir_rel=b.dir,
        options=b.options,
        force=b.force,
    )


@router.post("/metadata/stop")
def stop_metadata(engine=Engine):
    return engine.stop_metadata_check()


@router.post("/files/{fid}/metadata/check")
def check_file_metadata(fid: int, body: FileMetadataCheckBody | None = None, engine=Engine):
    b = body or FileMetadataCheckBody()
    row = engine.db.one("SELECT * FROM files WHERE id = ?", (fid,))
    if not row or row["media"] != "audio":
        raise HTTPException(404, "audio file not found")
    custom_q = dict(b.query) if isinstance(b.query, dict) else {}
    for k in ("artist", "album", "title"):
        val = getattr(b, k, None)
        if val is not None:
            custom_q[k] = val
    try:
        return engine.check_file_metadata(
            row,
            force_refresh=b.force,
            custom_query=custom_q or None,
            query_mode=b.query_mode,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/metadata/select")
def select_metadata(body: MetadataSelectBody, engine=Engine):
    val = body.custom_value if body.custom_value is not None else body.value
    return engine.select_metadata_choice(
        body.ids,
        field=body.field,
        choice=body.choice,
        custom_value=val,
        updates_map=body.values,
    )


@router.post("/metadata/apply")
def apply_metadata(body: MetadataApplyBody, engine=Engine):
    try:
        return engine.apply_metadata(
            body.ids,
            field_overrides=body.overrides,
            scope=body.scope,
            only_fields=body.fields,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


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
    engine.note_approved(1)
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
        engine.note_approved(done)
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
        engine.note_approved(len(ids))
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
