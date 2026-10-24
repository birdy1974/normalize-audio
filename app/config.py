"""Runtime configuration, read from environment variables (set in docker-compose.yml)."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass


def _int(env, name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class Config:
    audio_root: str          # base location for audio (mounted from the NAS)
    video_root: str          # base location for video
    config_dir: str          # persistent state: database, backups, tokens
    ffmpeg: str              # ffmpeg binary (also used for loudness analysis)
    mp3gain: str             # lossless MP3 gain tool
    aacgain: str             # lossless AAC/M4A gain tool
    concurrency: int         # files processed at the same time (1 on the DS918+)
    tz: str                  # time zone for schedule windows
    port: int                # port the web UI listens on (used in the HA YAML)
    static_dir: str          # web UI files

    @property
    def db_path(self) -> str:
        return os.path.join(self.config_dir, "normalizer.db")

    @property
    def backup_dir(self) -> str:
        return os.path.join(self.config_dir, "backup")

    def root(self, media: str) -> str:
        return self.audio_root if media == "audio" else self.video_root


def _default_config_dir(here: str) -> str:
    if os.path.isdir("/config"):
        return "/config"
    return os.path.join(here, "config")


def load_config(env=None) -> Config:
    env = os.environ if env is None else env
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return Config(
        audio_root=env.get("AUDIO_ROOT", "/data/audio"),
        video_root=env.get("VIDEO_ROOT", "/data/video"),
        config_dir=env.get("CONFIG_DIR") or _default_config_dir(here),
        ffmpeg=env.get("FFMPEG_BIN", "ffmpeg"),
        mp3gain=env.get("MP3GAIN_BIN", "mp3gain"),
        aacgain=env.get("AACGAIN_BIN", "aacgain"),
        concurrency=_int(env, "CONCURRENCY", 1, 1, 2),
        tz=env.get("TZ", "Europe/Berlin"),
        port=_int(env, "PORT", 8080, 1, 65535),
        static_dir=env.get("STATIC_DIR", os.path.join(here, "static")),
    )


def tool_path(binary: str) -> str | None:
    """Full path of a tool, or None when it is not installed."""
    if os.path.sep in binary and os.path.exists(binary):
        return binary
    return shutil.which(binary)
