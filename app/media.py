"""Media types, file extensions and codec families."""
from __future__ import annotations

MEDIA = ("audio", "video")

EXTENSIONS = {
    "audio": ("mp3", "flac", "wav", "aif", "aiff", "m4a", "aac", "ogg", "oga", "opus", "wma"),
    "video": ("avi", "mkv", "mp4", "m4v", "mov", "wmv", "mpg", "mpeg", "webm"),
}

LABELS = {"audio": "Audio", "video": "Video"}

# Codec family of the audio stream, from the codec name ffmpeg reports.
LOSSY_FAMILIES = ("mp3", "aac", "vorbis", "opus", "wma", "ac3", "eac3")

# Encoder used when a lossy stream is re-encoded at (about) its own bitrate.
ENCODER = {
    "mp3": "libmp3lame",
    "aac": "aac",
    "vorbis": "libvorbis",
    "opus": "libopus",
    "wma": "wmav2",
    "ac3": "ac3",
    "eac3": "eac3",
}

DEFAULT_KBPS = {"mp3": 192, "aac": 160, "vorbis": 160, "opus": 128, "wma": 128, "ac3": 384, "eac3": 384}

# Containers in which the aacgain tool can work on AAC.
AACGAIN_EXTS = ("m4a", "mp4", "m4v")


def audio_family(codec: str | None) -> str | None:
    """Return 'lossless', a lossy family name, or None when the codec is not supported."""
    if not codec:
        return None
    c = codec.lower()
    if c in ("flac", "alac") or c.startswith("pcm_") or c.startswith("wavpack"):
        return "lossless"
    if c == "mp3":
        return "mp3"
    if c == "aac":
        return "aac"
    if c == "vorbis":
        return "vorbis"
    if c == "opus":
        return "opus"
    if c.startswith("wma"):
        return "wma"
    if c in ("ac3", "eac3"):
        return c
    return None
