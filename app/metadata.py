"""Full audio metadata inspection, tag-vs-filename verification, online metadata lookup
(Discogs, MusicBrainz, Open/iTunes), and timestamp-preserving tag writing."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from typing import Any

import httpx
import mutagen
from mutagen.aiff import AIFF
from mutagen.asf import ASF, ASFUnicodeAttribute
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

from . import cover
from .state import METADATA_FIELDS

log = logging.getLogger("normalizer")

YEAR_IN_DIR_RE = re.compile(r"(?:^|\(|\b)((?:19|20)\d{2})(?:\)|\b)")
LEADING_TRACK_RE = re.compile(
    r"^(?:(?:cd|disc)\s*\d+[-_.\s]*)?(?:\[0*(\d{1,3})\]|\(0*(\d{1,3})\)|0*(\d{1,3}))\s*(?:[-_.)]+\s*|\s+(?=[A-Za-z]))",
    re.IGNORECASE,
)


def _norm_track(val: Any) -> str:
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    head = s.split("/", 1)[0].strip()
    if head.isdigit():
        return str(int(head))
    return head


def _norm_year(val: Any) -> str:
    if val is None:
        return ""
    m = re.search(r"((?:19|20)\d{2})", str(val))
    return m.group(1) if m else ""


def _id3_str(id3: ID3, *keys: str) -> str:
    for k in keys:
        frame = id3.get(k)
        if frame and getattr(frame, "text", None):
            val = str(frame.text[0]).strip()
            if val:
                return val
    return ""


def _vorbis_str(tags: Any, *keys: str) -> str:
    if not tags:
        return ""
    for k in keys:
        v = tags.get(k) or tags.get(k.upper()) or tags.get(k.lower())
        if v:
            s = str(v[0] if isinstance(v, (list, tuple)) else v).strip()
            if s:
                return s
    return ""


def read_full_tags(path: str) -> dict[str, Any]:
    """Read all 8 metadata fields from an audio file without modifying it."""
    raw = cover.read_raw_tags_cached(path)
    return {
        "artist": raw["artist"],
        "title": raw["title"],
        "album": raw["album"],
        "album_artist": raw["album_artist"],
        "year": _norm_year(raw["year"]),
        "track": _norm_track(raw["track"]),
        "genre": raw["genre"],
        "cover": "present" if raw["has_cover"] else "",
    }


class CoverUpdateError(ValueError):
    """Raised when text tags were saved on disk, but downloading/embedding cover art failed."""

    def __init__(self, message: str, *, tags_written: bool = False):
        super().__init__(message)
        self.tags_written = tags_written


def _apply_id3_updates(id3: ID3, updates: dict[str, Any]) -> None:
    mapping = {
        "artist": ("TPE1", TPE1),
        "title": ("TIT2", TIT2),
        "album": ("TALB", TALB),
        "album_artist": ("TPE2", TPE2),
        "year": ("TDRC", TDRC),
        "track": ("TRCK", TRCK),
        "genre": ("TCON", TCON),
    }
    for field, (frame_id, cls) in mapping.items():
        if field not in updates:
            continue
        val = str(updates[field] or "").strip()
        id3.delall(frame_id)
        if field == "year":
            id3.delall("TYER")
            id3.delall("TDAT")
        if val:
            id3.add(cls(encoding=3, text=[val]))


def _apply_text_updates_to_obj(ext: str, obj: Any, tag_updates: dict[str, Any]) -> None:
    if ext in ("mp3", "aac"):
        _apply_id3_updates(obj, tag_updates)
    elif ext in ("flac", "ogg", "oga", "opus"):
        if obj.tags is None:
            obj.add_tags()
        vmap = {
            "artist": "artist",
            "title": "title",
            "album": "album",
            "album_artist": "albumartist",
            "year": "date",
            "track": "tracknumber",
            "genre": "genre",
        }
        for field, vkey in vmap.items():
            if field in tag_updates:
                val = str(tag_updates[field] or "").strip()
                if val:
                    obj[vkey] = [val]
                elif vkey in obj:
                    del obj[vkey]
                if field == "year" and "year" in obj and vkey != "year":
                    del obj["year"]
    elif ext == "m4a":
        if obj.tags is None:
            obj.add_tags()
        mmap = {
            "artist": "\xa9ART",
            "title": "\xa9nam",
            "album": "\xa9alb",
            "album_artist": "aART",
            "year": "\xa9day",
            "genre": "\xa9gen",
        }
        for field, mkey in mmap.items():
            if field in tag_updates:
                val = str(tag_updates[field] or "").strip()
                if val:
                    obj.tags[mkey] = [val]
                elif mkey in obj.tags:
                    del obj.tags[mkey]
        if "track" in tag_updates:
            tval = _norm_track(tag_updates["track"])
            if tval and tval.isdigit():
                existing_total = 0
                if obj.tags.get("trkn") and isinstance(obj.tags["trkn"][0], tuple):
                    existing_total = obj.tags["trkn"][0][1]
                obj.tags["trkn"] = [(int(tval), existing_total)]
            elif "trkn" in obj.tags:
                del obj.tags["trkn"]
    elif ext in ("wav", "aif", "aiff"):
        if obj.tags is None:
            obj.add_tags()
        _apply_id3_updates(obj.tags, tag_updates)
    elif ext == "wma":
        if obj.tags is None:
            obj.add_tags()
        amap = {
            "artist": "Author",
            "title": "Title",
            "album": "WM/AlbumTitle",
            "album_artist": "WM/AlbumArtist",
            "year": "WM/Year",
            "track": "WM/TrackNumber",
            "genre": "WM/Genre",
        }
        for field, akey in amap.items():
            if field in tag_updates:
                val = str(tag_updates[field] or "").strip()
                if val:
                    obj.tags[akey] = [ASFUnicodeAttribute(val)]
                elif akey in obj.tags:
                    del obj.tags[akey]


def write_full_tags(
    path: str,
    updates: dict[str, Any],
    *,
    discogs_token: str = "",
    preserve_mtime: bool = True,
    prefetched_cover: tuple[bytes, str] | None = None,
) -> bool:
    """Write selected metadata fields (and optional cover URL) into an audio file in a single pass.
    Writes in-place when existing tag padding fits; falls back to an atomic sibling temp file with
    8 KiB reserved padding otherwise. Preserves original file timestamps when preserve_mtime=True."""
    if not os.path.isfile(path):
        raise ValueError("audio file not found")
    if not updates:
        return False

    tag_updates = {k: v for k, v in updates.items() if k != "cover"}
    cover_val = updates.get("cover")
    has_cover_url = bool(cover_val and str(cover_val).startswith(("http://", "https://", "data:", "local://")))
    if not tag_updates and not has_cover_url:
        return True

    cover_bytes: bytes | None = None
    cover_mime = "image/jpeg"
    cover_error: Exception | None = None
    if has_cover_url:
        try:
            if prefetched_cover is not None:
                data, mime = prefetched_cover
            else:
                data, mime = cover.download_cover(str(cover_val), token=discogs_token)
            if not data or len(data) < 16:
                raise ValueError("invalid cover image data")
            cover_bytes = data
            cover_mime = cover.detect_mime(data, mime or "image/jpeg")
        except Exception as exc:
            if not tag_updates:
                raise
            cover_error = exc

    stat_before = os.stat(path)
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    base = os.path.basename(path)

    can_try_inplace = False
    obj: Any = None
    if ext in ("mp3", "aac"):
        had_v2 = cover.has_id3v2_header(path)
        try:
            obj = ID3(path)
        except ID3NoHeaderError:
            obj = ID3()
        can_try_inplace = had_v2
    elif ext == "flac":
        obj = FLAC(path)
        can_try_inplace = True
    elif ext in ("ogg", "oga", "opus"):
        obj = cover.open_ogg_fast(path, ext)
        if obj is None:
            raise ValueError(f"unsupported audio format: {base}")
        can_try_inplace = False
    elif ext == "m4a":
        obj = MP4(path)
        can_try_inplace = True
    elif ext in ("wav", "aif", "aiff"):
        obj = WAVE(path) if ext == "wav" else AIFF(path)
        can_try_inplace = obj.tags is not None
    elif ext == "wma":
        obj = ASF(path)
        can_try_inplace = True
    elif cover_bytes is not None:
        raise ValueError(f"embedding cover art is not supported for .{ext} files")
    else:
        return True

    if tag_updates:
        _apply_text_updates_to_obj(ext, obj, tag_updates)
    if cover_bytes is not None:
        cover.apply_cover_to_mutagen_obj(ext, obj, cover_bytes, cover_mime)

    cover.save_mutagen_smart(
        path,
        ext,
        obj,
        can_try_inplace=can_try_inplace,
        v1=2 if tag_updates else 1,
        preserve_mtime=preserve_mtime,
        stat_before=stat_before,
        op_label="meta",
    )
    if cover_error is not None:
        raise CoverUpdateError(f"tags updated, but cover art failed ({cover_error})", tags_written=True) from cover_error
    return True


# ------------------------------------------------------------ path metadata parsing

def parse_path_full(relpath: str) -> dict[str, str]:
    """Parse artist, album, title, album_artist, year, and track number from folder/file path."""
    base = cover.parse_path_metadata(relpath)
    norm = (relpath or "").replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    filename = parts[-1] if parts else ""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    track = ""
    m_tr = LEADING_TRACK_RE.match(stem.strip())
    if m_tr:
        raw_num = m_tr.group(1) or m_tr.group(2) or m_tr.group(3)
        if raw_num and raw_num.isdigit():
            track = str(int(raw_num))
    elif " - " in stem:
        dash_parts = [p.strip() for p in stem.split(" - ")]
        if len(dash_parts) >= 3 and dash_parts[1].isdigit():
            track = str(int(dash_parts[1]))

    year = ""
    for folder_seg in reversed(parts[:-1]):
        m_yr = YEAR_IN_DIR_RE.search(folder_seg)
        if m_yr:
            year = m_yr.group(1)
            break

    return {
        "artist": base["artist"],
        "title": base["title"],
        "album": base["album"],
        "album_artist": base["artist"],
        "year": year,
        "track": track,
        "genre": "",
        "cover": "",
    }


# ------------------------------------------------------------ online metadata lookup

def _clean_discogs_artist(name: str) -> str:
    # Discogs appends (2), (3) to disambiguate artists with the same name
    return re.sub(r"\s*\(\d+\)$", "", (name or "").strip())


def _lookup_discogs(artist: str, album: str, title: str, track: str, token: str = "", cancel=None) -> list[dict]:
    headers = cover._discogs_headers(token)
    params_list = []
    if artist and album:
        params_list.append({"artist": artist, "release_title": album, "type": "master", "per_page": 5})
        params_list.append({"artist": artist, "release_title": album, "type": "release", "per_page": 5})
    if artist and title:
        params_list.append({"artist": artist, "track": title, "per_page": 5})
    q = " ".join(x for x in (artist, album, title) if x).strip()
    if q and not params_list:
        params_list.append({"q": q, "per_page": 5})

    matches: list[dict] = []
    seen_ids: set[Any] = set()
    for params in params_list:
        if cancel and cancel():
            return matches
        try:
            r = cover.http_get("https://api.discogs.com/database/search", params=params, headers=headers, timeout=6.0)
            if r.status_code != 200:
                continue
            results = r.json().get("results") or []
        except Exception:
            continue
        for item in results:
            mid = item.get("master_id") or item.get("id")
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            raw_title = (item.get("title") or "").strip()
            c_artist, c_album = artist, album
            if " - " in raw_title:
                c_artist, c_album = [p.strip() for p in raw_title.split(" - ", 1)]
            c_artist = _clean_discogs_artist(c_artist)
            genres = item.get("genre") or item.get("style") or []
            genre = genres[0] if isinstance(genres, list) and genres else ""
            year = _norm_year(item.get("year"))
            cover_url = (item.get("cover_image") or item.get("thumb") or "").strip()
            if cover_url.endswith("spacer.gif"):
                cover_url = ""

            matches.append({
                "source": "Discogs",
                "artist": c_artist or artist,
                "title": title,
                "album": c_album or album,
                "album_artist": c_artist or artist,
                "year": year,
                "track": track,
                "genre": genre,
                "cover": cover_url,
            })
            if len(matches) >= 3:
                break
        if matches:
            break
    return matches


def _caa_front_url(rg_id: str | None, rel_id: str | None, headers: dict[str, str]) -> str:
    """Resolve a verified front cover URL from Cover Art Archive, or empty string if none exists."""
    endpoints = []
    if rg_id:
        endpoints.append(f"https://coverartarchive.org/release-group/{rg_id}")
    if rel_id:
        endpoints.append(f"https://coverartarchive.org/release/{rel_id}")
    for url in endpoints:
        try:
            r = cover.http_get(url, headers=headers, follow_redirects=True, timeout=3.5)
            if r.status_code != 200:
                continue
            images = r.json().get("images") or []
            front = next((img for img in images if img.get("front")), images[0] if images else None)
            if not front or not front.get("image"):
                continue
            thumbs = front.get("thumbnails") or {}
            return str(thumbs.get("500") or thumbs.get("large") or front["image"]).strip()
        except Exception:
            continue
    return ""


def _lookup_musicbrainz(artist: str, album: str, title: str, track: str, cancel=None) -> list[dict]:
    if cancel and cancel():
        return []
    headers = {"User-Agent": cover.USER_AGENT, "Accept": "application/json"}
    q_parts = []
    if artist:
        q_parts.append(f'artist:"{artist}"')
    if title:
        q_parts.append(f'recording:"{title}"')
    if album:
        q_parts.append(f'release:"{album}"')
    if not q_parts:
        return []
    try:
        r = cover.http_get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": " AND ".join(q_parts), "fmt": "json", "limit": 4},
            headers=headers,
            timeout=6.0,
        )
        if r.status_code != 200:
            return []
        recs = r.json().get("recordings") or []
    except Exception:
        return []

    matches: list[dict] = []
    caa_checked: dict[str, str] = {}
    for idx, rec in enumerate(recs[:3]):
        if cancel and cancel():
            break
        rec_title = (rec.get("title") or title).strip()
        ac = rec.get("artist-credit") or []
        rec_artist = (ac[0].get("name") if ac and isinstance(ac[0], dict) else artist) or artist
        releases = rec.get("releases") or []
        rel = releases[0] if releases else {}
        rel_album = (rel.get("title") or album).strip()
        year = _norm_year(rel.get("date") or rec.get("first-release-date"))
        media_list = rel.get("media") or []
        rel_track = track
        if media_list and isinstance(media_list[0], dict):
            tr_list = media_list[0].get("tracks") or []
            if tr_list and isinstance(tr_list[0], dict) and tr_list[0].get("number"):
                rel_track = _norm_track(tr_list[0]["number"])
        tags = rec.get("tags") or []
        genre = ""
        if tags and isinstance(tags[0], dict):
            genre = (tags[0].get("name") or "").title()
        rg = rel.get("release-group") or {}
        rg_id = rg.get("id")
        rel_id = rel.get("id")
        cover_url = ""
        if idx == 0 and (rg_id or rel_id):
            ckey = f"{rg_id}:{rel_id}"
            if ckey not in caa_checked:
                caa_checked[ckey] = _caa_front_url(rg_id, rel_id, headers)
            cover_url = caa_checked[ckey]

        matches.append({
            "source": "MusicBrainz",
            "artist": rec_artist,
            "title": rec_title,
            "album": rel_album,
            "album_artist": rec_artist,
            "year": year,
            "track": rel_track,
            "genre": genre,
            "cover": cover_url,
        })
    return matches


def _lookup_itunes(artist: str, album: str, title: str, track: str, cancel=None) -> list[dict]:
    if cancel and cancel():
        return []
    term = " ".join(x for x in (artist, album, title) if x).strip()
    if not term:
        return []
    entity = "song" if title else "album"
    try:
        r = cover.http_get(
            "https://itunes.apple.com/search",
            params={"term": term, "media": "music", "entity": entity, "limit": 4},
            headers={"User-Agent": cover.USER_AGENT},
            timeout=6.0,
        )
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
    except Exception:
        return []

    matches: list[dict] = []
    for item in results[:3]:
        art = (item.get("artworkUrl100") or "").replace("100x100bb", "600x600bb")
        c_artist = (item.get("artistName") or artist).strip()
        c_album = (item.get("collectionName") or album).strip()
        c_title = (item.get("trackName") or title).strip()
        c_aa = (item.get("collectionArtistName") or c_artist).strip()
        c_year = _norm_year(item.get("releaseDate"))
        c_track = _norm_track(item.get("trackNumber")) or track
        c_genre = (item.get("primaryGenreName") or "").strip()
        matches.append({
            "source": "Open (iTunes)",
            "artist": c_artist,
            "title": c_title,
            "album": c_album,
            "album_artist": c_aa,
            "year": c_year,
            "track": c_track,
            "genre": c_genre,
            "cover": art,
        })
    return matches


def _merge_candidate_list(cands: list[dict]) -> dict[str, Any]:
    if not cands:
        return {}
    merged = dict(cands[0])
    if not merged.get("cover") and merged.get("cover_url"):
        merged["cover"] = merged["cover_url"]
    for sec in cands[1:]:
        for f in METADATA_FIELDS:
            if not merged.get(f):
                val = sec.get(f) or (sec.get("cover_url") if f == "cover" else "")
                if val:
                    merged[f] = val
    return merged


def lookup_online_metadata(
    artist: str,
    album: str,
    title: str,
    track: str = "",
    options: dict | None = None,
    settings: dict | None = None,
    *,
    sources: list[str] | dict[str, bool] | None = None,
    db=None,
    force_refresh: bool = False,
    cancel=None,
) -> dict[str, Any]:
    """Query enabled internet sources (Discogs, MusicBrainz, Open/iTunes) and return merged canonical
    metadata as well as independent per-source matches under 'by_source'."""
    if cancel and cancel():
        return {}
    opts = options or {}
    raw_sources = sources if sources is not None else opts.get("sources_map") or opts.get("sources")
    if isinstance(raw_sources, dict):
        sources_cfg = {k: bool(raw_sources.get(k)) for k in ("discogs", "musicbrainz", "open")}
    elif isinstance(raw_sources, (list, tuple, set)):
        s_set = {str(x) for x in raw_sources}
        sources_cfg = {k: (k in s_set) for k in ("discogs", "musicbrainz", "open")}
    else:
        sources_cfg = {"discogs": True, "musicbrainz": True, "open": True}

    if not any(sources_cfg.values()):
        return {}

    src_sig = ",".join(k for k in ("discogs", "musicbrainz", "open") if sources_cfg.get(k))
    query_key = f"v2|{src_sig}|{artist.lower().strip()}|{album.lower().strip()}|{title.lower().strip()}"

    if db is not None and not force_refresh:
        row = db.one("SELECT payload FROM metadata_cache WHERE query_key = ?", (query_key,))
        if row:
            try:
                cached = json.loads(row["payload"])
                if isinstance(cached, dict):
                    return cached
            except Exception:
                pass

    st = settings or {}
    token = st.get("general", {}).get("discogsToken", "") if isinstance(st.get("general"), dict) else ""
    by_source: dict[str, dict[str, Any]] = {}
    candidates: list[dict] = []

    tasks: list[tuple[str, Any]] = []
    if sources_cfg.get("discogs") and not (cancel and cancel()):
        tasks.append(("discogs", lambda: _lookup_discogs(artist, album, title, track, token=token, cancel=cancel)))
    if sources_cfg.get("musicbrainz") and not (cancel and cancel()):
        tasks.append(("musicbrainz", lambda: _lookup_musicbrainz(artist, album, title, track, cancel=cancel)))
    if sources_cfg.get("open") and not (cancel and cancel()):
        tasks.append(("open", lambda: _lookup_itunes(artist, album, title, track, cancel=cancel)))

    if len(tasks) == 1:
        src_name, fn = tasks[0]
        src_cands = fn()
        if src_cands:
            by_source[src_name] = _merge_candidate_list(src_cands)
            candidates.extend(src_cands)
    elif len(tasks) > 1:
        futures = [(src_name, cover._HTTP_POOL.submit(fn)) for src_name, fn in tasks]
        for src_name, fut in futures:
            src_cands = fut.result()
            if src_cands:
                by_source[src_name] = _merge_candidate_list(src_cands)
                candidates.extend(src_cands)

    if cancel and cancel():
        return {}
    if not candidates:
        if db is not None:
            try:
                db.execute(
                    "INSERT INTO metadata_cache (query_key, payload, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(query_key) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                    (query_key, json.dumps({}), time.time()),
                )
            except Exception:
                pass
        return {}

    primary = _merge_candidate_list(candidates)
    primary["candidates"] = candidates
    primary["by_source"] = by_source

    if db is not None:
        try:
            db.execute(
                "INSERT INTO metadata_cache (query_key, payload, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(query_key) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at",
                (query_key, json.dumps(primary), time.time()),
            )
        except Exception:
            pass
    return primary


# ------------------------------------------------------------ comparison & selection

def extract_online_by_source(o_meta: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Extract normalized {'discogs': {...}, 'musicbrainz': {...}, 'open': {...}} from online_meta,
    handling new 'by_source' dicts, legacy 'candidates' arrays, and single-source dicts."""
    out: dict[str, dict[str, Any]] = {"discogs": {}, "musicbrainz": {}, "open": {}}
    if not o_meta or not isinstance(o_meta, dict):
        return out

    raw_bs = o_meta.get("by_source")
    if isinstance(raw_bs, dict):
        for k in ("discogs", "musicbrainz", "open"):
            sub = raw_bs.get(k)
            if isinstance(sub, dict) and sub:
                d = dict(sub)
                if not d.get("cover") and d.get("cover_url"):
                    d["cover"] = d["cover_url"]
                out[k] = d

    cands = o_meta.get("candidates")
    if isinstance(cands, list) and cands:
        grouped: dict[str, list[dict]] = {"discogs": [], "musicbrainz": [], "open": []}
        for c in cands:
            if not isinstance(c, dict):
                continue
            s_lbl = str(c.get("source") or "").lower()
            if "discogs" in s_lbl:
                grouped["discogs"].append(c)
            elif "musicbrainz" in s_lbl:
                grouped["musicbrainz"].append(c)
            elif "open" in s_lbl or "itunes" in s_lbl:
                grouped["open"].append(c)
        for k, grp in grouped.items():
            if not out[k] and grp:
                out[k] = _merge_candidate_list(grp)

    if not any(out.values()):
        has_any_field = any(o_meta.get(f) or (f == "cover" and o_meta.get("cover_url")) for f in METADATA_FIELDS)
        if has_any_field:
            d = dict(o_meta)
            if not d.get("cover") and d.get("cover_url"):
                d["cover"] = d["cover_url"]
            s_lbl = str(o_meta.get("source") or "").lower()
            if "musicbrainz" in s_lbl:
                out["musicbrainz"] = d
            elif "open" in s_lbl or "itunes" in s_lbl:
                out["open"] = d
            else:
                out["discogs"] = d
    return out


