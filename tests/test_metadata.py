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


def test_bulk_parameter_edit_selects_updated_parameter_and_update_selected_writes_id3_preserving_timestamp(client, cfg, monkeypatch):
    """Verify:
    1. After bulk parameter edit (both custom values and source modes, even on fields disabled in check options),
       the updated parameters are selected in the metadata list.
    2. 'Update Selected ()' (/api/metadata/apply) writes ID3v2.3 + ID3v1 tags into the actual MP3 files on disk
       while keeping exact file modification timestamps unchanged, and keeps the updated parameter selected
       on 'current' rather than reverting to Discogs."""
    p1 = tone(os.path.join(cfg.audio_root, "BulkEditAlbum", "01 - Track One.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    p2 = tone(os.path.join(cfg.audio_root, "BulkEditAlbum", "02 - Track Two.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    meta_mod.write_full_tags(p1, {"artist": "Init Artist", "title": "Track One", "album": "Init Album", "genre": "Pop", "year": "1999"})
    meta_mod.write_full_tags(p2, {"artist": "Init Artist", "title": "Track Two", "album": "Init Album", "genre": "Pop", "year": "1999"})

    fixed_mtime_1 = 1_640_000_123
    fixed_mtime_2 = 1_640_000_456
    os.utime(p1, (fixed_mtime_1, fixed_mtime_1))
    os.utime(p2, (fixed_mtime_2, fixed_mtime_2))
    mtime_ns_1 = os.stat(p1).st_mtime_ns
    mtime_ns_2 = os.stat(p2).st_mtime_ns

    monkeypatch.setattr(
        meta_mod,
        "lookup_online_metadata",
        lambda artist, album, title, *a, **kw: {
            "source": "Discogs",
            "score": 0.95,
            "artist": "Discogs Artist",
            "album": "Discogs Album",
            "title": title,
            "year": "2001",
            "genre": "French House",
            "by_source": {
                "discogs": {"source": "Discogs", "artist": "Discogs Artist", "album": "Discogs Album", "year": "2001", "genre": "French House"},
                "musicbrainz": {"source": "MusicBrainz", "artist": "MB Artist", "album": "MB Album", "year": "2002", "genre": "Electronic"},
                "open": {"source": "Open (iTunes)", "artist": "iTunes Artist", "album": "iTunes Album", "year": "2003", "genre": "Dance"},
            },
        },
    )

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    # Even if 'genre' was unchecked in check options, bulk parameter edit must still select & apply it!
    assert client.put(
        "/api/metadata/options",
        json={"fields": ["artist", "title", "album", "year"], "check_path_vs_tags": True, "sources": ["discogs", "musicbrainz", "open"]},
    ).status_code == 200
    assert client.post("/api/metadata/check", json={"enabled_only": True}).status_code == 200

    files_before = client.get("/api/metadata").json()["files"]
    ids = [f["id"] for f in files_before]
    assert len(ids) == 2

    # 1) Bulk parameter edit: set genre -> Custom 'Synthwave', year -> MusicBrainz ('2002') in one atomic call
    sel_resp = client.post(
        "/api/metadata/select",
        json={
            "ids": ids,
            "values": {
                "genre": {"choice": "custom", "value": "Synthwave"},
                "year": {"choice": "musicbrainz", "value": ""},
            },
        },
    )
    assert sel_resp.status_code == 200
    assert sel_resp.json()["updated"] == 2

    # Verify both updated parameters are SELECTED in the list right after bulk edit!
    files_staged = client.get("/api/metadata").json()["files"]
    for item in files_staged:
        assert item["fields"]["genre"]["choice"] == "custom"
        assert item["fields"]["genre"]["custom_value"] == "Synthwave"
        assert item["fields"]["year"]["choice"] == "musicbrainz"

    # 2) Press "Update Selected ()" (/api/metadata/apply) to write ID3 tags into actual files on disk
    os.utime(p1, ns=(mtime_ns_1, mtime_ns_1))
    os.utime(p2, ns=(mtime_ns_2, mtime_ns_2))
    apply_resp = client.post("/api/metadata/apply", json={"ids": ids, "scope": "file"})
    assert apply_resp.status_code == 200
    assert apply_resp.json()["updated"] == 2

    # Verify exact file modification timestamps were strictly preserved!
    assert os.stat(p1).st_mtime_ns == mtime_ns_1
    assert os.stat(p2).st_mtime_ns == mtime_ns_2

    # Verify ID3v2.3 + ID3v1 tags were written into the actual MP3 files on disk
    for path in (p1, p2):
        disk_tags = meta_mod.read_full_tags(path)
        assert disk_tags["genre"] == "Synthwave"
        assert disk_tags["year"] == "2002"
        assert disk_tags["album"] == "Discogs Album"
        with open(path, "rb") as fh:
            fh.seek(-128, os.SEEK_END)
            assert fh.read(3) == b"TAG"

    # Verify in the list after disk write: updated parameters stay selected on 'current' (NOT reverting to 'discogs'!)
    files_after = client.get("/api/metadata").json()["files"]
    for item in files_after:
        assert item["fields"]["genre"]["current"] == "Synthwave"
        assert item["fields"]["genre"]["choice"] == "current"
        assert item["fields"]["year"]["current"] == "2002"
        assert item["fields"]["year"]["choice"] == "current"


def test_background_metadata_check_streams_intermediate_results_and_resumes_unchecked(client, cfg, monkeypatch):
    import threading
    import time

    p1 = os.path.join(cfg.audio_root, "StreamArtist - StreamAlbum", "01 - One.mp3")
    p2 = os.path.join(cfg.audio_root, "StreamArtist - StreamAlbum", "02 - Two.mp3")
    p3 = os.path.join(cfg.audio_root, "StreamArtist - StreamAlbum", "03 - Three.mp3")
    for idx, p in enumerate((p1, p2, p3), start=1):
        tone(p, 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
        meta_mod.write_full_tags(p, {"artist": "StreamArtist", "album": "StreamAlbum", "title": f"Track {idx}"})

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200

    first_done = threading.Event()
    release_second = threading.Event()
    calls: list[str] = []

    def slow_online(artist, album, title, *a, **kw):
        calls.append(title)
        if len(calls) == 2:
            first_done.set()
            release_second.wait(timeout=5.0)
        return {
            "artist": "StreamArtist",
            "album": "StreamAlbum",
            "title": title,
            "year": "2010",
            "genre": "Ambient",
            "source": "Discogs",
            "score": 0.96,
            "by_source": {
                "discogs": {
                    "artist": "StreamArtist",
                    "album": "StreamAlbum",
                    "title": title,
                    "year": "2010",
                    "genre": "Ambient",
                    "source": "Discogs",
                    "score": 0.96,
                }
            },
        }

    monkeypatch.setattr(meta_mod, "lookup_online_metadata", slow_online)

    # Start non-blocking background check
    start_resp = client.post("/api/metadata/check", json={"enabled_only": True, "force": False, "background": True})
    assert start_resp.status_code == 200
    assert start_resp.json()["started"] is True

    # Wait until file #1 has committed and file #2 is paused mid-flight
    assert first_done.wait(timeout=5.0)

    mid = client.get("/api/metadata").json()
    assert mid["meta_checking"] is True
    assert mid["meta_progress"]["running"] is True
    assert mid["meta_progress"]["total"] == 3
    assert mid["meta_progress"]["done"] == 1
    assert mid["checked_count"] == 1
    assert len(mid["files"]) == 1
    assert mid["files"][0]["name"] == "01 - One.mp3"

    # Stop after file #1 while file #2 is in-flight, then release file #2
    assert client.post("/api/metadata/stop").status_code == 200
    release_second.set()

    # Wait for background worker to finish stopping
    for _ in range(50):
        cur = client.get("/api/metadata").json()
        if not cur["meta_checking"]:
            break
        time.sleep(0.05)
    assert cur["meta_checking"] is False
    assert cur["checked_count"] == 1

    # Resume with "Check meta data" (force=False): should only check the 2 remaining unchecked files!
    calls.clear()
    resume_resp = client.post("/api/metadata/check", json={"enabled_only": True, "force": False})
    assert resume_resp.status_code == 200
    assert resume_resp.json()["checked"] == 2
    final = client.get("/api/metadata").json()
    assert final["checked_count"] == 3
    assert len(final["files"]) == 3


def test_ui_source_buttons_selected_count_and_invert_button_markup():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "app.js"), "r", encoding="utf-8") as fh:
        js = fh.read()
    for expected in (
        "Discogs (' + selCount + ')",
        "MusicBrainz (' + selCount + ')",
        "Open (iTunes) (' + selCount + ')",
        "Folder / File (' + selCount + ')",
        "Current tag (' + selCount + ')",
        "Update Selected (' + selCount + ')",
    ):
        assert expected in js

    idx_all = js.index('data-action="select-all">Select all</button>')
    idx_inv = js.index('data-action="invert-dirs"')
    idx_none = js.index('data-action="select-none">Select none</button>')
    assert idx_all < idx_inv < idx_none

    with open(os.path.join(root, "static", "index.html"), "r", encoding="utf-8") as hf:
        html_txt = hf.read()
    assert 'id="dirsBadge"' in html_txt
    assert "document.getElementById('dirsBadge')" in js

    with open(os.path.join(root, "Dockerfile"), "r", encoding="utf-8") as df:
        docker_txt = df.read()
    assert 'VOLUME ["/config"]' in docker_txt


def test_metadata_tab_and_popup_artist_title_from_file_or_metadata(client, cfg, monkeypatch):
    """Meta data tab (options + batch check) and per-file popup can take Artist / Title from file or from metadata."""
    from mutagen.id3 import ID3, TALB, TIT2, TPE1
    from app import metadata

    folder = os.path.join(cfg.audio_root, "PathBand", "PathRecord")
    p = tone(os.path.join(folder, "02 - PathSong.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["MetaBand"]))
    tags.add(TALB(encoding=3, text=["MetaRecord"]))
    tags.add(TIT2(encoding=3, text=["MetaSong"]))
    tags.save(p)

    seen = []

    def fake_lookup(artist, album, title, track="", options=None, settings=None, **kwargs):
        seen.append((artist, album, title))
        return {
            "source": "Discogs",
            "score": 0.95,
            "artist": artist,
            "album": album,
            "title": title,
            "by_source": {"discogs": {"artist": artist, "album": album, "title": title, "score": 0.95}},
        }

    monkeypatch.setattr(metadata, "lookup_online_metadata", fake_lookup)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    assert client.post("/api/folders/toggle", json={"media": "audio", "relpath": "PathBand/PathRecord", "enabled": True}).status_code == 200
    fid = client.get("/api/files", params={"media": "audio", "dir": "PathBand/PathRecord"}).json()["files"][0]["id"]

    # 1. Persistent Meta data tab option: searchFrom = "metadata"
    opt_res = client.put("/api/metadata/options", json={"searchFrom": "metadata"}).json()
    assert opt_res["search_from"] == "metadata"
    assert client.post("/api/metadata/check", json={"force": False}).status_code == 200
    assert seen[-1] == ("MetaBand", "MetaRecord", "MetaSong")

    # 2. Switch persistent Meta data tab option to "file" and run check (without force) -> re-queries using folder/file name!
    opt_res2 = client.put("/api/metadata/options", json={"searchFrom": "file"}).json()
    assert opt_res2["search_from"] == "file"
    assert client.post("/api/metadata/check", json={"force": False}).status_code == 200
    assert seen[-1] == ("PathBand", "PathRecord", "PathSong")

    # 3. Per-file Meta data popup explicit query_mode override ("metadata" vs "file")
    pop_meta = client.post(f"/api/files/{fid}/metadata/check", json={"query_mode": "metadata"}).json()
    assert pop_meta["query_mode"] == "metadata"
    assert pop_meta["query_source"] == "metadata"
    assert seen[-1] == ("MetaBand", "MetaRecord", "MetaSong")

    pop_file = client.post(f"/api/files/{fid}/metadata/check", json={"query_mode": "file"}).json()
    assert pop_file["query_mode"] == "file"
    assert pop_file["query_source"] == "file"
    assert seen[-1] == ("PathBand", "PathRecord", "PathSong")

    # 4. Verify UI buttons exist in static/app.js for Cover popup, Meta data tab, and Meta data popup
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "app.js"), "r", encoding="utf-8") as fh:
        js = fh.read()
    for token in (
        'data-action="cover-query-mode"',
        'data-action="meta-opt-search-from"',
        'data-action="meta-bulk-artist-title"',
        'data-action="meta-modal-query-mode"',
    ):
        assert token in js


def test_metadata_column_order_header_buttons_enabled_fields_and_selection_stability(client, cfg, monkeypatch):
    """Verify:
    - Custom/final column is in 2nd place (after File & folder) and renders final selected value + override.
    - Discogs (x), MusicBrainz (x), Open (iTunes) (x) buttons live inside their column headers in static/app.js.
    - Selecting a source for enabled parameters updates only enabled parameters (leaving disabled ones untouched).
    - Rapid/concurrent /api/metadata/select mutations serialize without jumping back, path values stay selectable
      even when check_path_vs_tags is off, and non-forced per-file popup check preserves user selections.
    """
    import concurrent.futures
    import re
    from mutagen.id3 import ID3, TALB, TCON, TIT2, TPE1, TYER
    from app import metadata as meta_mod

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "app.js"), "r", encoding="utf-8") as fh:
        js = fh.read()

    # 1. Column order: 'file' is 1st data col (after checkbox 'pick'), 'custom' is 2nd data col!
    spec_block = re.search(r"const META_COL_SPECS = \[(.*?)\];", js, re.S)
    assert spec_block
    col_keys = re.findall(r"\['([a-z]+)'", spec_block.group(1))
    assert col_keys[:3] == ["pick", "file", "custom"]
    assert "resolveFinalMetaValue" in js

    # 2. Source buttons moved inside <th> headers next to Discogs, MusicBrainz, Open (iTunes)
    for hdr_src in ("discogs", "musicbrainz", "open"):
        assert f'class="btn btn-sm meta-th-btn" data-action="meta-bulk-choice" data-choice="{hdr_src}"' in js

    # 3. End-to-end selection behavior: enabled-only bulk source apply + concurrent single-field selections + popup preservation
    folder = os.path.join(cfg.audio_root, "FolderArtist", "FolderAlbum")
    p = tone(os.path.join(folder, "03 - FolderTrack.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["OldArtist"]))
    tags.add(TALB(encoding=3, text=["OldAlbum"]))
    tags.add(TIT2(encoding=3, text=["OldTrack"]))
    tags.add(TYER(encoding=3, text=["1999"]))
    tags.add(TCON(encoding=3, text=["OldGenre"]))
    tags.save(p)

    def fake_lookup(artist, album, title, track="", options=None, settings=None, **kwargs):
        return {
            "source": "Discogs",
            "score": 0.95,
            "artist": "DiscogsArtist",
            "album": "DiscogsAlbum",
            "title": "DiscogsTrack",
            "year": "2022",
            "genre": "DiscogsGenre",
            "by_source": {
                "discogs": {
                    "artist": "DiscogsArtist",
                    "album": "DiscogsAlbum",
                    "title": "DiscogsTrack",
                    "year": "2022",
                    "genre": "DiscogsGenre",
                    "score": 0.95,
                },
                "musicbrainz": {
                    "artist": "MBArtist",
                    "album": "MBAlbum",
                    "title": "MBTrack",
                    "year": "2021",
                    "genre": "MBGenre",
                    "score": 0.91,
                },
                "open": {
                    "artist": "OpenArtist",
                    "album": "OpenAlbum",
                    "title": "OpenTrack",
                    "year": "2020",
                    "genre": "OpenGenre",
                    "score": 0.88,
                },
            },
        }

    monkeypatch.setattr(meta_mod, "lookup_online_metadata", fake_lookup)
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    assert client.post("/api/folders/toggle", json={"media": "audio", "relpath": "FolderArtist/FolderAlbum", "enabled": True}).status_code == 200

    # Enable ONLY artist, title, album (leave year & genre disabled!) and turn check_path_vs_tags False
    assert client.put(
        "/api/metadata/options",
        json={
            "fields": ["artist", "title", "album"],
            "check_path_vs_tags": False,
            "sources": ["discogs", "musicbrainz", "open"],
        },
    ).status_code == 200
    assert client.post("/api/metadata/check", json={"enabled_only": True}).status_code == 200

    f_item = client.get("/api/metadata").json()["files"][0]
    fid = f_item["id"]
    # Even with check_path_vs_tags=False, path value is populated so selecting 'path' never blanks or jumps back!
    assert f_item["fields"]["artist"]["path"] == "FolderArtist"
    assert f_item["fields"]["genre"]["choice"] == "current"  # disabled field stays current

    # Apply MusicBrainz column values for enabled parameters
    sel_mb = client.post("/api/metadata/select", json={"ids": [fid], "field": "all", "choice": "musicbrainz"}).json()
    assert sel_mb["updated"] == 1
    upd_fields = sel_mb["files_updated"][str(fid)]["fields"]
    assert upd_fields["artist"]["choice"] == "musicbrainz"
    assert upd_fields["title"]["choice"] == "musicbrainz"
    assert upd_fields["album"]["choice"] == "musicbrainz"
    assert upd_fields["genre"]["choice"] == "current"  # disabled parameter untouched!
    assert upd_fields["year"]["choice"] == "current"   # disabled parameter untouched!

    # Concurrent selections on different fields of the same file must serialize cleanly without losing any pick
    eng = client.app_state.engine
    mutations = [
        ("artist", "path", None),
        ("title", "open", None),
        ("album", "custom", "My Override Album"),
        ("genre", "discogs", None),
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = [
            pool.submit(eng.select_metadata_choice, [fid], fld, ch, cval)
            for (fld, ch, cval) in mutations
        ]
        for fut in futs:
            assert fut.result()["updated"] == 1

    after = client.get("/api/metadata").json()["files"][0]["fields"]
    assert after["artist"]["choice"] == "path"
    assert after["title"]["choice"] == "open"
    assert after["album"]["choice"] == "custom"
    assert after["album"]["custom_value"] == "My Override Album"
    assert after["genre"]["choice"] == "discogs"

    # Opening per-file metadata popup (force=False) must NOT wipe or jump back user selections!
    popup_data = client.post(f"/api/files/{fid}/metadata/check", json={"force": False}).json()
    assert popup_data["fields"]["artist"]["choice"] == "path"
    assert popup_data["fields"]["title"]["choice"] == "open"
    assert popup_data["fields"]["album"]["choice"] == "custom"
    assert popup_data["fields"]["album"]["custom_value"] == "My Override Album"
    assert popup_data["fields"]["genre"]["choice"] == "discogs"


@pytest.mark.parametrize("ext,codec_args", [
    ("mp3", ("-c:a", "libmp3lame", "-b:a", "128k")),
    ("flac", ("-c:a", "flac")),
    ("m4a", ("-c:a", "aac", "-b:a", "128k")),
    ("wav", ("-c:a", "pcm_s16le")),
])
def test_inplace_padding_write_and_single_pass_cover_avoid_audio_copy(tmp_path, monkeypatch, ext, codec_args):
    """Option A: initial write reserves 8 KiB padding in a single pass; subsequent tag & cover
    edits that fit in padding write directly in-place without copying the audio stream."""
    import shutil as shutil_mod
    from app import cover as cover_mod

    p = tone(str(tmp_path / f"inplace_test.{ext}"), 0, codec_args=codec_args)
    fixed_ns = 1_700_123_456_789_123_456
    os.utime(p, ns=(fixed_ns, fixed_ns))

    jpeg_a = b"\xff\xd8\xff\xe0" + b"\x22" * 512 + b"\xff\xd9"
    jpeg_b = b"\xff\xd8\xff\xe0" + b"\x33" * 400 + b"\xff\xd9"
    jpeg_huge = b"\xff\xd8\xff\xe0" + b"\x44" * (24 * 1024) + b"\xff\xd9"
    monkeypatch.setattr(
        cover_mod,
        "download_cover",
        lambda url, token="": (jpeg_a if "a.jpg" in url else (jpeg_b if "b.jpg" in url else jpeg_huge), "image/jpeg"),
    )

    real_copy2 = shutil_mod.copy2
    copy_calls = []

    def tracked_copy2(src, dst, *args, **kwargs):
        copy_calls.append((src, dst))
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil_mod, "copy2", tracked_copy2)

    # 1. Single-pass text + cover write copies at most ONCE (never twice!)
    assert meta_mod.write_full_tags(
        p,
        {"artist": "Initial Artist", "title": "Initial Title", "album": "Album 1", "cover": "https://img/a.jpg"},
        preserve_mtime=True,
    ) is True
    assert len(copy_calls) <= 1
    assert os.stat(p).st_mtime_ns == fixed_ns

    # 2. Subsequent text-tag + equal/smaller cover updates fit in reserved 8 KiB padding -> ZERO file copies!
    copy_calls.clear()
    st_mid = os.stat(p)
    assert meta_mod.write_full_tags(
        p,
        {
            "artist": "Updated Artist",
            "title": "Updated Title",
            "album": "Updated Album",
            "year": "2024",
            "track": "5",
            "genre": "Synthwave",
            "cover": "https://img/b.jpg",
        },
        preserve_mtime=True,
    ) is True
    st_after_inplace = os.stat(p)
    assert copy_calls == [], f"expected zero shutil.copy2 calls for in-place {ext} save"
    assert st_after_inplace.st_ino == st_mid.st_ino
    assert st_after_inplace.st_size == st_mid.st_size
    assert st_after_inplace.st_mtime_ns == fixed_ns

    tags = meta_mod.read_full_tags(p)
    assert tags["artist"] == "Updated Artist"
    assert tags["title"] == "Updated Title"
    assert tags["year"] == "2024"
    assert tags["track"] == "5"
    assert tags["genre"] == "Synthwave"
    assert tags["cover"] == "present"
    extracted = cover_mod.extract_cover(p)
    assert extracted is not None and extracted[0] == jpeg_b

    # 3. Cover exceeding reserved padding cleanly falls back to atomic temp-file resize
    assert meta_mod.write_full_tags(p, {"cover": "https://img/huge.jpg"}, preserve_mtime=True) is True
    assert len(copy_calls) == 1
    assert os.stat(p).st_mtime_ns == fixed_ns
    extracted_huge = cover_mod.extract_cover(p)
    assert extracted_huge is not None and extracted_huge[0] == jpeg_huge


def test_fast_binary_header_scanner_and_stat_tag_cache(tmp_path, monkeypatch):
    """Option B: seek-over-cover binary scanner handles UTF-8/UTF-16/Latin-1, numeric TCON fallback,
    ID3v1 tail fallback, FLAC picture blocks, and invalidates stat-fingerprinted cache on in-place writes."""
    from mutagen.id3 import ID3, TCON, TIT2, TPE1
    from app import cover as cover_mod

    # 1. MP3 with UTF-16LE + Latin-1 + numeric TCON "(17)" -> falls back cleanly for numeric genre ("Rock")
    mp3_num = tone(str(tmp_path / "genre_num.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    id3 = ID3()
    id3.add(TPE1(encoding=1, text=["UTF16 Artist"]))
    id3.add(TIT2(encoding=0, text=["Latin Title"]))
    id3.add(TCON(encoding=3, text=["(17)"]))
    id3.save(mp3_num, v2_version=3)
    cover_mod.invalidate_tag_cache(mp3_num)
    parsed_num = meta_mod.read_full_tags(mp3_num)
    assert parsed_num["artist"] == "UTF16 Artist"
    assert parsed_num["title"] == "Latin Title"
    assert parsed_num["genre"] == "Rock"

    # 2. FLAC + MP3 fast scanner + cache hit tracking + in-place cache invalidation
    flac_p = tone(str(tmp_path / "cached.flac"), 0, codec_args=("-c:a", "flac"))
    jpeg = b"\xff\xd8\xff\xe0" + b"\x55" * 1024 + b"\xff\xd9"
    cover_mod.embed_cover(flac_p, jpeg, mime="image/jpeg")
    meta_mod.write_full_tags(flac_p, {"artist": "Cache Artist 1", "title": "Song 1", "genre": "Ambient"})

    real_uncached = cover_mod._read_raw_tags_uncached
    uncached_calls = []

    def counted_uncached(path, ext, st_size):
        uncached_calls.append(path)
        return real_uncached(path, ext, st_size)

    monkeypatch.setattr(cover_mod, "_read_raw_tags_uncached", counted_uncached)
    cover_mod.invalidate_tag_cache(flac_p)

    # First read populates cache; second read + inspect_audio hit cache with 0 disk parses
    t1 = meta_mod.read_full_tags(flac_p)
    t2 = meta_mod.read_full_tags(flac_p)
    info = cover_mod.inspect_audio(flac_p)
    assert len(uncached_calls) == 1
    assert t1["artist"] == t2["artist"] == info["artist"] == "Cache Artist 1"
    assert info["has_cover"] is True

    # In-place write (preserving mtime and file size!) must invalidate cache immediately
    meta_mod.write_full_tags(flac_p, {"artist": "Cache Artist 2"}, preserve_mtime=True)
    t3 = meta_mod.read_full_tags(flac_p)
    assert t3["artist"] == "Cache Artist 2"
    assert len(uncached_calls) == 2


def test_reentrant_db_tx_concurrent_online_sources_and_album_cover_dedup(client, cfg, tmp_path, monkeypatch):
    """Option C: SQLite synchronous=NORMAL + re-entrant SAVEPOINT tx(), concurrent multi-source
    HTTP lookups with deterministic merge order, and single-download cover deduplication across an album."""
    import time as time_mod
    from app import cover as cover_mod
    from app.db import Database

    # 1. SQLite synchronous=NORMAL + nested SAVEPOINT transactions (including db.log inside db.tx)
    test_db = Database(str(tmp_path / "tx_test.db"))
    try:
        sync_val = test_db.one("PRAGMA synchronous")["synchronous"]
        assert sync_val == 1  # NORMAL
        with test_db.tx():
            test_db.set_json("outer", "kept")
            test_db.log("info", "nested log inside outer transaction", "audio")
            try:
                with test_db.tx():
                    test_db.set_json("inner", "rolled_back")
                    raise RuntimeError("abort inner savepoint")
            except RuntimeError:
                pass
        assert test_db.get_json("outer") == "kept"
        assert test_db.get_json("inner") is None
    finally:
        test_db.close()

    # 2. Concurrent multi-source lookup_online_metadata (~3x faster than sequential, deterministic priority)
    def slow_discogs(artist, album, title, track, token="", cancel=None):
        time_mod.sleep(0.15)
        return [{"source": "Discogs", "artist": "Discogs Artist", "album": album, "title": title, "year": "1999", "track": track, "genre": "House", "cover": ""}]

    def slow_mb(artist, album, title, track, cancel=None):
        time_mod.sleep(0.15)
        return [{"source": "MusicBrainz", "artist": "MB Artist", "album": album, "title": title, "year": "2000", "track": track, "genre": "Electronic", "cover": "https://img/mb.jpg"}]

    def slow_itunes(artist, album, title, track, cancel=None):
        time_mod.sleep(0.15)
        return [{"source": "Open (iTunes)", "artist": "Open Artist", "album": album, "title": title, "year": "2001", "track": track, "genre": "Pop", "cover": "https://img/open.jpg"}]

    monkeypatch.setattr(meta_mod, "_lookup_discogs", slow_discogs)
    monkeypatch.setattr(meta_mod, "_lookup_musicbrainz", slow_mb)
    monkeypatch.setattr(meta_mod, "_lookup_itunes", slow_itunes)

    t0 = time_mod.perf_counter()
    merged = meta_mod.lookup_online_metadata("Daft Punk", "Discovery", "One More Time", "1")
    elapsed = time_mod.perf_counter() - t0
    assert elapsed < 0.35, f"expected concurrent lookup (<0.35s), took {elapsed:.3f}s"
    # Deterministic merge order: Discogs wins artist/year/genre, MusicBrainz fills missing cover
    assert merged["artist"] == "Discogs Artist"
    assert merged["year"] == "1999"
    assert merged["cover"] == "https://img/mb.jpg"
    assert set(merged["by_source"].keys()) == {"discogs", "musicbrainz", "open"}

    # 3. Album metadata apply deduplicates cover download across all tracks in the album
    album_dir = os.path.join(cfg.audio_root, "DedupBand", "DedupAlbum")
    for idx in (1, 2, 3):
        tone(os.path.join(album_dir, f"0{idx} - Song {idx}.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))

    dl_calls = []
    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x77" * 512 + b"\xff\xd9"

    def counted_download(url, token=""):
        dl_calls.append(url)
        return jpeg_bytes, "image/jpeg"

    monkeypatch.setattr(cover_mod, "download_cover", counted_download)
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    rows = client.get("/api/files", params={"media": "audio", "dir": "DedupBand/DedupAlbum"}).json()["files"]
    assert len(rows) == 3

    res = client.post(
        "/api/metadata/apply",
        json={
            "ids": [rows[0]["id"]],
            "scope": "album",
            "overrides": {
                "artist": {"choice": "custom", "value": "DedupBand"},
                "album": {"choice": "custom", "value": "DedupAlbum"},
                "cover": {"choice": "discogs", "value": "https://img/shared-album.jpg"},
            },
        },
    ).json()
    assert res["updated"] == 3
    assert dl_calls == ["https://img/shared-album.jpg"]


def test_metadata_table_multiline_filename_and_combined_current_selected_column():
    """Verify:
    1) 'File & folder' cell renders full multi-line file names (.meta-file-name with white-space: normal
       and overflow-wrap: anywhere) instead of single-line ellipsis truncation.
    2) 'Selected / Custom' and 'Current tag (file)' are combined into a single unified column
       ('Current tag / Selected (✎)') in both the Meta data tab and the per-file metadata popup.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "app.js"), "r", encoding="utf-8") as jf:
        js = jf.read()
    with open(os.path.join(root, "static", "styles.css"), "r", encoding="utf-8") as cf:
        css = cf.read()

    # 1. Complete file name is visible across multiple lines in 'File & folder'
    assert '<strong class="meta-file-name" title="\' + esc(f.relpath) + \'">\' + esc(f.name) + \'</strong>' in js
    fn_rule = re.search(r"\.meta-file-name\s*\{([^}]*)\}", css, re.S)
    assert fn_rule is not None
    assert "white-space: normal" in fn_rule.group(1)
    assert "overflow-wrap: anywhere" in fn_rule.group(1)
    col_rule = re.search(r"td\.meta-file-col\s*\{([^}]*)\}", css, re.S)
    assert col_rule is not None
    assert "white-space: normal" in col_rule.group(1)

    # 2. Combined 'Current tag / Selected (✎)' column replaces separate 'current' column
    spec_block = re.search(r"const META_COL_SPECS = \[(.*?)\];", js, re.S)
    assert spec_block is not None
    col_keys = re.findall(r"\['([a-z]+)'", spec_block.group(1))
    assert col_keys == ["pick", "file", "custom", "field", "path", "discogs", "musicbrainz", "open"]
    assert "current" not in col_keys
    assert "Current tag / Selected (✎)" in js
    assert "Current tag (file)" not in js
    assert "return customCell + fieldCell + pathCell + discogsCell + mbCell + openCell;" in js
    # Revert-to-current-tag button is available inside the combined column when another source/custom is selected
    assert 'data-choice="current"' in js and ">↺</button>" in js
    # No source badge/label is rendered inside the 'Current tag / Selected (✎)' column
    assert "meta-final-src" not in js
    assert "finalInfo.srcLabel" not in js

    # 3. Detailed list header stays visible while scrolling down; content above header scrolls away
    assert "'page-head-static'" in js
    assert re.search(r"\.page-head\.page-head-static\s*\{\s*position:\s*static;", css)
    assert "function syncMetaStickyHeader()" in js
    assert "#meta-diff-table thead.is-stuck" in css
    assert "translate3d(0, var(--meta-head-y, 0px), 0)" in css

    # 4. Small directory/folder push button behind values on tab Meta data pushes value to filtered list
    assert "const FOLDER_PUSH_ICON = '<svg class=\"meta-push-dir-ico\"" in js
    assert "function renderPushFilteredBtn(fieldKey, fieldLabel, pushVal, isModal)" in js
    assert "if (isModal) return '';" in js
    assert "data-action=\"meta-push-filtered\"" in js
    assert "} else if (a === 'meta-push-filtered') {" in js
    assert "const visibleFiles = filterAndSortMetaFiles(m.files || [], m.options || {});" in js
    assert "await queueMetaSelect({ ids: ids, field: field, choice: 'custom', custom_value: val });" in js
    assert ".meta-push-dir-btn" in css
    assert ".meta-val-with-push" in css


def test_push_value_and_embedded_cover_to_filtered_files_bulk_update(client, cfg):
    """Verify pushing a parameter value (e.g. year) or an embedded/local cover across a filtered
    subset of files updates all filtered files in bulk while leaving unfiltered files untouched."""
    from app import cover as cover_mod

    d1 = os.path.join(cfg.audio_root, "Band - First Album")
    d2 = os.path.join(cfg.audio_root, "Band - Second Album")
    d3 = os.path.join(cfg.audio_root, "Unfiltered - Other Album")

    f1 = tone(os.path.join(d1, "01 - One.mp3"), 0)
    f2 = tone(os.path.join(d2, "01 - Two.mp3"), 0)
    f3 = tone(os.path.join(d3, "01 - Three.mp3"), 0)

    meta_mod.write_full_tags(f1, {"artist": "Band", "album": "First Album", "title": "One", "year": "1999"})
    meta_mod.write_full_tags(f2, {"artist": "Band", "album": "Second Album", "title": "Two", "year": "2001"})
    meta_mod.write_full_tags(f3, {"artist": "Unfiltered", "album": "Other Album", "title": "Three", "year": "1985"})

    # Embed a JPEG cover in f1 and write a local folder cover in d1
    embedded_jpg = b"\xff\xd8\xff\xe0" + b"\xab" * 512 + b"\xff\xd9"
    cover_mod.embed_cover(f1, embedded_jpg, mime="image/jpeg")
    local_jpg_path = os.path.join(d1, "cover.jpg")
    local_jpg = b"\xff\xd8\xff\xe0" + b"\xcd" * 512 + b"\xff\xd9"
    with open(local_jpg_path, "wb") as lf:
        lf.write(local_jpg)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200

    rows = {
        r["relpath"]: r["id"]
        for r in client.app_state.db.query("SELECT id, relpath FROM files WHERE media='audio'")
    }
    id1 = rows["Band - First Album/01 - One.mp3"]
    id2 = rows["Band - Second Album/01 - Two.mp3"]
    id3 = rows["Unfiltered - Other Album/01 - Three.mp3"]

    assert client.post("/api/metadata/check", json={"ids": [id1, id2, id3], "use_online": False}).status_code == 200

    # Simulate clicking the folder push button behind year="1999" with filter matching [id1, id2]
    sel_res = client.post(
        "/api/metadata/select",
        json={"ids": [id1, id2], "field": "year", "choice": "custom", "custom_value": "1999"},
    )
    assert sel_res.status_code == 200
    assert sel_res.json()["updated"] == 2

    # Also test cross-folder local:// cover preview fallback on id2 before switching to filecover://
    client.post(
        "/api/metadata/select",
        json={"ids": [id1, id2], "field": "cover", "choice": "custom", "custom_value": f"local://{local_jpg_path}"},
    ).raise_for_status()
    preview_res = client.get(f"/api/files/{id2}/local-cover")
    assert preview_res.status_code == 200
    assert preview_res.content == local_jpg

    # Push f1's embedded cover (filecover://<id1>) to filtered files [id1, id2]
    cov_res = client.post(
        "/api/metadata/select",
        json={"ids": [id1, id2], "field": "cover", "choice": "custom", "custom_value": f"filecover://{id1}"},
    )
    assert cov_res.status_code == 200
    assert cov_res.json()["updated"] == 2

    # Apply metadata bulk update to selected filtered files [id1, id2]
    apply_res = client.post("/api/metadata/apply", json={"ids": [id1, id2]}).json()
    assert apply_res["updated"] >= 1
    assert apply_res["errors"] == []

    # Verify year="1999" and embedded cover were written to f2, while unfiltered f3 stayed "1985"
    assert meta_mod.read_full_tags(f1)["year"] == "1999"
    assert meta_mod.read_full_tags(f2)["year"] == "1999"
    assert meta_mod.read_full_tags(f3)["year"] == "1985"

    cov2, mime2 = cover_mod.extract_cover(f2)
    assert cov2 == embedded_jpg
    assert mime2 == "image/jpeg"
    assert cover_mod.extract_cover(f3) is None


def test_metadata_file_col_cover_button_and_all_sources_popup(client, cfg, monkeypatch):
    """Verify:
    1) Tab Meta data 'File & folder' column has a 'Cover' button near 'Update' opening the Cover popup.
    2) Cover popup search ('all' sources) returns cover arts from Local + Discogs + MusicBrainz + Open (iTunes),
       lets the user change search values or filter source, and applying updates file_metadata immediately."""
    import re
    from app import cover as cover_mod

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "app.js"), "r", encoding="utf-8") as jf:
        js = jf.read()

    m_actions = re.search(
        r"meta-row-actions.*?(meta-apply-one.*?open-cover-by-id.*?>Cover</button>.*?open-meta-by-id)",
        js,
        re.S,
    )
    assert m_actions is not None
    assert "COVER.source = 'all';" in js
    assert "['all', 'All sources (Local + Discogs + MusicBrainz + Open)']" in js

    album_dir = os.path.join(cfg.audio_root, "ArtistA - AlbumA")
    f1 = tone(os.path.join(album_dir, "01 - TrackOne.mp3"), 0)
    meta_mod.write_full_tags(f1, {"artist": "ArtistA", "album": "AlbumA", "title": "TrackOne"})
    local_jpg = b"\xff\xd8\xff\xe0" + b"\x99" * 512 + b"\xff\xd9"
    with open(os.path.join(album_dir, "cover.jpg"), "wb") as lf:
        lf.write(local_jpg)

    monkeypatch.setattr(
        cover_mod,
        "_search_discogs",
        lambda a, al, t, token="": [
            {"id": "d1", "source": "Discogs", "title": f"{a} - {al}", "artist": a, "album": al, "year": "2001", "image_url": "https://img/discogs.jpg", "thumb_url": "https://img/discogs.jpg"}
        ],
    )
    monkeypatch.setattr(
        cover_mod,
        "_search_musicbrainz_caa",
        lambda a, al, t: [
            {"id": "m1", "source": "MusicBrainz", "title": f"{a} - {al}", "artist": a, "album": al, "year": "2001", "image_url": "https://img/mb.jpg", "thumb_url": "https://img/mb.jpg"}
        ],
    )
    monkeypatch.setattr(
        cover_mod,
        "_search_itunes",
        lambda a, al, t: [
            {"id": "i1", "source": "Open (iTunes)", "title": f"{a} - {al}", "artist": a, "album": al, "year": "2001", "image_url": "https://img/itunes.jpg", "thumb_url": "https://img/itunes.jpg"}
        ],
    )

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    fid = client.app_state.db.one("SELECT id FROM files WHERE media='audio'")["id"]
    assert client.post("/api/metadata/check", json={"ids": [fid], "use_online": False}).status_code == 200

    # 1. Default 'all' popup search returns Local + Discogs + MusicBrainz + Open (iTunes)
    s_all = client.post(f"/api/files/{fid}/cover/search", json={"source": "all"}).json()
    sources = [c["source"] for c in s_all["candidates"]]
    assert "Local folder" in sources
    assert "Discogs" in sources
    assert "MusicBrainz" in sources
    assert "Open (iTunes)" in sources

    # 2. User changes search values (artist/album) + source filter ('itunes')
    s_custom = client.post(
        f"/api/files/{fid}/cover/search",
        json={"artist": "CustomArtist", "album": "CustomAlbum", "title": "CustomTitle", "source": "itunes", "force": True},
    ).json()
    assert s_custom["artist"] == "CustomArtist"
    assert s_custom["album"] == "CustomAlbum"
    assert len(s_custom["candidates"]) == 1
    assert s_custom["candidates"][0]["source"] == "Open (iTunes)"
    assert "CustomArtist - CustomAlbum" in s_custom["candidates"][0]["title"]

    # 3. Selecting and applying cover from popup updates file + file_metadata and returns updated_ids
    chosen_local_url = next(c["image_url"] for c in s_all["candidates"] if c["source"] == "Local folder")
    app_res = client.post(
        f"/api/files/{fid}/cover/apply",
        json={"image_url": chosen_local_url, "scope": "file"},
    ).json()
    assert app_res["ok"] is True
    assert app_res["updated"] == 1
    assert app_res["updated_ids"] == [fid]

    meta_after = client.get("/api/metadata").json()
    f_meta = next(x for x in meta_after["files"] if x["id"] == fid)
    assert f_meta["has_cover"] == 1
    assert f_meta["fields"]["cover"]["current"] == "present"









