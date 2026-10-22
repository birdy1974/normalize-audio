"""End-to-end tests of the pipeline with real ffmpeg. Each test uses its own temporary library."""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timedelta

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


def wait_until(engine: Engine, predicate, timeout: float = 30.0) -> None:
    """Run the scheduler until a condition holds (the scan threads are not started in tests)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        engine.tick()
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met in time")


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


def test_bulk_run_respects_the_schedule_window(lib):
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
    assert state.get_run(db)["mode"] == "active"       # Start does not bypass a closed window

    open_windows(db)
    wait_until(engine, lambda: row(db, "night.flac")["status"] == "changed")


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


def test_excluded_backlog_does_not_starve_enabled_files(lib):
    """The 300-file batch limit must be applied after the enabled-folder filter."""
    from app import folders
    cfg, engine, db = lib
    engine.scan_now("audio")
    now = time.time()
    db.executemany(
        "INSERT INTO files (media, relpath, dir, name, ext, size, mtime_ns, status, seen_at) "
        "VALUES ('audio', ?, 'Excluded', ?, 'flac', 1, 1, 'pending', ?)",
        [(f"Excluded/{n}.flac", f"{n}.flac", now) for n in range(301)],
    )
    db.execute("INSERT INTO dirs (media, relpath, parent, enabled) VALUES ('audio', 'Excluded', '.', 0)")
    wanted = tone(os.path.join(cfg.audio_root, "Enabled", "wanted.flac"), 0, codec_args=("-c:a", "flac"))
    stat = os.stat(wanted)
    db.execute("INSERT INTO dirs (media, relpath, parent, enabled) VALUES ('audio', 'Enabled', '.', 1)")
    db.execute(
        "INSERT INTO files (media, relpath, dir, name, ext, size, mtime_ns, status, seen_at) "
        "VALUES ('audio', 'Enabled/wanted.flac', 'Enabled', 'wanted.flac', 'flac', ?, ?, 'pending', ?)",
        (stat.st_size, stat.st_mtime_ns, now),
    )
    open_windows(db)
    set_settings(db, target=-16.0, threshold=12.0)

    engine.start()
    wait_until(engine, lambda: row(db, "wanted.flac")["status"] == "changed", timeout=30)

    assert db.one("SELECT status FROM files WHERE relpath = 'Enabled/wanted.flac'")["status"] == "changed"
    assert db.one("SELECT COUNT(*) AS n FROM files WHERE dir = 'Excluded' AND status != 'pending'")["n"] == 0
    assert folders.enabled_set(db, "audio") == {".", "Enabled"}


# ------------------------------------------------------------ app modes (off / bulk / auto)

def test_off_mode_does_nothing_and_refuses_start(lib):
    cfg, engine, db = lib
    tone(os.path.join(cfg.audio_root, "off.flac"), 0, codec_args=("-c:a", "flac"))
    open_windows(db)
    engine.scan_now("audio")                       # a manual Rescan still shows what is on disk
    state.put_settings(db, {"general": {"mode": "off"}})
    with pytest.raises(ValueError):
        engine.start()
    for _ in range(3):
        engine.tick()
    assert engine.status() == "off"
    assert state.get_run(db)["mode"] == "stopped"
    assert row(db, "off.flac")["status"] == "pending"
    assert engine.next_scan() is None              # nothing is planned while the app is off


def test_bulk_run_stops_when_the_files_that_were_here_are_done(lib):
    cfg, engine, db = lib
    tone(os.path.join(cfg.audio_root, "first.flac"), 0, codec_args=("-c:a", "flac"))
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    wait_until(engine, lambda: state.get_run(db)["mode"] == "stopped")
    assert row(db, "first.flac")["status"] == "changed"
    assert state.get_run(db)["lastFinished"]

    # a file that appears later waits for the next Start
    tone(os.path.join(cfg.audio_root, "later.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    for _ in range(3):
        engine.tick()
        time.sleep(0.1)
    assert row(db, "later.flac")["status"] == "pending"
    assert engine.status() == "stopped"

    engine.start()                                 # bulk: the user asks for the next batch
    wait_until(engine, lambda: row(db, "later.flac")["status"] == "changed")


def test_a_bulk_run_with_nothing_to_do_finishes_at_once(lib):
    cfg, engine, db = lib
    open_windows(db)
    engine.start()
    wait_until(engine, lambda: state.get_run(db)["mode"] == "stopped")
    assert state.get_run(db)["lastFinished"]
    assert engine.status() == "stopped"


def test_automatic_mode_normalizes_new_files_without_start(lib):
    cfg, engine, db = lib
    state.put_settings(db, {"general": {"mode": "auto"}})
    open_windows(db)
    tone(os.path.join(cfg.audio_root, "new.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    engine.tick()                                  # applies the mode: the run starts itself
    assert state.get_run(db)["mode"] == "active"
    wait_until(engine, lambda: row(db, "new.flac")["status"] == "changed")
    assert engine.status() == "idle"               # waiting for the next file
    assert state.get_run(db)["mode"] == "active"   # and still watching

    # a file that appears later is picked up on its own, too
    tone(os.path.join(cfg.audio_root, "second.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    wait_until(engine, lambda: row(db, "second.flac")["status"] == "changed")


def test_automatic_mode_waits_for_the_schedule_window(lib):
    cfg, engine, db = lib
    state.put_settings(db, {"general": {"mode": "auto"}})
    closed = [[False] * 24 for _ in range(7)]
    state.put_schedule(db, {"audio": closed, "video": closed})
    tone(os.path.join(cfg.audio_root, "outside.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    for _ in range(3):
        engine.tick()
        time.sleep(0.05)
    assert engine.status() == "waiting_window"
    assert row(db, "outside.flac")["status"] == "pending"

    open_windows(db)
    wait_until(engine, lambda: row(db, "outside.flac")["status"] == "changed")


def test_automatic_mode_pauses_and_resumes(lib):
    cfg, engine, db = lib
    state.put_settings(db, {"general": {"mode": "auto"}})
    open_windows(db)
    engine.tick()                                  # starts the automatic run
    engine.toggle_pause()
    assert engine.status() == "paused"
    tone(os.path.join(cfg.audio_root, "held.flac"), 0, codec_args=("-c:a", "flac"))
    engine.scan_now("audio")
    for _ in range(3):
        engine.tick()
        time.sleep(0.05)
    assert row(db, "held.flac")["status"] == "pending"
    engine.toggle_pause()
    wait_until(engine, lambda: row(db, "held.flac")["status"] == "changed")


def test_switching_the_mode_stops_or_starts_the_run(lib):
    cfg, engine, db = lib
    tone(os.path.join(cfg.audio_root, "mode.flac"), 0, codec_args=("-c:a", "flac"))
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    assert state.get_run(db)["mode"] == "active"

    state.put_settings(db, {"general": {"mode": "off"}})
    engine.tick()                                  # the API applies the mode at once; a tick does too
    assert state.get_run(db)["mode"] == "stopped"
    assert engine.status() == "off"

    state.put_settings(db, {"general": {"mode": "auto"}})
    engine.tick()
    assert state.get_run(db)["mode"] == "active"
    wait_until(engine, lambda: row(db, "mode.flac")["status"] == "changed")


def test_switching_from_a_paused_automatic_run_to_bulk_stops_it(lib):
    cfg, engine, db = lib
    open_windows(db)
    state.put_settings(db, {"general": {"mode": "auto"}})
    engine.tick()
    engine.toggle_pause()
    assert engine.status() == "paused"

    state.put_settings(db, {"general": {"mode": "bulk"}})
    engine.tick()
    run = state.get_run(db)
    assert (run["mode"], run["paused"], engine.status()) == ("stopped", False, "stopped")


def test_automatic_mode_survives_a_restart(cfg):
    from app.main import create_app
    db = create_app(cfg, start_threads=False).state.db
    state.put_settings(db, {"general": {"mode": "auto"}})
    open_windows(db)
    app2 = create_app(cfg, start_threads=False)    # the app is started again
    engine2 = app2.state.engine
    assert state.get_run(app2.state.db)["mode"] == "active"
    assert engine2.status() in ("idle", "running", "waiting_window")
    tone(os.path.join(cfg.audio_root, "after-restart.flac"), 0, codec_args=("-c:a", "flac"))
    engine2.scan_now("audio")
    wait_until(engine2, lambda: row(app2.state.db, "after-restart.flac")["status"] == "changed")


def test_a_pause_in_automatic_mode_survives_a_restart(cfg):
    from app.main import create_app
    app = create_app(cfg, start_threads=False)
    db = app.state.db
    state.put_settings(db, {"general": {"mode": "auto"}})
    app.state.engine.tick()                       # activates the automatic run
    app.state.engine.toggle_pause()
    assert state.get_run(db)["paused"] is True

    app2 = create_app(cfg, start_threads=False)   # a restart keeps the hold
    assert app2.state.engine.status() == "paused"
    tone(os.path.join(cfg.audio_root, "held2.flac"), 0, codec_args=("-c:a", "flac"))
    app2.state.engine.scan_now("audio")
    for _ in range(3):
        app2.state.engine.tick()
        time.sleep(0.1)
    assert row(app2.state.db, "held2.flac")["status"] == "pending"


def test_off_mode_survives_a_restart(cfg):
    from app.main import create_app
    app = create_app(cfg, start_threads=False)
    state.put_settings(app.state.db, {"general": {"mode": "off"}})
    app2 = create_app(cfg, start_threads=False)
    assert state.get_run(app2.state.db)["mode"] == "stopped"
    assert app2.state.engine.status() == "off"


def test_the_scan_interval_comes_from_the_settings(lib):
    cfg, engine, db = lib
    assert engine.scan_interval() == 15
    state.put_schedule(db, {"scanEvery": 5})
    assert engine.scan_interval() == 5
    engine.scan_now("audio")
    assert engine.last_scan() and engine.next_scan() is None  # manual Bulk scan, no automatic follow-up
    state.put_settings(db, {"general": {"mode": "auto"}})
    last, nxt = engine.last_scan(), engine.next_scan()
    assert last and nxt
    assert datetime.fromisoformat(nxt) - datetime.fromisoformat(last) == timedelta(minutes=5)
    state.put_settings(db, {"general": {"mode": "off"}})
    assert engine.next_scan() is None


def test_only_automatic_mode_runs_the_periodic_scanner(lib, monkeypatch):
    cfg, engine, db = lib

    class StopAfterWaitOrScan:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True

    stop = StopAfterWaitOrScan()
    scans = []
    monkeypatch.setattr(engine, "_stop", stop)

    def scan():
        scans.append(engine.now())
        stop.stopped = True

    monkeypatch.setattr(engine, "scan_now", scan)

    engine._scan_loop()
    assert scans == []                                       # Bulk waits; it does not scan

    stop.stopped = False
    state.put_settings(db, {"general": {"mode": "off"}})
    engine._scan_loop()
    assert scans == []                                       # Off does not scan either

    stop.stopped = False
    state.put_settings(db, {"general": {"mode": "auto"}})
    engine._scan_loop()
    assert len(scans) == 1                                   # Automatic scans immediately after a restart
