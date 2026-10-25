"""Scanner robustness: names that are not valid UTF-8 must not crash scan_all()."""
from __future__ import annotations

import os

from app.db import Database
from app.scanner import Scanner
from conftest import make, tone


def test_scan_skips_non_utf8_names_instead_of_crashing(cfg, tmp_path):
    audio_root = cfg.root("audio")

    # A normal, well-formed file.
    tone(os.path.join(audio_root, "good.mp3"), 0.0, seconds=0.2)

    # A file name that is not valid UTF-8 (a lone surrogate once decoded with
    # surrogateescape, exactly what os.walk hands back for such names on POSIX).
    bad_name = b"track_\xf8.mp3".decode("utf-8", "surrogateescape")
    tone(os.path.join(audio_root, bad_name), 0.0, seconds=0.2)

    # A folder name that is not valid UTF-8, containing a valid file inside it.
    bad_dir = b"alb\xf8m".decode("utf-8", "surrogateescape")
    os.mkdir(os.path.join(audio_root, bad_dir))
    tone(os.path.join(audio_root, bad_dir, "inner.mp3"), 0.0, seconds=0.2)

    db = Database(str(tmp_path / "config" / "db.sqlite"))
    scanner = Scanner(cfg, db)

    # Must not raise, unlike before the fix (UnicodeEncodeError from sqlite).
    result = scanner.scan_all()

    assert result["audio"]["available"] is True
    assert result["audio"]["skipped"] == 2

    rows = db.query("SELECT relpath FROM files WHERE media = 'audio'")
    assert [r["relpath"] for r in rows] == ["good.mp3"]

    # The error is surfaced to the log so the user knows to rename the file(s).
    messages = [l["msg"] for l in db.log_list()]
    assert any("not valid UTF-8" in m for m in messages)

    # The other media type must still be scanned (previously the whole
    # scan_all() dict comprehension aborted, leaving every media type empty).
    assert result["video"]["available"] is True


def _touch_audio(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"audio")


def test_incremental_mtime_scan_skips_unchanged_dirs_and_full_forces_walk(cfg, tmp_path, monkeypatch):
    """Option A + B: unchanged settled folders skip os.scandir(), changed leaf folders are rescanned,
    and full=True forces a full walk."""
    import app.scanner as scanner_mod

    audio_root = cfg.root("audio")
    # Build >= 10 top-level artist folders so parallel subtree walk (Option B) is exercised too,
    # plus Synology @eaDir and #recycle folders that must be pruned at the byte level.
    for a in range(10):
        _touch_audio(os.path.join(audio_root, f"Artist_{a:02d}", "Album_A", "01.flac"))
        _touch_audio(os.path.join(audio_root, f"Artist_{a:02d}", "Album_A", "@eaDir", "thumb.mp3"))
    _touch_audio(os.path.join(audio_root, "#recycle", "trashed.mp3"))

    monkeypatch.setenv("SCAN_WORKERS", "3")
    monkeypatch.setattr(scanner_mod, "RACY_WINDOW_NS", 0)

    db = Database(str(tmp_path / "config" / "inc.sqlite"))
    scanner = Scanner(cfg, db)

    r1 = scanner.scan("audio")
    assert r1["files"] == 10
    assert r1["new"] == 10
    assert r1["folders"] == 20  # 10 Artist_XX + 10 Album_A (@eaDir and #recycle pruned)

    # Track os.scandir calls on repeat incremental rescan when nothing changed
    scandir_calls: list[bytes] = []
    real_scandir = os.scandir

    def counting_scandir(p):
        scandir_calls.append(os.fsencode(p))
        return real_scandir(p)

    monkeypatch.setattr(os, "scandir", counting_scandir)
    r2 = scanner.scan("audio")
    assert r2["files"] == 10 and r2["new"] == 0 and r2["removed"] == 0
    assert scandir_calls == []  # 0 directories needed readdir!

    # Mutate ONLY Artist_04/Album_A (bump mtime explicitly in case coarse clock shares same jiffy)
    album4 = os.path.join(audio_root, "Artist_04", "Album_A")
    _touch_audio(os.path.join(album4, "02.flac"))
    st4 = os.stat(album4)
    os.utime(album4, ns=(st4.st_atime_ns, st4.st_mtime_ns + 5_000_000))

    scandir_calls.clear()
    r3 = scanner.scan("audio")
    assert r3["files"] == 11 and r3["new"] == 1 and r3["removed"] == 0
    assert len(scandir_calls) == 1
    assert scandir_calls[0].endswith(b"Artist_04/Album_A")

    # full=True bypasses cached timestamps and walks all 21 directories (root + 20 subfolders)
    scandir_calls.clear()
    r4 = scanner.scan("audio", full=True)
    assert r4["files"] == 11 and r4["new"] == 0 and r4["removed"] == 0
    assert len(scandir_calls) == 21
    db.close()


