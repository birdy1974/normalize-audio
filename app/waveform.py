"""Waveform peaks for the timeline in the audio editor.

The first audio stream is decoded with ffmpeg to 8 kHz mono PCM and reduced to one
minimum/maximum pair per bucket. That is enough for a peak per pixel, also when the
user zooms in, without keeping the decoded audio in memory. A small cache keeps a
re-opened editor and repeated zoom steps fast; the file's mtime and size are part of
the key, so an edited or replaced file is decoded again.

Only the visible window is decoded when a window is asked for (`start` + `duration`),
so zooming into a long file stays quick: ffmpeg seeks to the window.
"""
from __future__ import annotations

import array
import os
import subprocess
import sys
import threading
from collections import OrderedDict

from . import ffmpeg as ff

RATE = 8000                    # samples per second used for the picture
MAX_BUCKETS = 8000
CACHE_ENTRIES = 48
DECODE_TIMEOUT = 300           # seconds before a stuck ffmpeg is killed

_lock = threading.Lock()
_cache: "OrderedDict[tuple, dict]" = OrderedDict()
_durations: "OrderedDict[tuple, float]" = OrderedDict()


def _file_duration(ffmpeg_bin: str, path: str, st: os.stat_result) -> float:
    key = (path, st.st_mtime_ns, st.st_size, st.st_ctime_ns)
    with _lock:
        if key in _durations:
            _durations.move_to_end(key)
            return _durations[key]
    probe = ff.probe(ffmpeg_bin, path)
    if not probe.duration or probe.duration <= 0:
        raise ValueError("the length of this file is unknown")
    with _lock:
        _durations[key] = probe.duration
        while len(_durations) > 256:
            _durations.popitem(last=False)
    return probe.duration


def info(ffmpeg_bin: str, path: str) -> dict:
    """Length and codec of a file, without decoding it (fast: ffmpeg only reads the header)."""
    st = os.stat(path)
    duration = _file_duration(ffmpeg_bin, path, st)
    probe = ff.probe(ffmpeg_bin, path)
    return {"duration": round(duration, 3), "codec": probe.codec, "size": st.st_size}


def peaks(ffmpeg_bin: str, path: str, start: float = 0.0, duration: float | None = None,
          buckets: int = 1200) -> dict:
    """Peaks of a window of the file: `buckets` (minimum, maximum) pairs in full scale."""
    st = os.stat(path)
    file_duration = _file_duration(ffmpeg_bin, path, st)
    buckets = max(8, min(MAX_BUCKETS, int(buckets)))
    start = max(0.0, min(float(start or 0.0), file_duration))
    if duration is None or float(duration) <= 0:
        window = file_duration - start
    else:
        window = min(float(duration), file_duration - start)
    window = max(window, 0.02)                     # also very short files get a picture

    key = (path, st.st_mtime_ns, st.st_size, st.st_ctime_ns, round(start, 3), round(window, 3), buckets)
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]

    lo, hi = _decode(ffmpeg_bin, path, start, window, buckets)
    out = {
        "start": round(start, 3),
        "duration": round(window, 3),
        "file_duration": round(file_duration, 3),
        "buckets": buckets,
        "min": lo,
        "max": hi,
    }
    with _lock:
        _cache[key] = out
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)
    return out


def _decode(ffmpeg_bin: str, path: str, start: float, window: float,
            buckets: int) -> tuple[list[float], list[float]]:
    cmd = [ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error",
           "-ss", f"{start:.3f}", "-i", path, "-t", f"{window:.3f}",
           "-map", "0:a:0", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    expected = max(1, int(round(window * RATE)))
    lo = [0] * buckets
    hi = [0] * buckets
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    killed = threading.Event()

    def stop() -> None:
        killed.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(DECODE_TIMEOUT, stop)
    watchdog.daemon = True
    watchdog.start()
    index = 0
    rest = b""
    try:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            data = rest + chunk
            usable = len(data) - (len(data) % 2)
            rest = data[usable:]
            if not usable:
                continue
            samples = array.array("h")
            samples.frombytes(data[:usable])
            if sys.byteorder == "big":             # ffmpeg writes little-endian
                samples.byteswap()
            pos = 0
            while pos < len(samples) and index < expected:
                bucket = index * buckets // expected
                if bucket >= buckets:
                    bucket = buckets - 1
                end = min(expected * (bucket + 1) // buckets, expected)
                take = min(len(samples) - pos, max(1, end - index))
                part = samples[pos:pos + take]
                low, high = min(part), max(part)
                if low < lo[bucket]:
                    lo[bucket] = low
                if high > hi[bucket]:
                    hi[bucket] = high
                pos += take
                index += take
        rc = proc.wait()
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
    if killed.is_set():
        raise ff.ToolError("reading the levels took too long")
    if rc != 0:
        raise ff.ToolError("could not read the audio: " + (err.strip()[-200:] or "no details"))
    scale = 1.0 / 32768.0
    return ([round(v * scale, 4) for v in lo], [round(v * scale, 4) for v in hi])
