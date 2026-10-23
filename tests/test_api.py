"""REST API tests with FastAPI's TestClient: Home Assistant auth, folders, settings, parked files."""
from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import tone


@pytest.fixture
def client(cfg):
    app = create_app(cfg, start_threads=False)
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


def _scan_library(cfg, client):
    tone(os.path.join(cfg.audio_root, "Music", "a.flac"), 0, codec_args=("-c:a", "flac"))
    tone(os.path.join(cfg.audio_root, "Old", "b.flac"), 0, codec_args=("-c:a", "flac"))
    tone(os.path.join(cfg.audio_root, "Old", "Deep", "c.flac"), 0, codec_args=("-c:a", "flac"))
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200


def _ha_token(client) -> str:
    return client.get("/api/ha").json()["endpointToken"]


# ------------------------------------------------------------ Home Assistant

def test_ha_status_requires_a_valid_bearer_token(client):
    assert client.get("/api/v1/ha/status").status_code == 401
    assert client.get("/api/v1/ha/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/ha/status", headers={"Authorization": "Basic abc"}).status_code == 401


def test_ha_status_returns_payload_with_the_right_token(client):
    token = _ha_token(client)
    assert len(token) >= 32
    r = client.get("/api/v1/ha/status", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    for key in ("status", "mode", "scan_every", "last_scan", "next_scan",
                "progress_percent", "errors", "current_file", "last_run_finished", "audio", "video"):
        assert key in body, key
    assert body["mode"] == "bulk" and body["scan_every"] == 15
    for media in ("audio", "video"):
        for key in ("in_schedule_window", "directories_total", "directories_enabled", "files_total",
                    "files_pending", "files_checked", "files_changed", "files_parked"):
            assert key in body[media], (media, key)


def test_ha_token_rotation_invalidates_the_old_token(client):
    old = _ha_token(client)
    assert client.post("/api/ha/rotate-token").status_code == 200
    new = _ha_token(client)
    assert new != old
    assert client.get("/api/v1/ha/status", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert client.get("/api/v1/ha/status", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_ha_yaml_uses_the_status_endpoint(client):
    view = client.get("/api/ha").json()
    assert "/api/v1/ha/status" in view["yaml"]
    assert "Bearer" in view["yaml"]


# ------------------------------------------------------------ folders

def test_folder_toggle_and_select_all_none(client, cfg):
    _scan_library(cfg, client)

    def enabled() -> int:
        return client.get("/api/dashboard").json()["media"]["audio"]["directories_enabled"]

    total = client.get("/api/dashboard").json()["media"]["audio"]["directories_total"]
    assert total >= 2
    assert enabled() == total                       # everything is enabled by default

    assert client.post("/api/folders/toggle", json={"media": "audio", "relpath": "Old", "enabled": False}).status_code == 200
    assert enabled() == total - 2                   # Old and its subfolder Old/Deep

    assert client.post("/api/folders/all", json={"media": "audio", "enabled": False}).status_code == 200
    assert enabled() == 0
    assert client.post("/api/folders/all", json={"media": "audio", "enabled": True}).status_code == 200
    assert enabled() == total


def test_folder_api_rejects_unknown_media(client):
    assert client.post("/api/folders/all", json={"media": "photos", "enabled": True}).status_code == 422
    assert client.get("/api/folders", params={"media": "photos"}).status_code == 400


def test_rescan_makes_new_folders_and_files_visible(client, cfg):
    """The Rescan button on the Directories tab uses this endpoint; the tree follows the disk."""
    _scan_library(cfg, client)
    names = {d["relpath"] for d in client.get("/api/folders/tree", params={"media": "audio"}).json()["dirs"]}
    assert "New" not in names

    # A folder and a file created after the previous scan.
    tone(os.path.join(cfg.audio_root, "New", "Album", "d.flac"), 0, codec_args=("-c:a", "flac"))
    r = client.post("/api/scan", json={"media": "audio"})
    assert r.status_code == 200
    assert r.json()["audio"]["new"] == 1

    data = client.get("/api/folders/tree", params={"media": "audio"}).json()
    by = {d["relpath"]: d for d in data["dirs"]}
    assert by["New"]["parent"] == "." and by["New/Album"]["parent"] == "New"
    assert by["New"]["files"] == 1 and by["New/Album"]["direct_files"] == 1

    # A new folder inherits the state of its parent, so a rescan never adds excluded files.
    assert client.post("/api/folders/toggle", json={"media": "audio", "relpath": "New", "enabled": False}).status_code == 200
    tone(os.path.join(cfg.audio_root, "New", "Album", "Later", "e.flac"), 0, codec_args=("-c:a", "flac"))
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    by = {d["relpath"]: d for d in client.get("/api/folders/tree", params={"media": "audio"}).json()["dirs"]}
    assert by["New/Album/Later"]["enabled"] is False

    # A folder removed from the disk disappears on the next scan.
    shutil.rmtree(os.path.join(cfg.audio_root, "Old", "Deep"))
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    names = {d["relpath"] for d in client.get("/api/folders/tree", params={"media": "audio"}).json()["dirs"]}
    assert "Old/Deep" not in names


def test_file_stream_serves_audio_and_supports_range_requests(client, cfg):
    """The player in the Directories tab streams the file itself, with seeking."""
    _scan_library(cfg, client)
    listing = client.get("/api/files", params={"media": "audio", "dir": "Old"}).json()["files"]
    row = next(f for f in listing if f["name"] == "b.flac")
    assert row["playable"] is True

    r = client.get("/api/files/stream", params={"media": "audio", "relpath": "Old/b.flac"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/flac"
    assert r.headers["accept-ranges"] == "bytes"
    with open(os.path.join(cfg.audio_root, "Old", "b.flac"), "rb") as fh:
        assert r.content == fh.read()

    # a range request returns just that part: the player uses it to seek
    r = client.get("/api/files/stream", params={"media": "audio", "relpath": "Old/b.flac"},
                   headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert len(r.content) == 10
    assert r.headers["content-range"].startswith("bytes 0-9/")


def test_file_stream_rejects_paths_outside_the_base_and_unplayable_formats(client, cfg):
    _scan_library(cfg, client)
    with open(os.path.join(cfg.audio_root, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("not audio")
    base = "/api/files/stream"
    assert client.get(base, params={"media": "audio", "relpath": "../etc/passwd"}).status_code in (400, 404)
    assert client.get(base, params={"media": "audio", "relpath": "/etc/passwd"}).status_code == 400
    assert client.get(base, params={"media": "audio", "relpath": "notes.txt"}).status_code == 404
    assert client.get(base, params={"media": "audio", "relpath": "Old/missing.flac"}).status_code == 404
    assert client.get(base, params={"media": "photos", "relpath": "a.flac"}).status_code == 400
    # the audio endpoint does not hand out video files
    assert client.get(base, params={"media": "audio", "relpath": "a.mp4"}).status_code == 404


def test_waveform_returns_levels_for_the_whole_file_and_for_a_window(client, cfg):
    """The editor draws its timeline from these peaks."""
    _scan_library(cfg, client)
    base = client.get("/api/files/waveform", params={"media": "audio", "relpath": "Music/a.flac", "buckets": 200})
    assert base.status_code == 200
    data = base.json()
    assert abs(data["file_duration"] - 3.0) < 0.2
    assert data["duration"] == data["file_duration"] and data["start"] == 0
    assert len(data["min"]) == len(data["max"]) == 200
    assert max(data["max"]) > 0.1 and min(data["min"]) < -0.1      # the test tone swings both ways

    window = client.get("/api/files/waveform", params={
        "media": "audio", "relpath": "Music/a.flac", "start": 1, "duration": 1, "buckets": 50}).json()
    assert window["start"] == 1 and abs(window["duration"] - 1.0) < 0.05
    assert len(window["min"]) == 50 and max(window["max"]) > 0.1

    # past the end: the window is clamped instead of failing
    late = client.get("/api/files/waveform", params={
        "media": "audio", "relpath": "Music/a.flac", "start": 2.9, "duration": 5, "buckets": 50}).json()
    assert late["duration"] <= 0.11 and len(late["min"]) == 50

    assert client.get("/api/files/waveform", params={"media": "photos", "relpath": "a.flac"}).status_code == 400
    assert client.get("/api/files/waveform", params={"media": "audio", "relpath": "missing.flac"}).status_code == 404
    assert client.get("/api/files/waveform", params={"media": "audio", "relpath": "../a.flac"}).status_code == 400


def _tone_then_silence(path: str, seconds: float = 2.0) -> None:
    """Two seconds of silence, then a tone of the same length."""
    from conftest import FFMPEG, make
    make(path, "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-filter_complex", "[0][1]concat=n=2:v=0:a=1", "-c:a", "flac")


def test_edit_adds_fades_and_keeps_the_file_playable(client, cfg):
    _scan_library(cfg, client)
    r = client.post("/api/files/edit", json={"media": "audio", "relpath": "Music/a.flac",
                                             "fade_in": 1.0, "fade_out": 1.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert abs(body["duration"] - 3.0) < 0.2 and body["fade_in"] == 1.0 and body["fade_out"] == 1.0

    # the file is still a playable flac of the same length
    stream = client.get("/api/files/stream", params={"media": "audio", "relpath": "Music/a.flac"})
    assert stream.status_code == 200 and stream.headers["content-type"] == "audio/flac"
    peaks = client.get("/api/files/waveform", params={
        "media": "audio", "relpath": "Music/a.flac", "buckets": 60}).json()
    assert len(peaks["max"]) == 60
    assert peaks["max"][0] < 0.02                      # faded in from silence
    assert peaks["max"][-1] < 0.02                     # and out again
    assert max(peaks["max"][20:40]) > 0.1              # the middle is untouched

    # the level changed, so the file is measured again in the next run
    row = client.app_state.db.one("SELECT status, lufs, gain_db FROM files WHERE relpath = 'Music/a.flac'")
    assert row["status"] == "pending" and row["lufs"] is None and row["gain_db"] is None
    messages = [l["msg"] for l in client.app_state.db.log_list()]
    assert any("Edited a.flac" in m and "fade in" in m for m in messages)


def test_edit_can_remove_a_part_and_reports_the_new_length(client, cfg):
    _tone_then_silence(os.path.join(cfg.audio_root, "Cut", "mixed.flac"))
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    # remove one second of silence and one second of the tone: 1 s silence + 1 s tone remains
    r = client.post("/api/files/edit", json={"media": "audio", "relpath": "Cut/mixed.flac",
                                             "cut_start": 1.0, "cut_end": 3.0})
    assert r.status_code == 200, r.text
    assert abs(r.json()["duration"] - 2.0) < 0.2
    peaks = client.get("/api/files/waveform", params={
        "media": "audio", "relpath": "Cut/mixed.flac", "buckets": 40}).json()
    assert abs(peaks["file_duration"] - 2.0) < 0.2
    half = len(peaks["max"]) // 2
    assert max(peaks["max"][:half]) < 0.02             # silence first
    assert max(peaks["max"][half:]) > 0.1              # then the tone

    # a second cut keeps the rest intact and updates the length again
    r = client.post("/api/files/edit", json={"media": "audio", "relpath": "Cut/mixed.flac",
                                             "cut_start": 0.5, "cut_end": 1.0})
    assert r.status_code == 200, r.text
    assert abs(r.json()["duration"] - 1.5) < 0.2


def test_info_reports_length_and_codec(client, cfg):
    os.makedirs(os.path.join(cfg.audio_root, "Info"), exist_ok=True)
    tone(os.path.join(cfg.audio_root, "Info", "i.mp3"), 0, seconds=3.0,
         codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    r = client.get("/api/files/info", params={"media": "audio", "relpath": "Info/i.mp3"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert abs(d["duration"] - 3.0) < 0.2 and d["codec"] == "mp3" and d["size"] > 1000
    assert client.get("/api/files/info", params={"media": "audio", "relpath": "Info/nope.mp3"}).status_code == 404
    assert client.get("/api/files/info", params={"media": "audio", "relpath": "../x.mp3"}).status_code == 400


def test_edit_and_waveform_refuse_files_outside_the_base_location(client, cfg):
    _scan_library(cfg, client)
    link = os.path.join(cfg.audio_root, "escape.flac")
    os.symlink("/etc/hostname", link)
    assert client.get("/api/files/stream", params={"media": "audio", "relpath": "escape.flac"}).status_code == 404
    assert client.get("/api/files/waveform", params={"media": "audio", "relpath": "escape.flac"}).status_code == 404
    r = client.post("/api/files/edit", json={"media": "audio", "relpath": "escape.flac", "fade_in": 1.0})
    assert r.status_code in (400, 404)
    os.remove(link)


def test_edit_cuts_at_the_edges_of_the_file(client, cfg):
    """A cut at the start or at the end must not confuse the filter graph."""
    tone(os.path.join(cfg.audio_root, "Edges", "e.flac"), 0, seconds=4.0, codec_args=("-c:a", "flac"))
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200

    r = client.post("/api/files/edit", json={"media": "audio", "relpath": "Edges/e.flac",
                                             "cut_start": 0.0, "cut_end": 1.5})
    assert r.status_code == 200, r.text
    assert abs(r.json()["duration"] - 2.5) < 0.2

    end = r.json()["duration"]
    r = client.post("/api/files/edit", json={"media": "audio", "relpath": "Edges/e.flac",
                                             "cut_start": end - 1.0, "cut_end": end})
    assert r.status_code == 200, r.text
    assert abs(r.json()["duration"] - 1.5) < 0.2
    peaks = client.get("/api/files/waveform", params={
        "media": "audio", "relpath": "Edges/e.flac", "buckets": 20}).json()
    assert abs(peaks["file_duration"] - 1.5) < 0.2 and max(peaks["max"]) > 0.1


def test_edit_rejects_bad_requests_and_busy_files(client, cfg):
    _scan_library(cfg, client)
    post = lambda **body: client.post("/api/files/edit", json={"media": "audio", **body})
    assert post(relpath="Music/a.flac").status_code == 400                       # nothing to do
    assert post(relpath="Music/a.flac", fade_in=5.0, fade_out=5.0).status_code == 400   # fades overlap
    assert post(relpath="Music/a.flac", fade_in=10.0).status_code == 400         # longer than the file
    assert post(relpath="Music/a.flac", cut_start=2.0).status_code == 400        # half a cut
    assert post(relpath="Music/a.flac", cut_start=2.0, cut_end=1.0).status_code == 400
    assert post(relpath="Music/a.flac", cut_start=0.0, cut_end=9.0).status_code == 400
    assert post(relpath="Music/a.flac", cut_start=0.0, cut_end=3.0).status_code == 400   # everything
    assert post(relpath="Music/missing.flac", fade_in=1.0).status_code == 400
    assert post(relpath="../a.flac", fade_in=1.0).status_code == 400
    assert client.post("/api/files/edit", json={"media": "video", "relpath": "a.mp4",
                                                "fade_in": 1.0}).status_code == 422

    client.app_state.db.execute("UPDATE files SET status = 'processing' WHERE relpath = 'Music/a.flac'")
    assert post(relpath="Music/a.flac", fade_in=1.0).status_code == 409
    client.app_state.db.execute("UPDATE files SET status = 'pending' WHERE relpath = 'Music/a.flac'")

    client.put("/api/settings", json={"general": {"dryRun": True}})
    r = post(relpath="Music/a.flac", fade_in=1.0)
    assert r.status_code == 400 and "Dry run" in r.json()["detail"]
    client.put("/api/settings", json={"general": {"dryRun": False}})

    client.put("/api/settings", json={"audio": {"writeMode": "output", "outputDir": "/tmp/out"}})
    r = post(relpath="Music/a.flac", fade_in=1.0)
    assert r.status_code == 400 and "output folder" in r.json()["detail"]


def test_edit_keeps_a_backup_when_the_write_mode_says_so(client, cfg):
    _scan_library(cfg, client)
    client.put("/api/settings", json={"audio": {"writeMode": "backup"}})
    original = os.path.join(cfg.audio_root, "Music", "a.flac")
    with open(original, "rb") as fh:
        before = fh.read()
    assert client.post("/api/files/edit", json={"media": "audio", "relpath": "Music/a.flac",
                                                "fade_in": 1.0}).status_code == 200
    with open(original, "rb") as fh:
        assert fh.read() != before
    backups = []
    for dirpath, _dirs, names in os.walk(cfg.backup_dir):
        backups += [os.path.join(dirpath, n) for n in names]
    assert len(backups) == 1
    with open(backups[0], "rb") as fh:
        assert fh.read() == before


def test_dashboard_reports_that_a_scan_is_running(client, cfg):
    """The web UI shows the periodic scan in its activity indicator."""
    _scan_library(cfg, client)
    assert client.get("/api/dashboard").json()["scanning"] is False

    engine = client.app_state.engine
    original = engine.scanner.scan
    started, release = threading.Event(), threading.Event()

    def slow_scan(media):
        started.set()
        release.wait(5)
        return original(media)

    engine.scanner.scan = slow_scan
    worker = threading.Thread(target=lambda: engine.scan_now("audio"))
    worker.start()
    try:
        assert started.wait(5)
        assert client.get("/api/dashboard").json()["scanning"] is True
    finally:
        release.set()
        worker.join(5)
        engine.scanner.scan = original
    assert client.get("/api/dashboard").json()["scanning"] is False


# ------------------------------------------------------------ settings

def test_settings_are_validated(client):
    assert client.put("/api/settings", json={"audio": {"target": 50}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"target": -31}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"hysteresis": -0.5}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"hysteresis": 6.5}}).status_code == 400
    assert client.put("/api/settings", json={"general": {"silenceLevelPct": 0.0}}).status_code == 400
    assert client.put("/api/settings", json={"general": {"silencePeakDb": -5.0}}).status_code == 400
    assert client.put("/api/settings", json={"general": {"silenceMinDuration": 0.1}}).status_code == 400
    assert client.put("/api/settings", json={"general": {"silenceFadeOut": -1.0}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"lossy": "transcode"}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"writeMode": "trash"}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"outputDir": "relative/path"}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"unknown": 1}}).status_code == 400
    assert client.put("/api/settings", json={"nope": {}}).status_code == 400


def test_valid_settings_are_saved_and_rounded(client):
    r = client.put("/api/settings", json={
        "general": {
            "trimSilence": True, "silenceLevelPct": 2.54, "silencePeakDb": -42.0,
            "silenceMinDuration": 4.5, "silenceFadeOut": 0.8, "silenceFadeIn": 0.6,
        },
        "audio": {"target": -18.04, "hysteresis": 1.24, "lossy": "skip", "writeMode": "backup"},
    })
    assert r.status_code == 200
    full = client.get("/api/settings").json()
    saved = full["audio"]
    assert saved["target"] == -18.0
    assert saved["hysteresis"] == 1.2
    assert saved["lossy"] == "skip"
    assert saved["writeMode"] == "backup"
    gen = full["general"]
    assert gen["trimSilence"] is True
    assert gen["silenceLevelPct"] == 2.5
    assert gen["silencePeakDb"] == -42.0
    assert gen["silenceMinDuration"] == 4.5
    assert gen["silenceFadeOut"] == 0.8
    assert gen["silenceFadeIn"] == 0.6


# ------------------------------------------------------------ parked files

def _park(client, app_state, name: str, gain: float, reason: str = "large_change") -> int:
    db = app_state.db
    db.execute("UPDATE files SET status = 'parked', reason = ?, gain_db = ?, adjust_db = 0 WHERE name = ?",
               (reason, gain, name))
    return db.one("SELECT id FROM files WHERE name = ?", (name,))["id"]


def test_parked_list_and_adjust_approve_skip(client, cfg):
    _scan_library(cfg, client)
    fid = _park(client, client.app_state, "a.flac", 8.0)
    _park(client, client.app_state, "b.flac", 4.0)

    parked = client.get("/api/parked", params={"media": "audio"}).json()
    row = next(f for f in parked["files"] if f["name"] == "a.flac")
    assert row["effective"] == 8.0 and row["warn"] is True       # 8 dB is over the 6 dB threshold
    assert parked["counts"]["large_change"] == 2

    # adjust to an absolute value: the stored change is the difference to the measured gain
    assert client.post(f"/api/parked/{fid}/adjust", json={"value": 5.0}).status_code == 200
    row = next(f for f in client.get("/api/parked").json()["files"] if f["name"] == "a.flac")
    assert row["effective"] == 5.0 and row["warn"] is False

    # small step relative to the current value
    assert client.post(f"/api/parked/{fid}/adjust", json={"delta": 0.1}).status_code == 200
    row = next(f for f in client.get("/api/parked").json()["files"] if f["name"] == "a.flac")
    assert row["effective"] == 5.1

    # the change is clamped to +/-12 dB
    assert client.post(f"/api/parked/{fid}/adjust", json={"value": 50}).status_code == 200
    row = next(f for f in client.get("/api/parked").json()["files"] if f["name"] == "a.flac")
    assert row["effective"] == 12.0

    assert client.post(f"/api/parked/{fid}/adjust", json={}).status_code == 400

    assert client.post(f"/api/parked/{fid}/approve").status_code == 200
    assert client.post(f"/api/parked/{fid}/approve").status_code == 409     # no longer parked
    assert client.post(f"/api/parked/{fid}/adjust", json={"value": 1.0}).status_code == 409

    other = client.app_state.db.one("SELECT id FROM files WHERE name = 'b.flac'")["id"]
    assert client.post(f"/api/parked/{other}/skip").status_code == 200
    assert client.app_state.db.one("SELECT status FROM files WHERE id = ?", (other,))["status"] == "skipped"


def test_parked_sort_and_unknown_ids(client, cfg):
    _scan_library(cfg, client)
    _park(client, client.app_state, "a.flac", 8.0)
    _park(client, client.app_state, "b.flac", -3.0)
    order = [f["name"] for f in client.get("/api/parked", params={"sort": "change_asc"}).json()["files"]]
    assert order == ["b.flac", "a.flac"]     # most negative change first
    assert client.get("/api/parked", params={"sort": "bogus"}).status_code == 400
    assert client.post("/api/parked/999999/approve").status_code == 404


def test_bulk_adjust_and_approve_all(client, cfg):
    _scan_library(cfg, client)
    a = _park(client, client.app_state, "a.flac", 7.0)
    b = _park(client, client.app_state, "b.flac", 7.0)
    assert client.post("/api/parked/bulk", json={"ids": [a, b], "action": "adjust", "delta": -0.1}).json()["done"] == 2
    effective = {f["name"]: f["effective"] for f in client.get("/api/parked").json()["files"]}
    assert effective == {"a.flac": 6.9, "b.flac": 6.9}
    r = client.post("/api/parked/approve-all", json={"media": "audio", "reason": "large_change"})
    assert r.json()["done"] == 2
    assert client.post("/api/parked/approve-all", json={"media": "audio", "reason": "bogus"}).status_code == 422


def test_measure_rejects_paths_outside_the_base(client):
    r = client.post("/api/files/measure", json={"media": "audio", "relpath": "../etc/passwd"})
    assert r.status_code == 400
    r = client.post("/api/files/measure", json={"media": "audio", "relpath": "/etc/passwd"})
    assert r.status_code == 400


def test_folder_tree_counts_include_subfolders(client, cfg):
    _scan_library(cfg, client)
    data = client.get("/api/folders/tree", params={"media": "audio"}).json()
    by = {d["relpath"]: d for d in data["dirs"]}
    assert by["Old"]["files"] == 2 and by["Old"]["direct_files"] == 1       # b.flac + Deep/c.flac
    assert by["Old/Deep"]["files"] == 1 and by["Old/Deep"]["parent"] == "Old"
    assert by["Music"]["files"] == 1
    assert data["root_files"] == 0
    assert client.get("/api/folders/tree", params={"media": "photos"}).status_code == 400


def test_ha_token_is_never_returned_in_clear_text(client):
    assert client.put("/api/ha", json={"token": "secret-ha-token-123456"}).status_code == 200
    view = client.get("/api/ha").json()
    assert "secret-ha-token" not in client.get("/api/ha").text
    assert view["token_set"] is True and view["token"].endswith("3456")
    # no network in tests: an empty URL is refused before any request is made
    client.put("/api/ha", json={"url": ""})
    assert client.post("/api/ha/test").json()["ok"] is False


def test_adjust_keeps_the_original_value_reachable(client, cfg):
    _scan_library(cfg, client)
    fid = _park(client, client.app_state, "a.flac", 25.8)      # a very large change
    # a small step must not jump to the +/-12 dB limit
    assert client.post(f"/api/parked/{fid}/adjust", json={"delta": -0.1}).status_code == 200
    row = next(f for f in client.get("/api/parked").json()["files"] if f["name"] == "a.flac")
    assert row["effective"] == 25.7
    # values beyond the measured change are still clamped
    assert client.post(f"/api/parked/{fid}/adjust", json={"value": 80}).status_code == 200
    row = next(f for f in client.get("/api/parked").json()["files"] if f["name"] == "a.flac")
    assert row["effective"] == 25.8


def _sensor(view: dict, path: str) -> dict:
    return next(x for x in view["sensors"] if x["path"] == path)


def test_sensor_names_can_be_aligned_with_home_assistant(client):
    before = _sensor(client.get("/api/ha").json(), "audio.files_pending")
    assert before["name"] == before["default_name"] == "Audio files to do"
    # disable it first: a name change must not switch the sensor back on
    client.put("/api/ha", json={"sensors": [{"path": "audio.files_pending", "enabled": False}]})
    r = client.put("/api/ha", json={"sensors": [{"path": "audio.files_pending", "name": "  Podcast   queue  "}]})
    assert r.status_code == 200
    s = _sensor(r.json(), "audio.files_pending")
    assert s["name"] == "Podcast queue"                       # whitespace collapsed
    assert s["default_name"] == "Audio files to do"
    assert s["enabled"] is False
    assert "Podcast queue" not in r.json()["yaml"]            # disabled sensors are not in the YAML
    assert "audio.files_pending" not in r.json()["enabled_paths"]
    r = client.put("/api/ha", json={"sensors": [{"path": "audio.files_pending", "enabled": True}]})
    assert '- name: "Podcast queue"' in r.json()["yaml"]
    assert "unique_id: audio_normalizer_audio_files_pending" in r.json()["yaml"]   # id does not follow the name
    client.put("/api/ha", json={"sensors": [{"path": "audio.files_pending", "enabled": False}]})
    # reset to the default name
    r = client.put("/api/ha", json={"sensors": [{"path": "audio.files_pending", "name": s["default_name"]}]})
    assert _sensor(r.json(), "audio.files_pending")["name"] == "Audio files to do"


def test_sensor_name_validation_and_yaml_quoting(client):
    path = "video.files_parked"
    assert client.put("/api/ha", json={"sensors": [{"path": path, "name": "   "}]}).status_code == 400
    assert client.put("/api/ha", json={"sensors": [{"path": path, "name": "x" * 81}]}).status_code == 400
    assert client.put("/api/ha", json={"sensors": [{"path": path, "name": "bad\x07name"}]}).status_code == 400
    # a rejected name leaves the stored one unchanged
    assert _sensor(client.get("/api/ha").json(), path)["name"] == "Video files parked"
    # quotes are escaped instead of being removed
    r = client.put("/api/ha", json={"sensors": [{"path": path, "enabled": True, "name": 'Files "parked"'}]})
    assert r.status_code == 200
    assert '- name: "Files \\"parked\\""' in r.json()["yaml"]
    # unknown paths are ignored
    assert client.put("/api/ha", json={"sensors": [{"path": "evil.path", "name": "x"}]}).status_code == 200


# ------------------------------------------------------------ app modes (off / bulk / auto)

def test_dashboard_reports_the_mode_and_the_scan_schedule(client, cfg):
    d = client.get("/api/dashboard").json()
    assert d["mode"] == "bulk" and d["run_mode"] == "stopped" and d["status"] == "stopped"
    assert d["scan_every"] == 15 and d["last_scan"] is None
    assert d["next_scan"] is None                          # Bulk has no periodic scan

    _scan_library(cfg, client)                             # a manual scan still works in Bulk
    d = client.get("/api/dashboard").json()
    assert d["last_scan"] and d["next_scan"] is None

    assert client.put("/api/settings", json={"general": {"mode": "auto"}}).status_code == 200
    d = client.get("/api/dashboard").json()
    last, nxt = d["last_scan"], d["next_scan"]
    assert last and nxt
    gap = datetime.fromisoformat(nxt) - datetime.fromisoformat(last)
    assert gap == timedelta(minutes=15)                    # the interval from Settings > Scheduling

    # a shorter interval is used at once
    assert client.put("/api/schedule", json={"scanEvery": 5}).status_code == 200
    d = client.get("/api/dashboard").json()
    assert d["scan_every"] == 5
    assert datetime.fromisoformat(d["next_scan"]) - datetime.fromisoformat(d["last_scan"]) == timedelta(minutes=5)


def test_the_mode_setting_switches_the_app_on_and_off(client, cfg):
    _scan_library(cfg, client)

    # off: nothing is scanned or normalized, and a run cannot be started
    r = client.put("/api/settings", json={"general": {"mode": "off"}})
    assert r.status_code == 200 and r.json()["general"]["mode"] == "off"
    d = client.get("/api/dashboard").json()
    assert d["mode"] == "off" and d["status"] == "off" and d["run_mode"] == "stopped"
    assert d["next_scan"] is None                          # no scan is planned
    refused = client.post("/api/run/start")
    assert refused.status_code == 400 and "off" in refused.json()["detail"].lower()

    # automatic: the run starts itself, so Start is not needed and Stop is not how it is turned off
    assert client.put("/api/settings", json={"general": {"mode": "auto"}}).status_code == 200
    d = client.get("/api/dashboard").json()
    assert d["mode"] == "auto" and d["run_mode"] == "active"
    assert d["next_scan"]
    stopped = client.post("/api/run/stop")
    assert stopped.status_code == 400 and "automatic" in stopped.json()["detail"].lower()
    assert client.post("/api/run/pause").json()["status"] == "paused"
    assert client.post("/api/run/pause").json()["status"] in ("idle", "running", "waiting_window")

    # bulk: the automatic run stops and the user starts a run by hand
    assert client.put("/api/settings", json={"general": {"mode": "bulk"}}).status_code == 200
    d = client.get("/api/dashboard").json()
    assert d["mode"] == "bulk" and d["status"] == "stopped" and d["run_mode"] == "stopped"
    assert client.post("/api/run/start").status_code == 200
    assert client.get("/api/dashboard").json()["run_mode"] == "active"
    assert client.post("/api/run/stop").status_code == 200

    # force: stops an active run when selected, and re-queues files in enabled folders on Start
    client.app_state.db.execute("UPDATE files SET status = 'ok', measured_at = 1")
    assert client.post("/api/folders/toggle", json={"media": "audio", "relpath": "Old", "enabled": False}).status_code == 200
    assert client.put("/api/settings", json={"general": {"mode": "force"}}).status_code == 200
    d = client.get("/api/dashboard").json()
    assert d["mode"] == "force" and d["status"] == "stopped" and d["run_mode"] == "stopped"
    assert d["next_scan"] is None
    assert client.post("/api/run/start").status_code == 200
    d = client.get("/api/dashboard").json()
    assert d["run_mode"] == "active"
    assert d["media"]["audio"]["files_pending"] == 1         # Music/a.flac re-queued; Old/* left untouched
    assert d["media"]["audio"]["files_checked"] == 0
    assert client.app_state.db.one("SELECT status FROM files WHERE relpath = 'Old/b.flac'")["status"] == "ok"
    assert client.post("/api/run/stop").status_code == 200

    # only the four modes exist
    assert client.put("/api/settings", json={"general": {"mode": "always"}}).status_code == 400


def test_the_lovelace_card_copy_matches_the_one_the_app_serves():
    """The card is shipped twice (served by the app and downloaded from GitHub); they must not drift."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "audio-normalizer-card.js"), "rb") as fh:
        served = fh.read()
    with open(os.path.join(root, "lovelace", "audio-normalizer-card.js"), "rb") as fb:
        published = fb.read()
    assert served == published


def test_ha_exposes_the_mode_and_the_scan_times(client, cfg):
    token = _ha_token(client)
    view = client.get("/api/ha").json()
    mode_sensor = _sensor(view, "mode")
    assert mode_sensor["name"] == "Mode" and mode_sensor["enabled"] is True
    assert '- name: "Mode"' in view["yaml"] and "value_json.mode" in view["yaml"]
    assert "          - mode" in view["yaml"]              # an attribute of the main entity as well
    assert _sensor(view, "last_scan")["deviceClass"] == "timestamp"
    assert _sensor(view, "next_scan")["enabled"] is False  # optional, like the other timestamps

    headers = {"Authorization": f"Bearer {token}"}
    body = client.get("/api/v1/ha/status", headers=headers).json()
    assert body["mode"] == "bulk"
    client.put("/api/settings", json={"general": {"mode": "force"}})
    body = client.get("/api/v1/ha/status", headers=headers).json()
    assert body["status"] == "stopped" and body["mode"] == "force" and body["next_scan"] is None
    client.put("/api/settings", json={"general": {"mode": "off"}})
    body = client.get("/api/v1/ha/status", headers=headers).json()
    assert body["status"] == "off" and body["mode"] == "off" and body["next_scan"] is None
