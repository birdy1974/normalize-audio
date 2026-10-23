"""Audio metadata inspection, tag-vs-path resolution, online cover art search (Discogs + Open sources),
and lossless cover picture embedding across audio formats."""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import time
import unicodedata
from typing import Any

import httpx
import mutagen
from mutagen.aiff import AIFF
from mutagen.asf import ASF, ASFByteArrayAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover
from mutagen.wave import WAVE

log = logging.getLogger("normalizer")

USER_AGENT = "AudioNormalizer/1.0 (+https://github.com/birdy1974/normalize-audio)"
LOCAL_COVER_NAMES = (
    "cover.jpg", "cover.jpeg", "cover.png",
    "folder.jpg", "folder.jpeg", "folder.png",
    "front.jpg", "front.jpeg", "front.png",
    "album.jpg", "album.jpeg", "album.png",
)

PLACEHOLDER_PATTERNS = re.compile(
    r"^(?:unknown(?:\s+artist|\s+album|\s+title)?|untitled|various(?:\s+artists)?|"
    r"track\s*\d*|audio\s*track\s*\d*|no\s+title|no\s+artist|no\s+album|n/?a|none|null|"
    r"(?:https?://|www\.)\S+)$",
    re.IGNORECASE,
)

GENERIC_DIRS = {
    ".", "music", "audio", "mp3", "flac", "wav", "m4a", "ogg", "opus",
    "albums", "songs", "tracks", "library", "media", "lossless", "downloads",
}
DISC_RE = re.compile(r"^(?:cd|disc|disk)\s*\d+$", re.IGNORECASE)
YEAR_PREFIX_RE = re.compile(r"^(?:\(\d{4}\)|\[\d{4}\]|\d{4})\s*[-_.]\s*")
YEAR_SUFFIX_RE = re.compile(r"\s*(?:\(\d{4}\)|\[\d{4}\])$")
BRACKET_NOISE_RE = re.compile(
    r"\s*(?:\((?:official[^)]*|audio[^)]*|video[^)]*|lyrics[^)]*|hd|hq|\d+\s*kbps)\)|"
    r"\[(?:official[^\]]*|audio[^\]]*|video[^\]]*|lyrics[^\]]*|flac|mp3|\d+\s*kbps)\])",
    re.IGNORECASE,
)
TRACK_NUM_RE = re.compile(
    r"^(?:(?:cd|disc)?\s*\d+[-_.]\s*)?(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3})\s*(?:[-_.)]+\s*|\s+(?=[A-Za-z]))",
    re.IGNORECASE,
)
STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by",
    "from", "feat", "ft", "featuring", "vs", "remaster", "remastered", "edition", "deluxe",
    "version", "mix", "original", "album", "single", "ep", "vol", "volume", "part", "pt",
}


def detect_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


# ------------------------------------------------------------ tag inspection