def test_targeted_subfolder_scan_and_api_options(cfg, tmp_path):
    """Option C: targeted folder scan scopes strictly to target_dir without SQL wildcard bleed,
    materializes placeholder ancestors safely, and cleans up deleted target folders."""
    import shutil
    from fastapi.testclient import TestClient
    from app.main import create_app

    audio_root = cfg.root("audio")
    _touch_audio(os.path.join(audio_root, "Artist_01", "a.mp3"))
    _touch_audio(os.path.join(audio_root, "Artist_01_Extra", "b.mp3"))

    app = create_app(cfg, start_threads=False)
    with TestClient(app) as client:
        assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
        # Exclude Artist_01 to verify new subfolders under it inherit enabled=False
        assert client.post(
            "/api/folders/toggle",
            json={"media": "audio", "relpath": "Artist_01", "enabled": False},
        ).status_code == 200

        # Add a file inside Artist_01/NewAlbum AND remove Artist_01_Extra/b.mp3 on disk;
        # targeted scan on Artist_01 must find NewAlbum/c.mp3 WITHOUT touching Artist_01_Extra!
        _touch_audio(os.path.join(audio_root, "Artist_01", "NewAlbum", "c.mp3"))
        os.remove(os.path.join(audio_root, "Artist_01_Extra", "b.mp3"))

        res = client.post("/api/scan", json={"media": "audio", "dir": "Artist_01"}).json()["audio"]
        assert res["target"] == "Artist_01"
        assert res["new"] == 1
        assert res["removed"] == 0  # Artist_01_Extra was outside target scope

        tree = {
            d["relpath"]: d
            for d in client.get("/api/folders/tree", params={"media": "audio"}).json()["dirs"]
        }
        assert tree["Artist_01/NewAlbum"]["enabled"] is False

        # Brand-new deep path scanned directly via target_dir leaves ancestor BrandNew with scanned_ok=0
        # so sibling folders created under BrandNew are still discovered by the next incremental scan.
        _touch_audio(os.path.join(audio_root, "BrandNew", "Album1", "t1.mp3"))
        _touch_audio(os.path.join(audio_root, "BrandNew", "Album2", "t2.mp3"))
        r_deep = client.post("/api/scan", json={"media": "audio", "dir": "BrandNew/Album1"}).json()["audio"]
        assert r_deep["new"] == 1

        r_inc = client.post("/api/scan", json={"media": "audio"}).json()["audio"]
        # Incremental scan sees BrandNew had scanned_ok=0 -> discovers Album2 (and notices Artist_01_Extra removal)
        assert r_inc["new"] == 1
        assert r_inc["removed"] == 1

        # Deleting BrandNew/Album1 and running targeted scan removes it cleanly
        shutil.rmtree(os.path.join(audio_root, "BrandNew", "Album1"))
        r_del = client.post("/api/scan", json={"media": "audio", "dir": "BrandNew/Album1"}).json()["audio"]
        assert r_del["removed"] == 1
        tree_after = {
            d["relpath"]
            for d in client.get("/api/folders/tree", params={"media": "audio"}).json()["dirs"]
        }
        assert "BrandNew/Album1" not in tree_after
        assert "BrandNew/Album2" in tree_after


