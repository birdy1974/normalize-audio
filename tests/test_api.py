"""REST API tests with FastAPI's TestClient: Home Assistant auth, folders, settings, parked files."""
from __future__ import annotations

import os

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
    for key in ("status", "progress_percent", "errors", "current_file", "last_run_finished", "audio", "video"):
        assert key in body, key
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


# ------------------------------------------------------------ settings

def test_settings_are_validated(client):
    assert client.put("/api/settings", json={"audio": {"target": 50}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"target": -31}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"lossy": "transcode"}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"writeMode": "trash"}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"outputDir": "relative/path"}}).status_code == 400
    assert client.put("/api/settings", json={"audio": {"unknown": 1}}).status_code == 400
    assert client.put("/api/settings", json={"nope": {}}).status_code == 400


def test_valid_settings_are_saved_and_rounded(client):
    r = client.put("/api/settings", json={"audio": {"target": -18.04, "lossy": "skip", "writeMode": "backup"}})
    assert r.status_code == 200
    saved = client.get("/api/settings").json()["audio"]
    assert saved["target"] == -18.0
    assert saved["lossy"] == "skip"
    assert saved["writeMode"] == "backup"


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
