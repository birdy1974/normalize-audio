"""End-to-end tests of the pipeline with real ffmpeg. Each test uses its own temporary library."""
from __future__ import annotations

import hashlib
import os
import time

import pytest

from app import ffmpeg as ff
from app import state
from app.engine import Engine
from app.db import Database
from app.media import MEDIA
from conftest import FFMPEG, make, tone

ALL_ON = [[True] * 24 for _ in range(7)]


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def set_settings(db, **audio):
    state.put_settings(db, {"audio": audio})


def open_windows(db):
    state.put_schedule(db, {"audio": ALL_ON, "video": ALL_ON})


def run_until_idle(engine: Engine, db: Database, timeout: float = 180) -> None:
    engine.tick()
    deadline = time.time() + timeout
    while time.time() < deadline:
        engine.tick()
        busy = db.query("SELECT 1 FROM files WHERE status IN ('pending', 'approved', 'processing') LIMIT 1")
        if engine.inflight_count() == 0 and not busy:
            return
        time.sleep(0.2)
    raise AssertionError("pipeline did not finish in time")


def row(db, name: str) -> dict:
    r = db.one("SELECT * FROM files WHERE name = ?", (name,))
    assert r is not None, name
    return r


def loudness(cfg_ffmpeg: str, path: str) -> float:
    return ff.measure(cfg_ffmpeg, path)[0]


@pytest.fixture
def lib(cfg, app_engine):
    app, engine, db = app_engine
    return cfg, engine, db


def test_quiet_flac_is_brought_to_target_and_keeps_mtime(lib):
    cfg, engine, db = lib
    path = tone(os.path.join(cfg.audio_root, "Music", "quiet.flac"), -6, codec_args=("-c:a", "flac"))
    os.utime(path, (1_600_000_000, 1_600_000_000))
    before = loudness(FFMPEG, path)
    set_settings(db, target=-16.0, threshold=12.0)
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)

    r = row(db, "quiet.flac")
    assert r["status"] == "changed", r
    after = loudness(FFMPEG, path)
    assert abs(after - (-16.0)) < 0.7, (before, after)
    assert int(os.stat(path).st_mtime) == 1_600_000_000          # time stamp restored
    assert r["applied_db"] is not None and r["applied_db"] > 0


def test_large_change_is_parked_then_approved_and_applied(lib):
    cfg, engine, db = lib
    path = tone(os.path.join(cfg.audio_root, "Podcasts", "faint.flac"), -20, codec_args=("-c:a", "flac"))
    set_settings(db, target=-16.0, threshold=6.0)
    open_windows(db)
    before = sha(path)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    r = row(db, "faint.flac")
    assert (r["status"], r["reason"]) == ("parked", "large_change")
    assert sha(path) == before  # parked means no write yet
    assert r["gain_db"] > 6.0
    untouched = loudness(FFMPEG, path)

    db.execute("UPDATE files SET status = 'approved' WHERE id = ?", (r["id"],))
    run_until_idle(engine, db)
    r = row(db, "faint.flac")
    assert r["status"] == "changed"
    assert abs(loudness(FFMPEG, path) - (-16.0)) < 0.7 and loudness(FFMPEG, path) > untouched + 5


def test_lossy_without_gain_tool_is_parked_and_can_be_reencoded(lib):
    cfg, engine, db = lib
    path = tone(os.path.join(cfg.audio_root, "Audiobooks", "book.ogg"), 0, codec_args=("-c:a", "libvorbis", "-b:a", "128k"))
    set_settings(db, target=-16.0, threshold=12.0)
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    r = row(db, "book.ogg")
    assert (r["status"], r["reason"]) == ("parked", "lossy_no_gain_tool")

    db.execute("UPDATE files SET status = 'approved' WHERE id = ?", (r["id"],))
    run_until_idle(engine, db)
    r = row(db, "book.ogg")
    assert r["status"] == "changed" and r["method"] == "reencode"
    assert abs(loudness(FFMPEG, path) - (-16.0)) < 1.0
    assert ff.probe(FFMPEG, path).codec == "vorbis"


def test_skip_strategy_skips_lossy_files(lib):
    cfg, engine, db = lib
    tone(os.path.join(cfg.audio_root, "a.mp3"), 0)
    set_settings(db, target=-16.0, threshold=12.0, lossy="skip")
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    assert row(db, "a.mp3")["status"] == "skipped"


def test_reencode_strategy_changes_lossy_at_same_codec(lib):
    cfg, engine, db = lib
    path = tone(os.path.join(cfg.audio_root, "b.mp3"), 0)
    set_settings(db, target=-16.0, threshold=12.0, lossy="reencode")
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    assert row(db, "b.mp3")["status"] == "changed"
    assert ff.probe(FFMPEG, path).codec == "mp3"
    assert abs(loudness(FFMPEG, path) - (-16.0)) < 1.0


def test_dry_run_writes_nothing(lib):
    cfg, engine, db = lib
    path = tone(os.path.join(cfg.audio_root, "c.flac"), 0, codec_args=("-c:a", "flac"))
    before = sha(path)
    set_settings(db, target=-16.0, threshold=12.0)
    state.put_settings(db, {"general": {"dryRun": True}})
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    assert row(db, "c.flac")["status"] == "dryrun"
    assert sha(path) == before


