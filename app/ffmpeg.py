"""Calls to ffmpeg (and the optional mp3gain / aacgain tools). Each call can be cancelled."""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

from .media import DEFAULT_KBPS, ENCODER

Cancel = Callable[[], bool] | None


class ToolError(RuntimeError):
    """A tool ran but failed; the message is shown to the user."""


class Cancelled(RuntimeError):
    """The run was stopped or paused while the tool was working."""


def run_tool(cmd: list[str], cancel: Cancel = None, timeout: float = 4 * 3600) -> tuple[int, str]:
    """Run a command, return (exit code, stderr). Kills it when cancel() becomes true."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True, errors="replace")
    started = time.monotonic()
    while True:
        try:
            _, err = proc.communicate(timeout=0.5)
            return proc.returncode, err or ""
        except subprocess.TimeoutExpired:
            if cancel and cancel():
                proc.kill()
                proc.communicate()
                raise Cancelled()
            if time.monotonic() - started > timeout:
                proc.kill()
                proc.communicate()
                raise ToolError("timed out")


def _tail(text: str, n: int = 3) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return " | ".join(lines[-n:])[-300:] or "no details"


@dataclass(frozen=True)
class Probe:
    codec: str | None          # codec of the first audio stream, e.g. "mp3", "pcm_s16le"
    kbps: int | None           # bitrate of the first audio stream, if reported
    sample_fmt: str | None     # e.g. "s16", "s32", "fltp"
    duration: float | None     # seconds
    audio_streams: int
    video_streams: int         # real video streams (cover art not counted)


_AUDIO = re.compile(r"Stream #\d+:\d+.*?: Audio: ([A-Za-z0-9_]+)")
_FMT = re.compile(r"^(u8|s16|s32|s64|flt|fltp|dbl|dblp|s16p|s32p|u8p)(\s|$)")


def probe(ffmpeg: str, path: str, cancel: Cancel = None) -> Probe:
    _, err = run_tool([ffmpeg, "-hide_banner", "-nostdin", "-i", path], cancel)
    lines = err.splitlines()
    audio = [ln for ln in lines if re.search(r"Stream #\d+:\d+.*: Audio:", ln)]
    video = [ln for ln in lines if re.search(r"Stream #\d+:\d+.*: Video:", ln) and "attached pic" not in ln]
    codec = kbps = fmt = None
    if audio:
        first = audio[0]
        codec = _AUDIO.search(first).group(1)
        m = re.search(r"(\d+) kb/s", first)
        kbps = int(m.group(1)) if m else None
        fmt = next((t.strip() for t in first.split(", ") if _FMT.match(t.strip())), None)
        fmt = _FMT.match(fmt).group(1) if fmt else None
    dur = None
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if not audio:
        raise ToolError("no audio stream found")
    return Probe(codec, kbps, fmt, dur, len(audio), len(video))


_I = re.compile(r"I:\s+(-?\d+(?:\.\d+)?|-inf) LUFS")
_TP = re.compile(r"True peak:\s+Peak:\s+(-?\d+(?:\.\d+)?|-inf) dBFS")


def measure(ffmpeg: str, path: str, cancel: Cancel = None) -> tuple[float, float]:
    """Integrated loudness (LUFS, EBU R128) and true peak (dBTP) of the first audio stream.

    Silent files return (-70, -70). The file itself is not changed.
    """
    rc, err = run_tool([ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-i", path,
                        "-map", "0:a:0", "-af", "ebur128=peak=true", "-f", "null", "-"], cancel)
    if rc != 0:
        raise ToolError("analysis failed: " + _tail(err))
    i_vals = _I.findall(err)
    tp_vals = _TP.findall(err)
    if not i_vals or not tp_vals:
        raise ToolError("analysis gave no result")
    lufs = -70.0 if i_vals[-1] == "-inf" else float(i_vals[-1])
    tp = -70.0 if tp_vals[-1] == "-inf" else float(tp_vals[-1])
    return lufs, tp


def temp_path(path: str) -> str:
    """Hidden temporary name in the same folder (same file system, so rename is atomic)."""
    folder, name = os.path.split(path)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    return os.path.join(folder, f".{stem}.nrm-tmp{'.' + ext if ext else ''}")


def transcode_args(src: str, dst: str, *, ext: str, probe_: Probe, family: str, method: str,
                   gain: float, kbps: int | None) -> list[str]:
    """ffmpeg command: audio gain (volume filter), video and subtitle streams are copied."""
    args = ["-map", "0:a", "-map", "0:v?", "-c:v", "copy", "-map_metadata", "0"]
    if ext == "mkv":
        args += ["-map", "0:s?", "-c:s", "copy"]
    if method == "volume":
        codec = probe_.codec
        args += ["-c:a", codec]
        if codec == "flac":
            args += ["-sample_fmt", "s32" if (probe_.sample_fmt or "").startswith("s32") else "s16"]
    else:
        bitrate = kbps or DEFAULT_KBPS.get(family, 192)
        args += ["-c:a", ENCODER[family], "-b:a", f"{bitrate}k"]
    args += ["-af", f"volume={gain:.2f}dB"]
    return ["-hide_banner", "-nostdin", "-y", "-v", "error", "-i", src, *args, dst]


def transcode(ffmpeg: str, src: str, dst: str, *, ext: str, probe_: Probe, family: str, method: str,
              gain: float, kbps: int | None, cancel: Cancel = None) -> None:
    cmd = [ffmpeg, *transcode_args(src, dst, ext=ext, probe_=probe_, family=family, method=method,
                                   gain=gain, kbps=kbps)]
    rc, err = run_tool(cmd, cancel)
    if rc != 0:
        raise ToolError("encoding failed: " + _tail(err))


def gain_tool(binary: str, path: str, steps: int, cancel: Cancel = None) -> None:
    """mp3gain / aacgain: change the global gain field in steps of 1.5 dB (lossless).

    -p keeps the original time stamp of the file.
    """
    rc, err = run_tool([binary, "-q", "-p", "-g", str(steps), path], cancel)
    if rc != 0:
        raise ToolError(f"{os.path.basename(binary)} failed: " + _tail(err))


def verify(ffmpeg: str, path: str, source: Probe, cancel: Cancel = None) -> None:
    """The new file must decode without errors and match the original length and streams."""
    rc, err = run_tool([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
                        "-map", "0:a", "-f", "null", "-"], cancel)
    if rc != 0 or err.strip():
        raise ToolError("verification failed: " + _tail(err))
    out = probe(ffmpeg, path, cancel)
    if out.audio_streams != source.audio_streams:
        raise ToolError("verification failed: audio stream count changed")
    if source.video_streams and out.video_streams != source.video_streams:
        raise ToolError("verification failed: video stream lost")
    if source.duration and out.duration is not None and abs(out.duration - source.duration) > 0.5:
        raise ToolError("verification failed: duration changed")
