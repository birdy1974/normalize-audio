"""Tests for audio cover art detection, tag-vs-path comparison, Discogs + open source search,
separate cover art parked pane, and per-file / per-album / bulk cover embedding."""
from __future__ import annotations

import base64
import os

import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3, TALB, TIT2, TPE1

from app import cover, state
from app.main import create_app
from conftest import tone
from test_pipeline import open_windows, run_until_idle

# Valid minimal JPEG and PNG byte payloads for cover embedding tests
FAKE_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00" + b"\x08" * 64 + b"\xff\xd9"
FAKE_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x00IEND\xaeB`\x82"


@pytest.fixture
def client(cfg):
    app = create_app(cfg, start_threads=False)
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


def _write_id3(path: str, *, artist: str = "", album: str = "", title: str = "") -> None:
    id3 = ID3()
    if artist:
        id3.add(TPE1(encoding=3, text=[artist]))
    if album:
        id3.add(TALB(encoding=3, text=[album]))
    if title:
        id3.add(TIT2(encoding=3, text=[title]))
    id3.save(path, v2_version=3)


# ------------------------------------------------------------ 1. Metadata & Tag-vs-Path comparison

def test_resolve_metadata_uses_path_when_tags_missing():
    rel = "Daft Punk/Discovery/01 - One More Time.mp3"
    tags = {"artist": "", "album": "", "title": "Track 01"}
    res = cover.resolve_search_metadata(rel, tags)
    assert res["source"] == "path"
    assert res["artist"] == "Daft Punk"
    assert res["album"] == "Discovery"
    assert res["title"] == "One More Time"


def test_resolve_metadata_uses_tags_when_matching_path():
    rel = "Daft Punk/Discovery/01 - One More Time.mp3"
    tags = {"artist": "Daft Punk", "album": "Discovery", "title": "One More Time (Original Mix)"}
    res = cover.resolve_search_metadata(rel, tags)
    assert res["source"] == "tags"
    assert res["artist"] == "Daft Punk"
    assert res["album"] == "Discovery"
    assert res["title"] == "One More Time (Original Mix)"


def test_resolve_metadata_takes_folder_and_file_name_when_id3_totally_different():
    """Requirement: do an additional check between the folder/file name and the ID3 tag;
    in case totally different, take the folder/file name."""
    rel = "Pink Floyd/The Dark Side of the Moon/02 - Breathe.mp3"
    tags = {
        "artist": "Britney Spears",
        "album": "Oops!... I Did It Again",
        "title": "Toxic",
    }
    res = cover.resolve_search_metadata(rel, tags)
    assert res["source"] == "path_override"
    assert res["artist"] == "Pink Floyd"
    assert res["album"] == "The Dark Side of the Moon"
    assert res["title"] == "Breathe"


def test_resolve_metadata_overrides_mismatched_album_tag_from_folder():
    rel = "Queen/A Night at the Opera/01 - Bohemian Rhapsody.mp3"
    tags = {
        "artist": "Queen",
        "album": "Greatest Hits Vol 99 Promo Rip",
        "title": "Bohemian Rhapsody",
    }
    res = cover.resolve_search_metadata(rel, tags)
    assert res["source"] == "path_override"
    assert res["artist"] == "Queen"
    assert res["album"] == "A Night at the Opera"
    assert res["title"] == "Bohemian Rhapsody"


# ------------------------------------------------------------ 2. All audio formats with metadata

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
def test_embed_and_extract_cover_across_audio_formats(cfg, ext, codec_args):
    path = tone(os.path.join(cfg.audio_root, "Formats", f"sample.{ext}"), -3, codec_args=codec_args)
    os.utime(path, (1_700_000_000, 1_700_000_000))

    info_before = cover.inspect_audio(path)
    assert info_before["has_cover"] is False
    assert cover.extract_cover(path) is None

    cover.embed_cover(path, FAKE_JPEG, mime="image/jpeg", preserve_mtime=True)
    assert int(os.stat(path).st_mtime) == 1_700_000_000

    info_after = cover.inspect_audio(path)
    assert info_after["has_cover"] is True
    extracted = cover.extract_cover(path)
    assert extracted is not None
    data, mime = extracted
    assert data == FAKE_JPEG
    assert mime == "image/jpeg"


# ------------------------------------------------------------ 3. Discogs + Open Sources & API workflows

def test_single_cover_auto_embeds_and_serves_cover_endpoint(client, cfg, monkeypatch):
    p = tone(
        os.path.join(cfg.audio_root, "Daft Punk", "Discovery", "01 - One More Time.mp3"),
        0,
        codec_args=("-c:a", "libmp3lame", "-b:a", "128k"),
    )
    # Write totally different wrong ID3 tags to verify folder/file name override in end-to-end check
    _write_id3(p, artist="Wrong Artist", album="Unrelated Album", title="Random Song")

    queried = {}

    def fake_search(artist, album, title, settings, db=None, force_refresh=False):
        queried["artist"] = artist
        queried["album"] = album
        queried["title"] = title
        data_url = "data:image/jpeg;base64," + base64.b64encode(FAKE_JPEG).decode("ascii")
        return [
            {
                "id": "discogs:101",
                "source": "Discogs",
                "title": f"{artist} - {album}",
                "artist": artist,
                "album": album,
                "year": "2001",
                "image_url": data_url,
                "thumb_url": data_url,
            }
        ]

    monkeypatch.setattr(cover, "search_covers", fake_search)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    files = client.get("/api/files", params={"media": "audio", "dir": "Daft Punk/Discovery"}).json()["files"]
    assert len(files) == 1
    assert files[0]["has_cover"] == 0
    assert files[0]["cover_query_source"] == "path_override"
    assert files[0]["tag_artist"] == "Daft Punk"
    assert files[0]["tag_album"] == "Discovery"

    # Trigger folder cover check
    r = client.post("/api/covers/check", json={"dir": "Daft Punk/Discovery"})
    assert r.status_code == 200
    res = r.json()
    assert res["checked"] == 1 and res["embedded"] == 1
    assert queried == {"artist": "Daft Punk", "album": "Discovery", "title": "One More Time"}

    # Verify the file now has embedded cover art and serves it over HTTP
    files_after = client.get("/api/files", params={"media": "audio", "dir": "Daft Punk/Discovery"}).json()["files"]
    assert files_after[0]["has_cover"] == 1
    assert files_after[0]["cover_status"] == "embedded"

    img_res = client.get(f"/api/files/{files_after[0]['id']}/cover")
    assert img_res.status_code == 200
    assert img_res.content == FAKE_JPEG
    assert img_res.headers["content-type"] == "image/jpeg"


def test_multiple_covers_park_in_separate_cover_pane_and_apply_per_album_or_file(client, cfg, monkeypatch):
    """When multiple pictures are found:
    - Park in the separate cover pane (/api/covers/parked), NOT in /api/parked (audio parked files).
    - Allow applying per file or per album."""
    for idx, track in enumerate(("01 - Airbag.mp3", "02 - Paranoid Android.mp3", "03 - Subterranean Homesick Alien.mp3"), start=1):
        p = tone(
            os.path.join(cfg.audio_root, "Radiohead", "OK Computer", track),
            0,
            codec_args=("-c:a", "libmp3lame", "-b:a", "128k"),
        )
        _write_id3(p, artist="Radiohead", album="OK Computer", title=track.split(" - ", 1)[1].rsplit(".", 1)[0])

    url1 = "data:image/jpeg;base64," + base64.b64encode(FAKE_JPEG).decode("ascii")
    url2 = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode("ascii")

    def fake_search(artist, album, title, settings, db=None, force_refresh=False):
        return [
            {
                "id": "discogs:1",
                "source": "Discogs",
                "title": "Radiohead - OK Computer (1997 UK)",
                "artist": "Radiohead",
                "album": "OK Computer",
                "year": "1997",
                "image_url": url1,
                "thumb_url": url1,
            },
            {
                "id": "caa:2",
                "source": "Cover Art Archive",
                "title": "Radiohead - OK Computer OKNOTOK (2017)",
                "artist": "Radiohead",
                "album": "OK Computer",
                "year": "2017",
                "image_url": url2,
                "thumb_url": url2,
            },
        ]

    monkeypatch.setattr(cover, "search_covers", fake_search)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    r = client.post("/api/covers/check", json={"enabled_only": True})
    assert r.status_code == 200
    assert r.json()["choices"] == 3

    # Audio normalization parked list (/api/parked) MUST remain empty (not mixed!)
    audio_parked = client.get("/api/parked", params={"media": "audio"}).json()
    assert audio_parked["files"] == []

    # Separate Cover Art parked endpoint (/api/covers/parked) has 1 album group with 3 files and 2 candidate pictures
    cover_parked = client.get("/api/covers/parked").json()
    assert cover_parked["counts"]["albums"] == 1
    assert cover_parked["counts"]["choices"] == 3
    album_grp = cover_parked["albums"][0]
    assert album_grp["artist"] == "Radiohead"
    assert album_grp["album"] == "OK Computer"
    assert len(album_grp["file_ids"]) == 3
    assert len(album_grp["candidates"]) == 2

    # 1) Apply second cover to a single file (per-file basis)
    first_id = album_grp["file_ids"][0]
    apply_one = client.post(f"/api/files/{first_id}/cover/apply", json={"image_url": url2, "scope": "file"})
    assert apply_one.status_code == 200
    assert apply_one.json()["updated"] == 1

    # Now 2 files remain in /api/covers/parked
    cover_parked_2 = client.get("/api/covers/parked").json()
    assert cover_parked_2["counts"]["choices"] == 2

    # 2) Apply first cover to the whole album (per-album basis)
    second_id = album_grp["file_ids"][1]
    apply_album = client.post(
        f"/api/files/{second_id}/cover/apply",
        json={"image_url": url1, "scope": "album", "target_ids": cover_parked_2["albums"][0]["file_ids"]},
    )
    assert apply_album.status_code == 200
    assert apply_album.json()["updated"] == 2

    # Cover parked pane is now empty and all 3 files have embedded covers
    assert client.get("/api/covers/parked").json()["counts"]["total"] == 0
    dir_files = client.get("/api/files", params={"media": "audio", "dir": "Radiohead/OK Computer"}).json()["files"]
    assert all(f["has_cover"] == 1 and f["cover_status"] == "embedded" for f in dir_files)


def test_cover_auto_embeds_during_normalization_run_when_enabled(cfg, app_engine, monkeypatch):
    _app, engine, db = app_engine
    p = tone(
        os.path.join(cfg.audio_root, "Massive Attack", "Mezzanine", "01 - Angel.mp3"),
        -4,
        codec_args=("-c:a", "libmp3lame", "-b:a", "128k"),
    )
    url = "data:image/jpeg;base64," + base64.b64encode(FAKE_JPEG).decode("ascii")
    monkeypatch.setattr(
        cover,
        "search_covers",
        lambda artist, album, title, settings, db=None, force_refresh=False: [
            {
                "id": "discogs:555",
                "source": "Discogs",
                "title": "Massive Attack - Mezzanine",
                "artist": artist,
                "album": album,
                "year": "1998",
                "image_url": url,
                "thumb_url": url,
            }
        ],
    )

    state.put_settings(db, {"general": {"coverAuto": True}, "audio": {"target": -16.0, "threshold": 12.0}})
    open_windows(db)
    engine.scan_now("audio")
    engine.start()
    run_until_idle(engine, db)

    r = db.one("SELECT * FROM files WHERE name = '01 - Angel.mp3'")
    assert r["has_cover"] == 1
    assert r["cover_status"] == "embedded"
    assert cover.inspect_audio(p)["has_cover"] is True


def test_legacy_database_migration_adds_cover_columns_and_index(tmp_path):
    """Opening an existing SQLite database created before cover_status existed must migrate cleanly."""
    import sqlite3
    from app.db import Database

    legacy_db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(legacy_db_path)
    conn.executescript(
        """
        CREATE TABLE files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media TEXT NOT NULL,
            relpath TEXT NOT NULL,
            dir TEXT NOT NULL,
            name TEXT NOT NULL,
            ext TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reason TEXT,
            codec TEXT,
            family TEXT,
            lufs REAL,
            true_peak REAL,
            measured_at REAL,
            gain_db REAL,
            adjust_db REAL NOT NULL DEFAULT 0,
            applied_db REAL,
            method TEXT,
            changed_at REAL,
            seen_at REAL NOT NULL,
            UNIQUE (media, relpath)
        );
        """
    )
    conn.close()

    db = Database(legacy_db_path)
    cols = {r["name"] for r in db.query("PRAGMA table_info(files)")}
    assert "cover_status" in cols
    assert "has_cover" in cols
    assert "cover_candidates" in cols
    assert "cover_updated_at" in cols
    assert "tag_artist" in cols
    assert "cover_query_source" in cols


def test_cover_apply_syncs_file_metadata_table_and_local_cover_endpoint(client, cfg):
    """Applying cover art from the Cover Picker must also sync file_metadata and serve /local-cover."""
    folder = os.path.join(cfg.audio_root, "LocalArtBand", "AlbumOne")
    p = tone(os.path.join(folder, "01 - Intro.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    cover_file = os.path.join(folder, "cover.jpg")
    with open(cover_file, "wb") as fh:
        fh.write(FAKE_JPEG + b"\x00" * 80)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    files = client.get("/api/files", params={"media": "audio", "dir": "LocalArtBand/AlbumOne"}).json()["files"]
    fid = files[0]["id"]

    # Local cover endpoint serves the folder's cover.jpg for browser previews
    loc_res = client.get(f"/api/files/{fid}/local-cover")
    assert loc_res.status_code == 200
    assert loc_res.content.startswith(b"\xff\xd8\xff")

    # Populate file_metadata first, then apply PNG cover via /cover/apply and ensure file_metadata is immediately synced
    assert client.post(f"/api/files/{fid}/metadata/check", json={"force": False}).status_code == 200
    png_url = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode("ascii")
    assert client.post(f"/api/files/{fid}/cover/apply", json={"image_url": png_url, "scope": "file"}).status_code == 200

    meta_item = client.get("/api/metadata").json()["files"][0]
    assert meta_item["has_cover"] == 1
    assert meta_item["current"]["cover"] == "present"
    assert client.get(f"/api/files/{fid}/cover").content == FAKE_PNG


def test_cover_search_query_mode_file_vs_metadata(client, cfg, monkeypatch):
    """Cover Picker popup can explicitly take Artist / Title from file name or from embedded metadata tags."""
    from mutagen.id3 import ID3, TALB, TIT2, TPE1

    folder = os.path.join(cfg.audio_root, "FolderArtist", "FolderAlbum")
    p = tone(os.path.join(folder, "01 - FolderTitle.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["TagArtist"]))
    tags.add(TALB(encoding=3, text=["TagAlbum"]))
    tags.add(TIT2(encoding=3, text=["TagTitle"]))
    tags.save(p)

    seen_queries = []

    def fake_search_covers(artist, album, title, st, db=None, force_refresh=False):
        seen_queries.append((artist, album, title))
        return [{"source": "Open", "title": f"{artist} - {title}", "year": "2024", "image_url": "https://img/1.jpg", "thumb_url": "https://img/1.jpg"}]

    monkeypatch.setattr(cover, "search_covers", fake_search_covers)

    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    fid = client.get("/api/files", params={"media": "audio", "dir": "FolderArtist/FolderAlbum"}).json()["files"][0]["id"]

    # Explicit "file" mode takes artist/album/title from folder/file name
    r_file = client.post(f"/api/files/{fid}/cover/search", json={"query_mode": "file"}).json()
    assert r_file["query_mode"] == "file"
    assert r_file["query_source"] == "file"
    assert (r_file["artist"], r_file["album"], r_file["title"]) == ("FolderArtist", "FolderAlbum", "FolderTitle")
    assert seen_queries[-1] == ("FolderArtist", "FolderAlbum", "FolderTitle")

    # Explicit "metadata" mode takes artist/album/title from embedded ID3 tags even when totally different from path
    r_meta = client.post(f"/api/files/{fid}/cover/search", json={"query_mode": "metadata"}).json()
    assert r_meta["query_mode"] == "metadata"
    assert r_meta["query_source"] == "metadata"
    assert (r_meta["artist"], r_meta["album"], r_meta["title"]) == ("TagArtist", "TagAlbum", "TagTitle")
    assert seen_queries[-1] == ("TagArtist", "TagAlbum", "TagTitle")
    assert (r_meta["path_artist"], r_meta["path_title"]) == ("FolderArtist", "FolderTitle")
    assert (r_meta["tag_artist"], r_meta["tag_title"]) == ("TagArtist", "TagTitle")


def test_cover_modal_buttons_hover_zoom_and_global_search_from(client, cfg, monkeypatch):
    """Verify:
    1) Cover modal 'Use for file' is green (btn-primary) and 'Use for album' is white (btn without btn-primary).
    2) Cover hover zoom preview tooltip markup & JS handlers exist and modals ignore outside backdrop clicks.
    3) Global 'Take Artist / Title from' setting (exposed in Directories files pane) drives cover search & batch cover check.
    """
    import re
    from mutagen.id3 import ID3, TALB, TIT2, TPE1

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "static", "index.html"), "r", encoding="utf-8") as hf:
        html_txt = hf.read()
    with open(os.path.join(root, "static", "app.js"), "r", encoding="utf-8") as jf:
        js_txt = jf.read()
    with open(os.path.join(root, "static", "styles.css"), "r", encoding="utf-8") as cf:
        css_txt = cf.read()

    # 1. Button colors in cover-modal
    m_file = re.search(r'<button[^>]*id="cover-apply-file"[^>]*>', html_txt)
    m_album = re.search(r'<button[^>]*id="cover-apply-album"[^>]*>', html_txt)
    assert m_file and "btn-primary" in m_file.group(0)
    assert m_album and "btn-primary" not in m_album.group(0)

    # 2. Cover hover zoom preview & no outside-click modal dismissal
    assert 'id="cover-zoom-tip"' in html_txt
    assert 'id="cover-zoom-img"' in html_txt
    assert ".cover-zoom-tip" in css_txt
    assert "findCoverZoomTarget" in js_txt
    assert "data-cover-zoom=" in js_txt
    assert "if (ev.target === modal) closeEditor()" not in js_txt
    assert "if (ev.target === coverModalEl) closeCoverModal()" not in js_txt
    assert "if (ev.target === metaModalEl) closeMetaModal()" not in js_txt

    # 3. Directories files-pane header setting + global search_from honored by cover check & cover search
    assert "renderFilesPaneSearchSourceBar" in js_txt
    folder = os.path.join(cfg.audio_root, "DirBand", "DirAlbum")
    p = tone(os.path.join(folder, "01 - DirTrack.mp3"), 0, codec_args=("-c:a", "libmp3lame", "-b:a", "128k"))
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["Id3Band"]))
    tags.add(TALB(encoding=3, text=["Id3Album"]))
    tags.add(TIT2(encoding=3, text=["Id3Track"]))
    tags.save(p)

    seen_queries = []

    def fake_search_covers(artist, album, title, st, db=None, force_refresh=False):
        seen_queries.append((artist, album, title))
        return [
            {"source": "Discogs", "title": "Opt 1", "year": "2020", "image_url": "https://img/a.jpg", "thumb_url": "https://img/a.jpg"},
            {"source": "Open", "title": "Opt 2", "year": "2020", "image_url": "https://img/b.jpg", "thumb_url": "https://img/b.jpg"},
        ]

    monkeypatch.setattr(cover, "search_covers", fake_search_covers)
    assert client.post("/api/scan", json={"media": "audio"}).status_code == 200
    fid = client.get("/api/files", params={"media": "audio", "dir": "DirBand/DirAlbum"}).json()["files"][0]["id"]

    # Set global searchFrom = "metadata" -> dashboard reports it, cover search & batch cover check honor it
    assert client.put("/api/metadata/options", json={"searchFrom": "metadata"}).status_code == 200
    assert client.get("/api/dashboard").json()["search_from"] == "metadata"

    s_meta = client.post(f"/api/files/{fid}/cover/search", json={}).json()
    assert s_meta["query_mode"] == "metadata"
    assert seen_queries[-1] == ("Id3Band", "Id3Album", "Id3Track")

    assert client.post("/api/covers/check", json={"ids": [fid]}).status_code == 200
    assert seen_queries[-1] == ("Id3Band", "Id3Album", "Id3Track")

    # Switch global searchFrom = "file" -> cover check & search use folder/file name
    assert client.put("/api/metadata/options", json={"searchFrom": "file"}).status_code == 200
    assert client.get("/api/dashboard").json()["search_from"] == "file"
    assert client.post("/api/covers/check", json={"ids": [fid]}).status_code == 200
    assert seen_queries[-1] == ("DirBand", "DirAlbum", "DirTrack")