def _clean_str(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        val = val[0] if val else ""
    text = " ".join(str(val).replace("\x00", " ").split()).strip()
    if not text or PLACEHOLDER_PATTERNS.match(text):
        return ""
    return text


def _id3_text(id3: ID3, key: str) -> str:
    frame = id3.get(key)
    if not frame or not getattr(frame, "text", None):
        return ""
    return _clean_str(frame.text[0])


def _vorbis_text(tags: Any, *keys: str) -> str:
    if not tags:
        return ""
    for k in keys:
        v = tags.get(k) or tags.get(k.upper()) or tags.get(k.lower())
        if v:
            s = _clean_str(v)
            if s:
                return s
    return ""


def inspect_audio(path: str) -> dict:
    """Read metadata tags and check whether an embedded picture is present.
    Never modifies the file on disk."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    out = {
        "has_cover": False,
        "artist": "",
        "album_artist": "",
        "album": "",
        "title": "",
        "year": "",
    }
    if not os.path.isfile(path):
        return out
    try:
        if ext in ("mp3", "aac"):
            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                return out
            out["has_cover"] = bool(id3.getall("APIC"))
            out["artist"] = _id3_text(id3, "TPE1")
            out["album_artist"] = _id3_text(id3, "TPE2")
            out["album"] = _id3_text(id3, "TALB")
            out["title"] = _id3_text(id3, "TIT2")
            out["year"] = (_id3_text(id3, "TDRC") or _id3_text(id3, "TYER"))[:4]
            return out

        if ext == "flac":
            audio = FLAC(path)
            out["has_cover"] = bool(audio.pictures)
            tags = audio.tags or {}
            out["artist"] = _vorbis_text(tags, "artist")
            out["album_artist"] = _vorbis_text(tags, "albumartist", "album artist")
            out["album"] = _vorbis_text(tags, "album")
            out["title"] = _vorbis_text(tags, "title")
            out["year"] = _vorbis_text(tags, "date", "year")[:4]
            return out

        if ext == "m4a":
            audio = MP4(path)
            tags = audio.tags or {}
            out["has_cover"] = bool(tags.get("covr"))
            out["artist"] = _clean_str(tags.get("\xa9ART"))
            out["album_artist"] = _clean_str(tags.get("aART"))
            out["album"] = _clean_str(tags.get("\xa9alb"))
            out["title"] = _clean_str(tags.get("\xa9nam"))
            out["year"] = _clean_str(tags.get("\xa9day"))[:4]
            return out

        if ext in ("ogg", "oga", "opus"):
            audio = mutagen.File(path)
            tags = (audio.tags if audio else None) or {}
            out["has_cover"] = bool(tags.get("metadata_block_picture") or tags.get("coverart"))
            out["artist"] = _vorbis_text(tags, "artist")
            out["album_artist"] = _vorbis_text(tags, "albumartist", "album artist")
            out["album"] = _vorbis_text(tags, "album")
            out["title"] = _vorbis_text(tags, "title")
            out["year"] = _vorbis_text(tags, "date", "year")[:4]
            return out

        if ext in ("wav", "aif", "aiff"):
            audio = WAVE(path) if ext == "wav" else AIFF(path)
            id3 = audio.tags
            if id3 and isinstance(id3, ID3):
                out["has_cover"] = bool(id3.getall("APIC"))
                out["artist"] = _id3_text(id3, "TPE1")
                out["album_artist"] = _id3_text(id3, "TPE2")
                out["album"] = _id3_text(id3, "TALB")
                out["title"] = _id3_text(id3, "TIT2")
                out["year"] = (_id3_text(id3, "TDRC") or _id3_text(id3, "TYER"))[:4]
            return out

        if ext == "wma":
            audio = ASF(path)
            tags = audio.tags or {}
            out["has_cover"] = bool(tags.get("WM/Picture"))
            out["artist"] = _clean_str(tags.get("Author")) or _clean_str(tags.get("WM/AlbumArtist"))
            out["album_artist"] = _clean_str(tags.get("WM/AlbumArtist"))
            out["album"] = _clean_str(tags.get("WM/AlbumTitle"))
            out["title"] = _clean_str(tags.get("Title"))
            out["year"] = _clean_str(tags.get("WM/Year"))[:4]
            return out

        audio = mutagen.File(path)
        if audio and audio.tags:
            tags = audio.tags
            if isinstance(tags, ID3):
                out["has_cover"] = bool(tags.getall("APIC"))
    except Exception as exc:
        log.debug("inspect_audio failed for %s: %s", path, exc)
    return out


def extract_cover(path: str) -> tuple[bytes, str] | None:
    """Return (image_bytes, mime_type) of the embedded front cover, or None."""
    if not os.path.isfile(path):
        return None
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    try:
        if ext in ("mp3", "aac"):
            id3 = ID3(path)
            apics = id3.getall("APIC")
            if apics:
                pic = next((a for a in apics if a.type == 3), apics[0])
                return bytes(pic.data), detect_mime(pic.data, pic.mime or "image/jpeg")
            return None

        if ext == "flac":
            audio = FLAC(path)
            if audio.pictures:
                pic = next((p for p in audio.pictures if p.type == 3), audio.pictures[0])
                return bytes(pic.data), detect_mime(pic.data, pic.mime or "image/jpeg")
            return None

        if ext == "m4a":
            audio = MP4(path)
            covr = (audio.tags or {}).get("covr")
            if covr:
                item = covr[0]
                raw = bytes(item)
                mime = "image/png" if getattr(item, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
                return raw, detect_mime(raw, mime)
            return None

        if ext in ("ogg", "oga", "opus"):
            audio = mutagen.File(path)
            tags = (audio.tags if audio else None) or {}
            mbp = tags.get("metadata_block_picture")
            if mbp:
                pic = Picture(base64.b64decode(mbp[0]))
                return bytes(pic.data), detect_mime(pic.data, pic.mime or "image/jpeg")
            return None

        if ext in ("wav", "aif", "aiff"):
            audio = WAVE(path) if ext == "wav" else AIFF(path)
            id3 = audio.tags
            if id3 and isinstance(id3, ID3):
                apics = id3.getall("APIC")
                if apics:
                    pic = next((a for a in apics if a.type == 3), apics[0])
                    return bytes(pic.data), detect_mime(pic.data, pic.mime or "image/jpeg")
            return None

        if ext == "wma":
            audio = ASF(path)
            pics = (audio.tags or {}).get("WM/Picture")
            if pics:
                unpacked = _unpack_asf_picture(bytes(pics[0].value))
                if unpacked:
                    return unpacked
            return None
    except Exception as exc:
        log.debug("extract_cover failed for %s: %s", path, exc)
    return None


def _pack_asf_picture(image_bytes: bytes, mime: str, desc: str = "Cover") -> bytes:
    mime_b = (mime + "\x00").encode("utf-16-le")
    desc_b = (desc + "\x00").encode("utf-16-le")
    header = struct.pack("<BI", 3, len(image_bytes))
    return header + mime_b + desc_b + image_bytes


def _unpack_asf_picture(raw: bytes) -> tuple[bytes, str] | None:
    if len(raw) < 9:
        return None
    _ptype, data_len = struct.unpack_from("<BI", raw, 0)
    pos = 5
    # read two null-terminated UTF-16LE strings (mime and description)
    strings = []
    for _ in range(2):
        chars = []
        while pos + 1 < len(raw):
            ch = raw[pos:pos + 2]
            pos += 2
            if ch == b"\x00\x00":
                break
            chars.append(ch)
        strings.append(b"".join(chars).decode("utf-16-le", errors="replace"))
    img = raw[pos:pos + data_len] if data_len else raw[pos:]
    if not img:
        return None
    mime = strings[0] if strings and strings[0] else "image/jpeg"
    return img, detect_mime(img, mime)


def embed_cover(path: str, image_bytes: bytes, mime: str | None = None,
                preserve_mtime: bool = True) -> None:
    """Embed front cover art into an audio file safely via a temporary sibling file."""
    if not os.path.isfile(path):
        raise ValueError("audio file not found")
    if not image_bytes or len(image_bytes) < 16:
        raise ValueError("invalid cover image data")
    actual_mime = detect_mime(image_bytes, mime or "image/jpeg")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    stat_before = os.stat(path)
    folder = os.path.dirname(path) or "."
    base = os.path.basename(path)
    tmp = os.path.join(folder, f".{base}.cover-{os.getpid()}-{int(time.time() * 1000)}.nrm-tmp")
    shutil.copy2(path, tmp)
    try:
        if ext in ("mp3", "aac"):
            try:
                id3 = ID3(tmp)
            except ID3NoHeaderError:
                id3 = ID3()
            id3.delall("APIC")
            id3.add(APIC(encoding=3, mime=actual_mime, type=3, desc="Cover", data=image_bytes))
            id3.save(tmp, v2_version=3)
        elif ext == "flac":
            audio = FLAC(tmp)
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3
            pic.mime = actual_mime
            pic.desc = "Cover"
            pic.data = image_bytes
            audio.add_picture(pic)
            audio.save()
        elif ext == "m4a":
            audio = MP4(tmp)
            if audio.tags is None:
                audio.add_tags()
            fmt = MP4Cover.FORMAT_PNG if actual_mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio.tags["covr"] = [MP4Cover(image_bytes, imageformat=fmt)]
            audio.save()
        elif ext in ("ogg", "oga", "opus"):
            audio = mutagen.File(tmp)
            if audio is None:
                raise ValueError(f"unsupported Ogg/Opus file: {base}")
            if audio.tags is None:
                audio.add_tags()
            pic = Picture()
            pic.type = 3
            pic.mime = actual_mime
            pic.desc = "Cover"
            pic.data = image_bytes
            audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
            audio.save()
        elif ext in ("wav", "aif", "aiff"):
            audio = WAVE(tmp) if ext == "wav" else AIFF(tmp)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(encoding=3, mime=actual_mime, type=3, desc="Cover", data=image_bytes))
            audio.save()
        elif ext == "wma":
            audio = ASF(tmp)
            if audio.tags is None:
                audio.add_tags()
            packed = _pack_asf_picture(image_bytes, actual_mime)
            audio.tags["WM/Picture"] = [ASFByteArrayAttribute(packed)]
            audio.save()
        else:
            raise ValueError(f"embedding cover art is not supported for .{ext} files")

        os.replace(tmp, path)
        if preserve_mtime:
            os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# ------------------------------------------------------------ path vs tag resolution

def _clean_folder_segment(seg: str) -> str:
    s = YEAR_PREFIX_RE.sub("", seg.strip())
    s = YEAR_SUFFIX_RE.sub("", s)
    s = BRACKET_NOISE_RE.sub("", s)
    return " ".join(s.replace("_", " ").split()).strip(" -_.")


def _clean_stem(stem: str) -> str:
    s = BRACKET_NOISE_RE.sub("", stem.strip())
    s = TRACK_NUM_RE.sub("", s)
    s = " ".join(s.replace("_", " ").split()).strip(" -_.")
    if PLACEHOLDER_PATTERNS.match(s):
        return ""
    return s


def parse_path_metadata(relpath: str) -> dict[str, str]:
    """Extract (artist, album, title) from the relative folder and file path."""
    norm = (relpath or "").replace("\\", "/").strip("/")
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts:
        return {"artist": "", "album": "", "title": ""}

    filename = parts[-1]
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    folders = parts[:-1]

    # Strip leading generic collection folders when deeper folders exist
    while len(folders) > 1 and folders[0].lower() in GENERIC_DIRS:
        folders = folders[1:]
    # If the only folder is a generic root name like "Music", ignore it as album/artist
    if len(folders) == 1 and folders[0].lower() in GENERIC_DIRS:
        folders = []
    # Step up from CD1 / Disc 1 folders
    if folders and DISC_RE.match(folders[-1].strip()):
        folders = folders[:-1]

    cleaned_stem = _clean_stem(stem)
    file_artist = ""
    file_album = ""
    file_title = cleaned_stem

    if " - " in cleaned_stem:
        dash_parts = [p.strip() for p in cleaned_stem.split(" - ") if p.strip()]
        # Strip track numbers from the second part if format is "Artist - 01 - Title"
        if len(dash_parts) >= 3:
            file_artist = _clean_stem(dash_parts[0])
            mid = TRACK_NUM_RE.sub("", dash_parts[1]).strip()
            if mid and not mid.isdigit():
                file_album = mid
            file_title = _clean_stem(dash_parts[-1])
        elif len(dash_parts) == 2:
            left, right = dash_parts[0], dash_parts[1]
            if not left.isdigit():
                file_artist = _clean_stem(left)
                file_title = _clean_stem(right)

    dir_artist = ""
    dir_album = ""
    if len(folders) >= 2:
        dir_artist = _clean_folder_segment(folders[-2])
        dir_album = _clean_folder_segment(folders[-1])
        if " - " in dir_album and not dir_artist:
            da_parts = [p.strip() for p in dir_album.split(" - ", 1)]
            if len(da_parts) == 2:
                dir_artist, dir_album = da_parts[0], da_parts[1]
        elif dir_artist and dir_album.lower().startswith(dir_artist.lower() + " - "):
            dir_album = dir_album[len(dir_artist) + 3:].strip()
    elif len(folders) == 1:
        leaf = _clean_folder_segment(folders[0])
        if " - " in leaf:
            da_parts = [p.strip() for p in leaf.split(" - ", 1)]
            dir_artist = da_parts[0]
            dir_album = da_parts[1]
        elif file_artist:
            if leaf.lower() == file_artist.lower():
                dir_artist = leaf
            else:
                dir_album = leaf
        else:
            dir_album = leaf

    artist = dir_artist or file_artist
    album = dir_album or file_album
    title = file_title
    return {
        "artist": artist if not PLACEHOLDER_PATTERNS.match(artist) else "",
        "album": album if not PLACEHOLDER_PATTERNS.match(album) else "",
        "title": title if not PLACEHOLDER_PATTERNS.match(title) else "",
    }


def _norm_tokens(text: str) -> set[str]:
    if not text:
        return set()
    decomposed = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]{2,}", ascii_text)
    meaningful = {w for w in words if w not in STOPWORDS and not w.isdigit()}
    return meaningful or set(words)


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if ta and tb:
        overlap = ta & tb
        if overlap:
            return len(overlap) / min(len(ta), len(tb))
        for wa in ta:
            for wb in tb:
                if len(wa) >= 4 and len(wb) >= 4 and (wa in wb or wb in wa):
                    return 0.85
        best_word_sim = max(
            (difflib.SequenceMatcher(None, wa, wb).ratio() for wa in ta for wb in tb),
            default=0.0,
        )
        return best_word_sim * 0.5
    na = re.sub(r"[^a-z0-9]+", " ", a.lower()).strip()
    nb = re.sub(r"[^a-z0-9]+", " ", b.lower()).strip()
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def are_tags_totally_different(tags: dict, path_meta: dict) -> bool:
    """Return True when both ID3/file tags and folder/file name have meaningful text,
    but they are completely unrelated (e.g. wrong/leftover ID3 tags on a renamed file/folder)."""
    tag_artist = _clean_str(tags.get("album_artist") or tags.get("artist"))
    tag_album = _clean_str(tags.get("album"))
    tag_title = _clean_str(tags.get("title"))

    path_artist = _clean_str(path_meta.get("artist"))
    path_album = _clean_str(path_meta.get("album"))
    path_title = _clean_str(path_meta.get("title"))

    tag_all = " ".join(x for x in (tag_artist, tag_album, tag_title) if x)
    path_all = " ".join(x for x in (path_artist, path_album, path_title) if x)

    tag_tokens = _norm_tokens(tag_all)
    path_tokens = _norm_tokens(path_all)
    if not tag_tokens or not path_tokens:
        return False

    # Check overall token overlap and string similarity
    overall_sim = _similarity(tag_all, path_all)
    if overall_sim < 0.38:
        return True

    # Even if one minor word happens to match, check if both artist and title/album are present and conflict
    comparisons = []
    if tag_artist and path_artist:
        comparisons.append(_similarity(tag_artist, path_artist))
    if tag_album and path_album:
        comparisons.append(_similarity(tag_album, path_album))
    if tag_title and path_title:
        comparisons.append(_similarity(tag_title, path_title))

    if len(comparisons) >= 2 and max(comparisons) < 0.38:
        return True
    if len(comparisons) == 1 and comparisons[0] < 0.32 and len(tag_tokens & path_tokens) == 0:
        return True
    return False


def resolve_search_metadata(relpath: str, tags: dict) -> dict:
    """Determine the best (artist, album, title) search query by combining ID3/file tags
    with folder/file name parsing, and overriding with the folder/file name when the
    ID3 tags are totally different from the folder/file name."""
    path_meta = parse_path_metadata(relpath)
    tag_artist = _clean_str(tags.get("album_artist") or tags.get("artist"))
    tag_album = _clean_str(tags.get("album"))
    tag_title = _clean_str(tags.get("title"))

    path_artist = path_meta["artist"]
    path_album = path_meta["album"]
    path_title = path_meta["title"]

    has_any_tag = bool(tag_artist or tag_album or tag_title)
    has_any_path = bool(path_artist or path_album or path_title)

    if not has_any_tag:
        return {
            "artist": path_artist,
            "album": path_album,
            "title": path_title,
            "source": "path",
            "tag_artist": tag_artist,
            "tag_album": tag_album,
            "tag_title": tag_title,
            "path_artist": path_artist,
            "path_album": path_album,
            "path_title": path_title,
        }

    if has_any_path and are_tags_totally_different(tags, path_meta):
        return {
            "artist": path_artist,
            "album": path_album,
            "title": path_title,
            "source": "path_override",
            "tag_artist": tag_artist,
            "tag_album": tag_album,
            "tag_title": tag_title,
            "path_artist": path_artist,
            "path_album": path_album,
            "path_title": path_title,
        }

    # Field-level check: if artist matches path_artist, but tag_album is totally different from path_album
    # (e.g. a compilation tag on a track placed inside an Artist/Album folder), prefer path_album.
    resolved_artist = tag_artist or path_artist
    resolved_album = tag_album or path_album
    resolved_title = tag_title or path_title
    source = "tags"

    if tag_artist and path_artist and _similarity(tag_artist, path_artist) < 0.35 and (
        (tag_album and path_album and _similarity(tag_album, path_album) < 0.35)
        or (not tag_album and path_album)
    ):
        resolved_artist = path_artist
        resolved_album = path_album or resolved_album
        source = "path_override"
    elif tag_album and path_album and _similarity(tag_album, path_album) < 0.32 and path_artist:
        resolved_album = path_album
        source = "path_override"

    if tag_title and path_title and _similarity(tag_title, path_title) < 0.30 and (
        not tag_artist or source == "path_override"
    ):
        resolved_title = path_title
        source = "path_override"

    return {
        "artist": resolved_artist,
        "album": resolved_album,
        "title": resolved_title,
        "source": source,
        "tag_artist": tag_artist,
        "tag_album": tag_album,
        "tag_title": tag_title,
        "path_artist": path_artist,
        "path_album": path_album,
        "path_title": path_title,
    }


def find_local_cover(audio_path: str) -> dict | None:
    """Return a local folder cover candidate if cover.jpg / folder.jpg exists next to the file."""
    folder = os.path.dirname(audio_path)
    if not folder or not os.path.isdir(folder):
        return None
    try:
        entries = {name.lower(): name for name in os.listdir(folder)}
    except OSError:
        return None
    for wanted in LOCAL_COVER_NAMES:
        actual = entries.get(wanted)
        if actual:
            full = os.path.join(folder, actual)
            if os.path.isfile(full) and os.path.getsize(full) > 64:
                return {
                    "id": f"local:{actual}",
                    "source": "Local folder",
                    "title": actual,
                    "artist": "",
                    "album": os.path.basename(folder),
                    "year": "",
                    "image_url": f"local://{full}",
                    "thumb_url": f"local://{full}",
                }
    return None


# ------------------------------------------------------------ online search

def _discogs_headers(token: str = "") -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.discogs.v2+json",
    }
    if token and token.strip():
        headers["Authorization"] = f"Discogs token={token.strip()}"
    return headers


def test_discogs(token: str = "") -> dict:
    """Verify connectivity (and token if given) against the Discogs API."""
    url = "https://api.discogs.com/database/search"
    try:
        r = httpx.get(
            url,
            params={"q": "Discovery", "type": "master", "per_page": 1},
            headers=_discogs_headers(token),
            timeout=6.0,
        )
        if r.status_code == 200:
            auth_note = "authenticated with token" if (token and token.strip()) else "anonymous access"
            return {"ok": True, "message": f"Connected to Discogs ({auth_note})."}
        if r.status_code == 401:
            return {"ok": False, "message": "Discogs rejected the token (HTTP 401). Check your Personal Access Token."}
        return {"ok": False, "message": f"Discogs returned HTTP {r.status_code}."}
    except Exception as exc:
        return {"ok": False, "message": f"Could not reach Discogs: {exc}"}


def _search_discogs(artist: str, album: str, title: str, token: str = "") -> list[dict]:
    """Query the native Discogs database search endpoint and return candidate cover dicts."""
    headers = _discogs_headers(token)
    queries: list[dict[str, Any]] = []
    if artist and album:
        queries.append({"artist": artist, "release_title": album, "type": "master", "per_page": 10})
        queries.append({"artist": artist, "release_title": album, "type": "release", "per_page": 10})
    elif artist and title:
        queries.append({"artist": artist, "track": title, "type": "master", "per_page": 10})
        queries.append({"artist": artist, "release_title": title, "per_page": 10})
    else:
        q = " ".join(x for x in (artist, album, title) if x).strip()
        if q:
            queries.append({"q": q, "per_page": 10})

    candidates: list[dict] = []
    seen_masters: set[Any] = set()
    seen_images: set[str] = set()

    for params in queries:
        try:
            r = httpx.get("https://api.discogs.com/database/search", params=params, headers=headers, timeout=6.0)
            if r.status_code != 200:
                continue
            results = r.json().get("results") or []
        except Exception as exc:
            log.debug("Discogs search failed (%s): %s", params, exc)
            continue

        for item in results:
            cover_url = (item.get("cover_image") or "").strip()
            thumb_url = (item.get("thumb") or cover_url).strip()
            if not cover_url or cover_url.endswith("spacer.gif"):
                continue
            master_id = item.get("master_id") or item.get("id")
            norm_title = re.sub(r"[^a-z0-9]+", "", (item.get("title") or "").lower())
            dedup_key = f"m:{master_id}" if item.get("master_id") else f"t:{norm_title}:{cover_url}"
            if dedup_key in seen_masters or cover_url in seen_images:
                continue
            seen_masters.add(dedup_key)
            seen_images.add(cover_url)

            raw_title = (item.get("title") or "").strip()
            c_artist, c_album = artist, album
            if " - " in raw_title:
                c_artist, c_album = [p.strip() for p in raw_title.split(" - ", 1)]
            elif raw_title:
                c_album = raw_title

            candidates.append({
                "id": f"discogs:{item.get('id', len(candidates))}",
                "source": "Discogs",
                "title": raw_title or f"{c_artist} - {c_album}".strip(" -"),
                "artist": c_artist,
                "album": c_album,
                "year": str(item.get("year") or ""),
                "image_url": cover_url,
                "thumb_url": thumb_url or cover_url,
            })
            if len(candidates) >= 6:
                break
        if candidates:
            break
    return candidates


def _search_musicbrainz_caa(artist: str, album: str, title: str) -> list[dict]:
    """Query MusicBrainz release-groups and Cover Art Archive (100% open source, no API key)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    q_parts = []
    if artist:
        q_parts.append(f'artist:"{artist}"')
    if album:
        q_parts.append(f'releasegroup:"{album}"')
    elif title:
        q_parts.append(f'release:"{title}"')
    if not q_parts:
        return []

    candidates: list[dict] = []
    try:
        r = httpx.get(
            "https://musicbrainz.org/ws/2/release-group/",
            params={"query": " AND ".join(q_parts), "fmt": "json", "limit": 5},
            headers=headers,
            timeout=6.0,
        )
        if r.status_code != 200:
            return []
        groups = r.json().get("release-groups") or []
    except Exception as exc:
        log.debug("MusicBrainz search failed: %s", exc)
        return []

    seen_titles: set[str] = set()
    for g in groups[:4]:
        mbid = g.get("id")
        if not mbid:
            continue
        rg_title = (g.get("title") or album or title).strip()
        ac = g.get("artist-credit") or []
        rg_artist = (ac[0].get("name") if ac and isinstance(ac[0], dict) else artist) or artist
        year = str(g.get("first-release-date") or "")[:4]
        try:
            caa = httpx.get(
                f"https://coverartarchive.org/release-group/{mbid}",
                headers=headers,
                follow_redirects=True,
                timeout=5.0,
            )
            if caa.status_code != 200:
                continue
            images = caa.json().get("images") or []
            front = next((img for img in images if img.get("front")), images[0] if images else None)
            if not front or not front.get("image"):
                continue
            thumbs = front.get("thumbnails") or {}
            thumb_url = thumbs.get("500") or thumbs.get("large") or thumbs.get("small") or front["image"]
            image_url = thumbs.get("1200") or front["image"]
            t_key = f"{rg_artist.lower()}::{rg_title.lower()}"
            if t_key in seen_titles and len(candidates) >= 1:
                continue
            seen_titles.add(t_key)
            candidates.append({
                "id": f"caa:{mbid}",
                "source": "Cover Art Archive",
                "title": f"{rg_artist} - {rg_title}".strip(" -"),
                "artist": rg_artist,
                "album": rg_title,
                "year": year,
                "image_url": image_url,
                "thumb_url": thumb_url,
            })
        except Exception:
            continue
    return candidates


def _search_itunes(artist: str, album: str, title: str) -> list[dict]:
    """Query Apple iTunes Search API as an open fallback for album/track artwork."""
    term = " ".join(x for x in (artist, album or title) if x).strip()
    if not term:
        return []
    entity = "album" if album else "song"
    candidates: list[dict] = []
    seen_urls: set[str] = set()
    try:
        r = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": term, "media": "music", "entity": entity, "limit": 6},
            headers={"User-Agent": USER_AGENT},
            timeout=6.0,
        )
        if r.status_code != 200:
            return []
        results = r.json().get("results") or []
    except Exception as exc:
        log.debug("iTunes search failed: %s", exc)
        return []

    for item in results:
        art100 = (item.get("artworkUrl100") or "").strip()
        if not art100:
            continue
        image_url = art100.replace("100x100bb", "600x600bb")
        thumb_url = art100.replace("100x100bb", "300x300bb")
        if image_url in seen_urls:
            continue
        seen_urls.add(image_url)
        c_artist = (item.get("artistName") or artist).strip()
        c_album = (item.get("collectionName") or item.get("trackName") or album or title).strip()
        year = str(item.get("releaseDate") or "")[:4]
        candidates.append({
            "id": f"itunes:{item.get('collectionId') or item.get('trackId') or len(candidates)}",
            "source": "Open (iTunes)",
            "title": f"{c_artist} - {c_album}".strip(" -"),
            "artist": c_artist,
            "album": c_album,
            "year": year,
            "image_url": image_url,
            "thumb_url": thumb_url,
        })
    return candidates


