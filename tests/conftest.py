"""Shared test setup: real ffmpeg, temporary base locations and a fresh app per test."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _ffmpeg_path() -> str:
    env = os.environ.get("FFMPEG_BIN")
    if env:
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # the imageio-ffmpeg wheel ships a static ffmpeg binary (used for local testing only)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pytest.skip("ffmpeg not available")


FFMPEG = _ffmpeg_path()


def make(path: str, *args: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", *args, path], check=True)
    return path


def tone(path: str, level_db: float, seconds: float = 3.0, codec_args=("-c:a", "libmp3lame", "-b:a", "192k")) -> str:
    """A 440 Hz tone at the given level (relative to the reference tone)."""
    return make(path, "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-af", f"volume={level_db}dB", *codec_args)


@pytest.fixture
def ffmpeg_bin() -> str:
    return FFMPEG


@pytest.fixture
def cfg(tmp_path):
    from app.config import Config
    audio = tmp_path / "audio"
    video = tmp_path / "video"
    audio.mkdir()
    video.mkdir()
    return Config(
        audio_root=str(audio), video_root=str(video), config_dir=str(tmp_path / "config"),
        ffmpeg=FFMPEG, mp3gain="/nonexistent/mp3gain", aacgain="/nonexistent/aacgain",
        concurrency=1, tz="Europe/Berlin", port=8080, static_dir=os.path.join(ROOT, "static"),
    )


@pytest.fixture
def app_engine(cfg):
    from app.main import create_app
    app = create_app(cfg, start_threads=False)
    return app, app.state.engine, app.state.db
