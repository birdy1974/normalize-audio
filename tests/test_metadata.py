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