def _deduplicate_candidates(items: list[dict], collapse_same_album: bool = False) -> list[dict]:
    """Remove duplicate URLs; when collapse_same_album is True and multiple sources return
    the exact same artist+album with only 1 album title overall, keep the highest-priority one
    unless genuinely different albums/editions were found."""
    by_url: list[dict] = []
    seen_urls: set[str] = set()
    for c in items:
        url = (c.get("image_url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        by_url.append(c)

    if not collapse_same_album or len(by_url) <= 1:
        return by_url

    # Group by normalized (artist, album)
    album_keys = set()
    for c in by_url:
        ak = re.sub(r"[^a-z0-9]+", "", (c.get("artist") or "").lower())
        al = re.sub(r"[^a-z0-9]+", "", (c.get("album") or c.get("title") or "").lower())
        album_keys.add((ak, al))

    # If multiple sources returned the exact same single (artist, album) and Discogs has a single match,
    # keep all distinct candidates when the user is searching interactively, or keep distinct covers.
    return by_url


def search_covers(
    artist: str,
    album: str,
    title: str,
    settings: dict,
    db=None,
    force_refresh: bool = False,
) -> list[dict]:
    """Search configured sources ('both', 'discogs', or 'open') for cover art candidates."""
    gen = settings.get("general", settings)
    source_mode = gen.get("coverSources", "both")
    token = gen.get("discogsToken", "")

    artist = (artist or "").strip()
    album = (album or "").strip()
    title = (title or "").strip()
    if not (artist or album or title):
        return []

    # Cache by (source_mode, artist, album) when album is known so all tracks in an album share 1 lookup
    cache_target = f"{artist.lower()}|{album.lower()}" if album else f"{artist.lower()}||{title.lower()}"
    query_key = f"{source_mode}|{bool(token)}|{cache_target}"

    if db is not None and not force_refresh:
        row = db.one("SELECT candidates FROM cover_cache WHERE query_key = ?", (query_key,))
        if row:
            try:
                cached = json.loads(row["candidates"])
                if isinstance(cached, list):
                    return cached
            except Exception:
                pass

    results: list[dict] = []
    if source_mode in ("both", "discogs"):
        results.extend(_search_discogs(artist, album, title, token=token))

    if source_mode in ("both", "open"):
        # Query open sources (MusicBrainz/CAA + iTunes fallback)
        mb = _search_musicbrainz_caa(artist, album, title)
        results.extend(mb)
        if not results:
            results.extend(_search_itunes(artist, album, title))

    deduped = _deduplicate_candidates(results)

    if db is not None:
        try:
            db.execute(
                "INSERT INTO cover_cache (query_key, candidates, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(query_key) DO UPDATE SET candidates = excluded.candidates, updated_at = excluded.updated_at",
                (query_key, json.dumps(deduped), time.time()),
            )
        except Exception:
            pass

    return deduped


def download_cover(url: str, token: str = "") -> tuple[bytes, str]:
    """Fetch cover image bytes from local://, data:, or http(s):// URL."""
    url = (url or "").strip()
    if not url:
        raise ValueError("missing image URL")
    if url.startswith("local://"):
        local_path = url[len("local://"):]
        if not os.path.isfile(local_path):
            raise ValueError("local cover file not found")
        with open(local_path, "rb") as fh:
            data = fh.read()
        return data, detect_mime(data)
    if url.startswith("data:"):
        head, _, b64 = url.partition(",")
        mime = "image/jpeg"
        if ";" in head and ":" in head:
            mime = head.split(":", 1)[1].split(";", 1)[0] or "image/jpeg"
        data = base64.b64decode(b64)
        return data, detect_mime(data, mime)
    if not url.startswith(("http://", "https://")):
        raise ValueError("unsupported cover URL scheme")

    headers = {"User-Agent": USER_AGENT}
    if "discogs.com" in url and token:
        headers["Authorization"] = f"Discogs token={token.strip()}"
    r = httpx.get(url, headers=headers, follow_redirects=True, timeout=15.0)
    if r.status_code != 200 or not r.content:
        raise ValueError(f"failed to download cover image (HTTP {r.status_code})")
    data = r.content
    mime = detect_mime(data, r.headers.get("content-type", "image/jpeg").split(";")[0].strip())
    return data, mime
