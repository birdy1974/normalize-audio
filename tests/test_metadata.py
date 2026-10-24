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
def test_write_full_tags_with_cover_and_replace_existing_cover(cfg, ext, codec_args):
    """Regression test: write_full_tags must embed AND replace cover art alongside text tags
    without failing on temporary filenames, preserving mtime across all supported formats."""
    import base64
    from app import cover as cover_mod

    jpeg1 = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00" + b"\x08" * 64 + b"\xff\xd9"
    png2 = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x00IEND\xaeB`\x82"
    url1 = "data:image/jpeg;base64," + base64.b64encode(jpeg1).decode("ascii")
    url2 = "data:image/png;base64," + base64.b64encode(png2).decode("ascii")

    path = tone(os.path.join(cfg.audio_root, "CoverAndTags", f"song.{ext}"), -3, codec_args=codec_args)
    fixed_mtime = 1_660_000_111
    os.utime(path, (fixed_mtime, fixed_mtime))

    meta_mod.write_full_tags(
        path,
        {"artist": "Dire Straits", "album": "Brothers in Arms", "year": "1985", "cover": url1},
        preserve_mtime=True,
    )
    assert int(os.stat(path).st_mtime) == fixed_mtime
    assert cover_mod.extract_cover(path) == (jpeg1, "image/jpeg")

    # Now replace existing cover AND update year/genre simultaneously
    meta_mod.write_full_tags(
        path,
        {"year": "1986", "genre": "Rock", "cover": url2},
        preserve_mtime=True,
    )
    assert int(os.stat(path).st_mtime) == fixed_mtime
    assert cover_mod.extract_cover(path) == (png2, "image/png")
    tags = meta_mod.read_full_tags(path)
    assert tags["artist"] == "Dire Straits"
    assert tags["year"] == "1986"
    assert tags["genre"] == "Rock"
    assert tags["cover"] == "present"


def test_per_source_columns_bulk_specific_fields_cover_replacement_and_db_sync(client, cfg, monkeypatch):
    """Verify:
    - Per-source columns (Discogs, MusicBrainz, Open) in compare & select
    - Replacing an already-present cover updates disk, SQLite (files + file_metadata), cover_updated_at, and /cover endpoint
    - Bulk changing ONLY specific parameters of selected files leaves unselected parameters untouched."""
    import base64
    from app import cover as cover_mod

    jpeg_old = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00" + b"\x04" * 64 + b"\xff\xd9"
    png_new = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x00IEND\xaeB`\x82"
    url_new = "data:image/png;base64," + base64.b64encode(png_new).decode("ascii")

    p1 = tone(os.path.join(cfg.audio_root, "AlbumDir", "01 - First.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    p2 = tone(os.path.join(cfg.audio_root, "AlbumDir", "02 - Second.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    cover_mod.embed_cover(p1, jpeg_old, mime="image/jpeg")
    meta_mod.write_full_tags(p1, {"artist": "Old Artist", "title": "Old Title 1", "album": "Old Album"})
    meta_mod.write_full_tags(p2, {"artist": "Old Artist", "title": "Old Title 2", "album": "Old Album"})

    def fake_online(artist, album, title, track="", options=None, settings=None, sources=None, db=None, force_refresh=False, cancel=None):
        return {
            "source": "Discogs",
            "score": 0.94,
            "artist": "Discogs Artist",
            "title": f"Discogs {title}",
            "album": "Discogs Album",
            "year": "1991",
            "genre": "Synth-pop",
            "cover": url_new,
            "by_source": {
                "discogs": {"source": "Discogs", "artist": "Discogs Artist", "album": "Discogs Album", "year": "1991", "genre": "Synth-pop", "cover": url_new},
                "musicbrainz": {"source": "MusicBrainz", "artist": "MB Artist", "album": "MB Album", "year": "1992", "genre": "Electronic"},
                "open": {"source": "Open (iTunes)", "artist": "iTunes Artist", "album": "iTunes Album", "year": "1993", "genre": "Pop"},
            },
        }

    monkeypatch.setattr(meta_mod, "lookup_online_metadata", fake_online)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    assert client.post("/api/metadata/check", json={"enabled_only": True}).status_code == 200

    res = client.get("/api/metadata").json()
    assert len(res["files"]) == 2
    f1, f2 = res["files"][0], res["files"][1]
    assert f1["fields"]["album"]["discogs"] == "Discogs Album"
    assert f1["fields"]["album"]["musicbrainz"] == "MB Album"
    assert f1["fields"]["album"]["open"] == "iTunes Album"

    # Initial cover on f1 is jpeg_old
    cov_before = client.get(f"/api/files/{f1['id']}/cover")
    assert cov_before.status_code == 200
    assert cov_before.content == jpeg_old
    assert "no-store" in cov_before.headers.get("cache-control", "")

    # 1) Select MusicBrainz for album on f1, and Discogs cover to REPLACE existing cover on f1
    assert client.post("/api/metadata/select", json={"ids": [f1["id"]], "field": "album", "choice": "musicbrainz"}).status_code == 200
    assert client.post("/api/metadata/select", json={"ids": [f1["id"]], "field": "cover", "choice": "discogs"}).status_code == 200
    app1 = client.post("/api/metadata/apply", json={"ids": [f1["id"]], "fields": ["album", "cover"]})
    assert app1.status_code == 200
    assert app1.json()["updated"] == 1

    # Verify new cover is served immediately and SQLite files + file_metadata are synced
    cov_after = client.get(f"/api/files/{f1['id']}/cover")
    assert cov_after.status_code == 200
    assert cov_after.content == png_new

    dir_rows = client.get("/api/files", params={"media": "audio", "dir": "AlbumDir"}).json()["files"]
    f1_dir = next(r for r in dir_rows if r["id"] == f1["id"])
    assert f1_dir["tag_album"] == "MB Album"
    assert f1_dir["cover_updated_at"] is not None

    # 2) Bulk change ONLY specific parameter 'genre' across both selected files (leaving 'title' untouched!)
    bulk_app = client.post(
        "/api/metadata/apply",
        json={
            "ids": [f1["id"], f2["id"]],
            "overrides": {"genre": {"choice": "custom", "value": "Ambient Techno"}},
            "fields": ["genre"],
        },
    )
    assert bulk_app.status_code == 200
    assert bulk_app.json()["updated"] == 2

    t1 = meta_mod.read_full_tags(p1)
    t2 = meta_mod.read_full_tags(p2)
    assert t1["genre"] == "Ambient Techno"
    assert t2["genre"] == "Ambient Techno"
    # Title was NOT in fields=['genre'], so original titles remain untouched!
    assert t1["title"] == "Old Title 1"
    assert t2["title"] == "Old Title 2"


def test_stop_metadata_check_halts_running_batch(client, cfg, monkeypatch):
    """POST /api/metadata/stop must halt a running check_metadata_batch immediately."""
    for idx in range(1, 5):
        tone(os.path.join(cfg.audio_root, "StopTest", f"0{idx} - Song.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))

    calls = 0

    def fake_online_stopping(artist, album, title, track="", options=None, settings=None, sources=None, db=None, force_refresh=False, cancel=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Trigger stop right after the first file's lookup starts
            client.app_state.engine.stop_metadata_check()
        return {"source": "Discogs", "score": 0.9, "artist": "Test", "album": "StopTest", "title": title}

    monkeypatch.setattr(meta_mod, "lookup_online_metadata", fake_online_stopping)
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200

    res = client.post("/api/metadata/check", json={"enabled_only": True}).json()
    assert res["stopped"] is True
    assert calls == 1

    stop_api = client.post("/api/metadata/stop")
    assert stop_api.status_code == 200
    assert stop_api.json()["ok"] is True


def test_partial_text_tags_preserved_and_synced_when_cover_download_fails(client, cfg, monkeypatch):
    """If an online cover URL 404s during apply_metadata, text ID3 updates must still be saved & synced to SQLite."""
    from app import cover as cover_mod

    p = tone(os.path.join(cfg.audio_root, "Resilient", "01 - Track.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    monkeypatch.setattr(
        cover_mod,
        "download_cover",
        lambda url, token="": (_ for _ in ()).throw(ValueError("HTTP 404 from cover server")),
    )
    monkeypatch.setattr(
        meta_mod,
        "lookup_online_metadata",
        lambda *a, **kw: {
            "source": "MusicBrainz",
            "score": 0.9,
            "artist": "New Artist",
            "album": "New Album",
            "title": "New Track",
            "year": "2020",
            "cover": "https://broken.example.invalid/404.jpg",
        },
    )

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    assert client.post("/api/metadata/check", json={"enabled_only": True}).status_code == 200
    fid = client.get("/api/metadata").json()["files"][0]["id"]

    res = client.post("/api/metadata/apply", json={"ids": [fid]}).json()
    assert res["updated"] == 1
    assert len(res["errors"]) == 1

    # Text tags WERE saved to disk AND synced to SQLite!
    disk = meta_mod.read_full_tags(p)
    assert disk["artist"] == "New Artist"
    assert disk["album"] == "New Album"
    assert disk["year"] == "2020"
    meta_after = client.get("/api/metadata").json()["files"][0]
    assert meta_after["current"]["artist"] == "New Artist"
    assert meta_after["current"]["year"] == "2020"