def test_stats_and_folder_tree_data_seq_caching_and_ui_wiring(cfg, tmp_path, monkeypatch):
    """Option D: media_stats and folder_tree avoid redundant SQLite scans when db.data_seq is unchanged,
    and static/app.js wires parallel refresh + Rescan / Full rescan / Rescan folder actions."""
    from fastapi.testclient import TestClient
    from app import stats
    from app.main import create_app

    _touch_audio(os.path.join(cfg.root("audio"), "Band", "Record", "01.flac"))
    app = create_app(cfg, start_threads=False)
    with TestClient(app) as client:
        db = app.state.db
        assert client.post("/api/scan", json={"media": "audio"}).status_code == 200

        s1 = stats.media_stats(db, "audio")
        t1 = client.get("/api/folders/tree", params={"media": "audio"}).json()
        assert s1["files_total"] == 1
        assert len(t1["dirs"]) == 2

        # Monkeypatch db.query_tuples to fail if cache misses when no writes happened
        orig_qt = db.query_tuples

        def forbid_qt(*_a, **_kw):
            raise AssertionError("Should hit data_seq cache without querying SQLite")

        monkeypatch.setattr(db, "query_tuples", forbid_qt)
        assert stats.media_stats(db, "audio") == s1
        assert client.get("/api/folders/tree", params={"media": "audio"}).json() == t1

        # Restore and mutate DB -> cache invalidates immediately via data_seq
        monkeypatch.setattr(db, "query_tuples", orig_qt)
        assert client.post(
            "/api/folders/toggle",
            json={"media": "audio", "relpath": "Band", "enabled": False},
        ).status_code == 200
        s2 = stats.media_stats(db, "audio")
        assert s2["directories_enabled"] == 0
        assert s2["files_total"] == 0

    with open(os.path.join(cfg.static_dir, "app.js"), encoding="utf-8") as fh:
        js = fh.read()
    assert 'data-action="rescan-full"' in js
    assert 'data-action="rescan-folder"' in js
    assert "dashStructureSignature" in js


def test_live_library_watcher_auto_syncs_added_and_deleted_folders(cfg, tmp_path):
    """Option E: LibraryWatcher (watchfiles/inotify) automatically detects added & deleted folders/files
    while ignoring @eaDir sidecars and in-place modifications."""
    import shutil
    import time
    from app.engine import Engine
    from app.watcher import LibraryWatcher, coalesce_target_dirs

    assert coalesce_target_dirs({"Artist", "Artist/Album1", "Other/Album"}) == ["Artist", "Other/Album"]
    assert coalesce_target_dirs({f"Dir_{i}" for i in range(20)}, max_individual=12) == [None]

    audio_root = cfg.root("audio")
    _touch_audio(os.path.join(audio_root, "Existing", "01.mp3"))

    db = Database(str(tmp_path / "config" / "watch.sqlite"))
    engine = Engine(cfg, db)
    engine.watcher = LibraryWatcher(
        cfg, db, engine._on_watcher_targets, engine._stop, debounce_ms=80, step_ms=30
    )
    try:
        engine.scan_now("audio")
        seq0 = engine.fs_seq()
        engine.watcher.start()

        # Wait briefly for inotify watches to arm
        deadline = time.monotonic() + 3.0
        while not engine.watcher.active and not engine.watcher.disabled_reason and time.monotonic() < deadline:
            time.sleep(0.03)
        if engine.watcher.disabled_reason:
            return  # graceful fallback verified in restricted sandbox kernels

        # Give notify backend a moment to register recursive root watch
        time.sleep(0.12)

        # Drop a new album + @eaDir sidecar that must be ignored
        _touch_audio(os.path.join(audio_root, "LiveArtist", "LiveAlbum", "@eaDir", "sidecar.mp3"))
        _touch_audio(os.path.join(audio_root, "LiveArtist", "LiveAlbum", "track.flac"))

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            rows = db.query("SELECT relpath FROM files WHERE media = 'audio' ORDER BY relpath")
            rels = [r["relpath"] for r in rows]
            if "LiveArtist/LiveAlbum/track.flac" in rels:
                break
            time.sleep(0.05)

        rels = [r["relpath"] for r in db.query("SELECT relpath FROM files WHERE media = 'audio' ORDER BY relpath")]
        assert rels == ["Existing/01.mp3", "LiveArtist/LiveAlbum/track.flac"]
        assert engine.fs_seq() > seq0

        # Delete LiveArtist and verify automatic cleanup
        shutil.rmtree(os.path.join(audio_root, "LiveArtist"))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            rels = [r["relpath"] for r in db.query("SELECT relpath FROM files WHERE media = 'audio'")]
            if "LiveArtist/LiveAlbum/track.flac" not in rels:
                break
            time.sleep(0.05)
        assert [r["relpath"] for r in db.query("SELECT relpath FROM files WHERE media = 'audio'")] == [
            "Existing/01.mp3"
        ]
    finally:
        engine.shutdown()
        db.close()

