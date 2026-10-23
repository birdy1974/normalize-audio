"""Tests for the Meta data tab & per-file metadata popup:
- 8-field tag reading & writing across audio formats while preserving file mtime
- Folder/file name vs tag comparison and override
- Online metadata check, custom inline editing, selection, and timestamp-preserving apply."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app import metadata as meta_mod
from app.main import create_app
from conftest import tone


@pytest.fixture
def client(cfg):
    app = create_app(cfg, start_threads=False)
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


@pytest.mark.parametrize(
    "ext,codec_args",
    [
        ("mp3", ("-c:a", "libmp3lame", "-b:a", "128k")),
        ("flac", ("-c:a", "flac")),
        ("m4a", ("-c:a", "aac", "-b:a", "128k")),
        ("ogg", ("-c:a", "libvorbis", "-b:a", "128k")),
        ("wav", ("-c:a", "pcm_s16le")),
    ],
)
def test_read_and_write_full_tags_preserves_mtime(cfg, ext, codec_args):
    """Writing metadata must update all 7 text fields and keep the exact file modification timestamp."""
    path = tone(os.path.join(cfg.audio_root, "MetaFormats", f"track.{ext}"), -3, codec_args=codec_args)
    fixed_mtime = 1_650_123_456
    os.utime(path, (fixed_mtime, fixed_mtime))

    updates = {
        "artist": "Pink Floyd",
        "title": "Time",
        "album": "The Dark Side of the Moon",
        "album_artist": "Pink Floyd",
        "year": "1973",
        "track": "4",
        "genre": "Progressive Rock",
    }
    ok = meta_mod.write_full_tags(path, updates, preserve_mtime=True)
    assert ok is True

    # Verify the file modification timestamp did NOT change
    assert int(os.stat(path).st_mtime) == fixed_mtime

    tags = meta_mod.read_full_tags(path)
    assert tags["artist"] == "Pink Floyd"
    assert tags["title"] == "Time"
    assert tags["album"] == "The Dark Side of the Moon"
    assert tags["album_artist"] == "Pink Floyd"
    assert tags["year"] == "1973"
    assert tags["track"] == "4"
    assert tags["genre"] == "Progressive Rock"


def test_compare_file_metadata_detects_path_mismatch_and_online_diffs():
    relpath = "Daft Punk/Discovery (2001)/01 - One More Time.mp3"
    current_tags = {
        "artist": "Wrong Artist",
        "title": "Wrong Title",
        "album": "Wrong Album",
        "album_artist": "",
        "year": "",
        "track": "",
        "genre": "",
        "has_cover": False,
    }
    online_match = {
        "source": "Discogs",
        "score": 0.92,
        "artist": "Daft Punk",
        "title": "One More Time",
        "album": "Discovery",
        "album_artist": "Daft Punk",
        "year": "2001",
        "track": "1",
        "genre": "Electronic",
        "cover_url": "https://example.com/discovery.jpg",
    }
    opts = {
        "fields": ["artist", "title", "album", "album_artist", "year", "track", "genre", "cover"],
        "check_path_vs_tags": True,
        "sources": ["discogs", "musicbrainz", "open"],
    }
    cmp_res = meta_mod.compare_file_metadata(
        relpath=relpath,
        current_tags=current_tags,
        online_match=online_match,
        options=opts,
    )
    assert cmp_res["status"] == "diff"
    assert cmp_res["query_source"] == "path_override"
    assert cmp_res["path_diff_count"] == 3
    assert cmp_res["fields"]["artist"]["path_vs_tag_diff"] is True
    assert cmp_res["fields"]["artist"]["path"] == "Daft Punk"
    assert cmp_res["fields"]["artist"]["online"] == "Daft Punk"
    assert cmp_res["fields"]["year"]["path"] == "2001"
    assert cmp_res["fields"]["track"]["path"] == "1"


def test_metadata_api_workflow_check_select_custom_and_apply_preserving_mtime(client, cfg, monkeypatch):
    """End-to-end API test for:
    - /api/metadata/options
    - /api/metadata/check (only enabled folders)
    - /api/files/{id}/metadata/check (per-file popup)
    - /api/metadata/select (including custom inline edit value)
    - /api/metadata/apply (updates tags on disk without changing mtime)
    """
    p_enabled = tone(
        os.path.join(cfg.audio_root, "Fleetwood Mac", "Rumours", "02 - Dreams.mp3"),
        0,
        codec_args=("-c:a", "libmp3lame", "-b:a", "128k"),
    )
    p_disabled = tone(
        os.path.join(cfg.audio_root, "ExcludedBand", "Album", "01 - Song.mp3"),
        0,
        codec_args=("-c:a", "libmp3lame", "-b:a", "128k"),
    )
    fixed_mtime = 1_680_000_000
    os.utime(p_enabled, (fixed_mtime, fixed_mtime))

    def fake_online(artist, album, title, track="", options=None, settings=None, sources=None, db=None, force_refresh=False):
        return {
            "source": "MusicBrainz",
            "score": 0.95,
            "artist": "Fleetwood Mac",
            "title": "Dreams",
            "album": "Rumours",
            "album_artist": "Fleetwood Mac",
            "year": "1977",
            "track": "2",
            "genre": "Classic Rock",
            "cover_url": "",
        }

    monkeypatch.setattr(meta_mod, "lookup_online_metadata", fake_online)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    # Exclude ExcludedBand folder on the Directories tab
    assert client.post(
        "/api/folders/toggle",
        json={"media": "audio", "relpath": "ExcludedBand", "enabled": False},
    ).status_code == 200

    # Save metadata check options
    opt_res = client.put(
        "/api/metadata/options",
        json={
            "fields": ["artist", "title", "album", "album_artist", "year", "track", "genre"],
            "check_path_vs_tags": True,
            "sources": ["discogs", "musicbrainz"],
        },
    )
    assert opt_res.status_code == 200
    assert opt_res.json()["sources"] == ["discogs", "musicbrainz"]

    # Run batch metadata check on enabled folders
    chk = client.post("/api/metadata/check", json={"enabled_only": True})
    assert chk.status_code == 200
    assert chk.json()["checked"] == 1
    assert chk.json()["diff"] == 1

    # Fetch metadata overview
    overview = client.get("/api/metadata").json()
    assert overview["checked_count"] == 1
    assert overview["diff_count"] == 1
    item = overview["files"][0]
    fid = item["id"]
    assert item["name"] == "02 - Dreams.mp3"
    assert item["fields"]["artist"]["online"] == "Fleetwood Mac"
    assert item["fields"]["year"]["online"] == "1977"

    # Also test per-file metadata popup endpoint (/api/files/{fid}/metadata/check)
    single_chk = client.post(f"/api/files/{fid}/metadata/check", json={"force": False})
    assert single_chk.status_code == 200
    assert single_chk.json()["id"] == fid
    assert single_chk.json()["album_file_count"] == 1

    # Test inline custom edit (✎) selection for genre
    sel_res = client.post(
        "/api/metadata/select",
        json={"ids": [fid], "field": "genre", "choice": "custom", "custom_value": "Soft Rock / Pop Rock"},
    )
    assert sel_res.status_code == 200

    # Apply metadata updates to the file
    os.utime(p_enabled, (fixed_mtime, fixed_mtime))
    app_res = client.post("/api/metadata/apply", json={"ids": [fid], "scope": "file"})
    assert app_res.status_code == 200
    assert app_res.json()["updated"] == 1

    # Verify file timestamp (mtime) on disk was preserved!
    assert int(os.stat(p_enabled).st_mtime) == fixed_mtime

    # Verify tags were written to disk including the custom genre
    disk_tags = meta_mod.read_full_tags(p_enabled)
    assert disk_tags["artist"] == "Fleetwood Mac"
    assert disk_tags["title"] == "Dreams"
    assert disk_tags["album"] == "Rumours"
    assert disk_tags["year"] == "1977"
    assert disk_tags["track"] == "2"
    assert disk_tags["genre"] == "Soft Rock / Pop Rock"