def test_backup_mode_keeps_original(lib):
    cfg, engine, db = lib
    path = tone(os.path.join(cfg.audio_root, "d.flac"), 0, codec_args=("-c:a", "flac"))
    original = sha(path)
    set_settings(db, target=-16.0, threshold=12.0, writeMode="backup")
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    assert row(db, "d.flac")["status"] == "changed"
    backup = os.path.join(cfg.backup_dir, "audio", "d.flac")
    assert os.path.exists(backup) and sha(backup) == original
    assert sha(path) != original


def test_output_mode_writes_copy_and_leaves_original(lib, tmp_path):
    cfg, engine, db = lib
    path = tone(os.path.join(cfg.audio_root, "Sub", "e.flac"), 0, codec_args=("-c:a", "flac"))
    original = sha(path)
    out = str(tmp_path / "out")
    set_settings(db, target=-16.0, threshold=12.0, writeMode="output", outputDir=out)
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    assert row(db, "e.flac")["status"] == "changed"
    assert sha(path) == original
    produced = os.path.join(out, "Sub", "e.flac")
    assert os.path.exists(produced)
    assert abs(loudness(FFMPEG, produced) - (-16.0)) < 0.7


def test_silent_file_is_skipped(lib):
    cfg, engine, db = lib
    make(os.path.join(cfg.audio_root, "silence.wav"), "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
         "-c:a", "pcm_s16le")
    set_settings(db, target=-16.0, threshold=12.0)
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)
    r = row(db, "silence.wav")
    assert (r["status"], r["reason"]) == ("skipped", "silent")


def test_video_keeps_video_stream_and_changes_audio(lib):
    cfg, engine, db = lib
    path = os.path.join(cfg.video_root, "Home", "clip.mp4")
    make(path, "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=3", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=3", "-af", "volume=0dB", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k")
    before = ff.probe(FFMPEG, path)
    state.put_settings(db, {"video": {"target": -16.0, "threshold": 12.0}})
    open_windows(db)
    engine.scan_now("video")
    engine.start()
    run_until_idle(engine, db)
    r = row(db, "clip.mp4")
    assert r["status"] == "changed", r
    after = ff.probe(FFMPEG, path)
    assert after.video_streams == before.video_streams == 1
    assert abs(loudness(FFMPEG, path) - (-16.0)) < 1.0


def test_restart_keeps_state_and_resets_interrupted_files(cfg):
    from app.main import create_app
    app = create_app(cfg, start_threads=False)
    db = app.state.db
    tone(os.path.join(cfg.audio_root, "x.flac"), 0, codec_args=("-c:a", "flac"))
    tone(os.path.join(cfg.audio_root, "y.flac"), 0, codec_args=("-c:a", "flac"))
    app.state.engine.scan_now("audio")
    db.execute("UPDATE files SET status = 'processing' WHERE name = 'x.flac'")
    db.execute("UPDATE files SET status = 'parked', reason = 'large_change' WHERE name = 'y.flac'")
    # simulate a restart: a second application instance on the same database
    app2 = create_app(cfg, start_threads=False)
    x = app2.state.db.one("SELECT status FROM files WHERE name = 'x.flac'")
    y = app2.state.db.one("SELECT status, reason FROM files WHERE name = 'y.flac'")
    assert x["status"] == "pending"                                # interrupted file is queued again
    assert (y["status"], y["reason"]) == ("parked", "large_change")  # parked files survive a restart
    assert app2.state.engine.status() == "stopped"


def test_new_and_changed_files_are_detected(lib):
    cfg, engine, db = lib
    first = tone(os.path.join(cfg.audio_root, "one.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    db.execute("UPDATE files SET status = 'ok', measured_at = 1 WHERE name = 'one.flac'")
    tone(os.path.join(cfg.audio_root, "two.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    assert row(db, "two.flac")["status"] == "pending"
    assert row(db, "one.flac")["status"] == "ok"
    tone(first, 0, seconds=4.0, codec_args=("-c:a", "flac"))        # rewritten: different size
    engine.scan_now("audio")
    assert row(db, "one.flac")["status"] == "pending"


def test_no_processing_outside_schedule_window(lib):
    cfg, engine, db = lib
    tone(os.path.join(cfg.audio_root, "night.flac"), 0, codec_args=("-c:a", "flac"))
    closed = [[False] * 24 for _ in range(7)]
    state.put_schedule(db, {"audio": closed, "video": closed})
    engine.scan_now("audio")
    engine.start()
    for _ in range(3):
        engine.tick()
    assert engine.inflight_count() == 0
    assert row(db, "night.flac")["status"] == "pending"
    assert engine.status() == "waiting_window"


def test_stopped_run_does_not_process(lib):
    cfg, engine, db = lib
    tone(os.path.join(cfg.audio_root, "idle.flac"), 0, codec_args=("-c:a", "flac"))
    open_windows(db)
    engine.scan_now("audio")
    engine.tick()
    assert row(db, "idle.flac")["status"] == "pending"
    assert engine.status() == "stopped"


def test_disabled_folder_is_not_processed(lib):
    from app import folders
    cfg, engine, db = lib
    tone(os.path.join(cfg.audio_root, "Old", "skip.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    folders.set_enabled(db, "audio", "Old", False)
    open_windows(db)
    engine.start()
    # tick() submits work synchronously, so an empty in-flight set means nothing was picked up
    engine.tick()
    assert engine.inflight_count() == 0
    assert row(db, "skip.flac")["status"] == "pending"