def _values_differ(field: str, a: str, b: str) -> bool:
    sa = (a or "").strip()
    sb = (b or "").strip()
    if field == "track":
        return _norm_track(sa) != _norm_track(sb)
    if field == "year":
        return _norm_year(sa) != _norm_year(sb)
    if field == "cover":
        # If current cover is 'present', it is not flagged as missing during automatic comparison
        if sa == "present":
            return False
        return bool(sb)
    return sa != sb


def compare_file_metadata(
    relpath: str,
    current: dict[str, Any] | None = None,
    path_meta: dict[str, Any] | None = None,
    online_meta: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    existing_selected: dict[str, Any] | None = None,
    *,
    current_tags: dict[str, Any] | None = None,
    online_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare current file tags against folder/file name and internet sources for the enabled fields."""
    cur = dict(current if current is not None else (current_tags or {}))
    if not cur.get("cover") and cur.get("has_cover"):
        cur["cover"] = "present"
    p_meta = dict(path_meta if path_meta is not None else parse_path_full(relpath))
    o_meta = dict(online_match if online_match is not None else (online_meta or {}))
    if not o_meta.get("cover") and o_meta.get("cover_url"):
        o_meta["cover"] = o_meta["cover_url"]

    opts = options or {}
    raw_fields = opts.get("fields_map") or opts.get("fields")
    if isinstance(raw_fields, dict):
        enabled_fields = {f: bool(raw_fields.get(f, True)) for f in METADATA_FIELDS}
    elif isinstance(raw_fields, (list, tuple, set)):
        f_set = {str(x) for x in raw_fields}
        enabled_fields = {f: (f in f_set) for f in METADATA_FIELDS}
    else:
        enabled_fields = {f: True for f in METADATA_FIELDS}

    raw_sources = opts.get("sources_map") or opts.get("sources")
    if isinstance(raw_sources, dict):
        enabled_sources = {k: bool(raw_sources.get(k, True)) for k in ("discogs", "musicbrainz", "open")}
    elif isinstance(raw_sources, (list, tuple, set)):
        s_set = {str(x) for x in raw_sources}
        enabled_sources = {k: (k in s_set) for k in ("discogs", "musicbrainz", "open")}
    else:
        enabled_sources = {"discogs": True, "musicbrainz": True, "open": True}

    check_path = bool(
        opts.get("check_path_vs_tags", opts.get("checkPath", True))
    )

    by_src = extract_online_by_source(o_meta)
    resolved = cover.resolve_search_metadata(relpath, cur)
    diffs: list[str] = []
    path_mismatches: list[str] = []
    selected: dict[str, dict[str, str]] = {}
    fields_out: dict[str, dict[str, Any]] = {}

    # 1. Check Artist, Title, Album against folder/file name when check_path is enabled
    if check_path:
        for pf in ("artist", "title", "album"):
            p_val = (p_meta.get(pf) or "").strip()
            c_val = (cur.get(pf) or "").strip()
            if p_val and (not c_val or cover._similarity(c_val, p_val) < 0.75 or c_val.lower() != p_val.lower()):
                path_mismatches.append(pf)

    # 2. Evaluate all 8 fields
    for f in METADATA_FIELDS:
        is_enabled = enabled_fields.get(f, True)
        c_val = (cur.get(f) or "").strip()
        p_val = (p_meta.get(f) or "").strip()
        d_val = (by_src["discogs"].get(f) or "").strip() if enabled_sources.get("discogs", True) else ""
        m_val = (by_src["musicbrainz"].get(f) or "").strip() if enabled_sources.get("musicbrainz", True) else ""
        i_val = (by_src["open"].get(f) or "").strip() if enabled_sources.get("open", True) else ""
        o_val = d_val or m_val or i_val or ((o_meta.get(f) or "").strip() if any(enabled_sources.values()) else "")

        has_discogs_diff = bool(is_enabled and d_val and _values_differ(f, c_val, d_val))
        has_mb_diff = bool(is_enabled and m_val and _values_differ(f, c_val, m_val))
        has_open_diff = bool(is_enabled and i_val and _values_differ(f, c_val, i_val))
        has_online_diff = has_discogs_diff or has_mb_diff or has_open_diff or bool(is_enabled and o_val and _values_differ(f, c_val, o_val))
        has_path_diff = bool(is_enabled and check_path and p_val and _values_differ(f, c_val, p_val))

        prior_sel = existing_selected.get(f) if (existing_selected and isinstance(existing_selected.get(f), dict)) else None
        resolved_prior: dict[str, str] | None = None
        has_selected_diff = False
        if prior_sel:
            p_choice = str(prior_sel.get("choice") or "current").strip()
            p_sval = str(prior_sel.get("value") or "").strip()
            if p_choice == "online":
                if d_val and (not p_sval or p_sval == d_val):
                    resolved_prior = {"choice": "discogs", "value": d_val}
                elif m_val and (not p_sval or p_sval == m_val):
                    resolved_prior = {"choice": "musicbrainz", "value": m_val}
                elif i_val and (not p_sval or p_sval == i_val):
                    resolved_prior = {"choice": "open", "value": i_val}
                else:
                    resolved_prior = {"choice": "online", "value": p_sval or o_val}
            elif p_choice == "discogs":
                resolved_prior = {"choice": "discogs", "value": d_val or p_sval}
            elif p_choice == "musicbrainz":
                resolved_prior = {"choice": "musicbrainz", "value": m_val or p_sval}
            elif p_choice == "open":
                resolved_prior = {"choice": "open", "value": i_val or p_sval}
            elif p_choice == "path":
                resolved_prior = {"choice": "path", "value": p_val or p_sval}
            elif p_choice == "custom":
                resolved_prior = {"choice": "custom", "value": p_sval}
            else:
                resolved_prior = {"choice": "current", "value": c_val}

            r_choice = resolved_prior["choice"]
            r_val = resolved_prior["value"]
            if r_choice != "current":
                if f == "cover":
                    has_selected_diff = bool(r_val)
                elif r_choice == "custom":
                    has_selected_diff = _values_differ(f, c_val, r_val)
                elif r_val:
                    has_selected_diff = _values_differ(f, c_val, r_val)

        is_diff = has_online_diff or has_path_diff or has_selected_diff

        if is_diff:
            diffs.append(f)
            if resolved_prior is not None:
                selected[f] = resolved_prior
            elif has_discogs_diff:
                selected[f] = {"choice": "discogs", "value": d_val}
            elif has_mb_diff:
                selected[f] = {"choice": "musicbrainz", "value": m_val}
            elif has_open_diff:
                selected[f] = {"choice": "open", "value": i_val}
            elif has_online_diff:
                selected[f] = {"choice": "online", "value": o_val}
            elif has_path_diff:
                selected[f] = {"choice": "path", "value": p_val}
            else:
                selected[f] = {"choice": "current", "value": c_val}
        else:
            if resolved_prior is not None:
                selected[f] = resolved_prior
            else:
                selected[f] = {"choice": "current", "value": c_val}

        sel_obj = selected[f]
        choice = sel_obj.get("choice", "current")
        custom_val = sel_obj.get("value", "") if choice == "custom" else ""
        fields_out[f] = {
            "current": c_val,
            "path": p_val,
            "discogs": d_val,
            "musicbrainz": m_val,
            "open": i_val,
            "online": o_val,
            "choice": choice,
            "custom_value": custom_val,
            "diff": is_diff,
            "path_vs_tag_diff": f in path_mismatches,
            "missing": not bool(c_val),
        }

    status = "diff" if diffs else "ok"
    return {
        "status": status,
        "diffs": diffs,
        "diff_count": len(diffs),
        "path_mismatches": path_mismatches,
        "path_diff": 1 if path_mismatches else 0,
        "path_diff_count": len(path_mismatches),
        "query_source": resolved["source"],
        "selected": selected,
        "fields": fields_out,
    }
