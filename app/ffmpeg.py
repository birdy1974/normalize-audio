"""Calls to ffmpeg (and the optional mp3gain / aacgain tools). Each call can be cancelled."""
from __future__ import annotations

import array
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .media import DEFAULT_KBPS, ENCODER, audio_family

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
_INPUT_END = re.compile(r"^(?:Stream mapping:|Output #0)", re.M)


def _parse_probe(err: str) -> Probe:
    head = _INPUT_END.split(err, maxsplit=1)[0]
    lines = head.splitlines()
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
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", head)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if not audio:
        raise ToolError("no audio stream found")
    return Probe(codec, kbps, fmt, dur, len(audio), len(video))


def probe(ffmpeg: str, path: str, cancel: Cancel = None) -> Probe:
    _, err = run_tool([ffmpeg, "-hide_banner", "-nostdin", "-i", path], cancel)
    return _parse_probe(err)


_I = re.compile(r"I:\s+(-?\d+(?:\.\d+)?|-inf) LUFS")
_TP = re.compile(r"True peak:\s+Peak:\s+(-?\d+(?:\.\d+)?|-inf) dBFS")


def measure_and_probe(ffmpeg: str, path: str, cancel: Cancel = None) -> tuple[float, float, Probe]:
    """Integrated loudness (LUFS, EBU R128), true peak (dBTP) and stream info in one ffmpeg pass.

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
    return lufs, tp, _parse_probe(err)


def measure(ffmpeg: str, path: str, cancel: Cancel = None) -> tuple[float, float]:
    """Integrated loudness (LUFS, EBU R128) and true peak (dBTP) of the first audio stream."""
    lufs, tp, _ = measure_and_probe(ffmpeg, path, cancel)
    return lufs, tp


SILENCE_RATE = 8000
SILENCE_WIN = 400  # 0.05 s (50 ms) at 8000 Hz


def detect_silent_sections(ffmpeg: str, path: str, *, lufs: float,
                           level_pct: float = 2.0, peak_db: float = -40.0,
                           min_duration: float = 5.0,
                           cancel: Cancel = None) -> list[tuple[float, float]]:
    """Find contiguous 'no sound' sections strictly longer than `min_duration` seconds.

    A 50 ms window counts as 'no sound' when its RMS level is at most `level_pct`% of the
    file's average sound level (`lufs`) and its highest sample peak is at most `peak_db` dBFS.
    """
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
           "-map", "0:a:0", "-ac", "1", "-ar", str(SILENCE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    avg_amp = 32768.0 * (10.0 ** (float(lufs) / 20.0))
    rms_limit = avg_amp * (max(0.0, float(level_pct)) / 100.0)
    peak_limit = 32768.0 * (10.0 ** (float(peak_db) / 20.0))
    dt = SILENCE_WIN / SILENCE_RATE
    rest = b""
    win_idx = 0
    run_start: float | None = None
    cuts: list[tuple[float, float]] = []
    try:
        while True:
            if cancel and cancel():
                proc.kill()
                proc.communicate()
                raise Cancelled()
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            data = rest + chunk
            usable = (len(data) // (SILENCE_WIN * 2)) * (SILENCE_WIN * 2)
            if not usable:
                rest = data
                continue
            rest = data[usable:]
            samples = array.array("h")
            samples.frombytes(data[:usable])
            if sys.byteorder != "little":
                samples.byteswap()
            for offset in range(0, len(samples), SILENCE_WIN):
                sl = samples[offset:offset + SILENCE_WIN]
                hi, lo = max(sl), min(sl)
                pk = hi if hi >= -lo else -lo
                if pk > peak_limit:
                    silent = False
                elif pk <= rms_limit:
                    silent = True
                else:
                    rms = math.sqrt(sum(x * x for x in sl) / SILENCE_WIN)
                    silent = rms <= rms_limit
                t0 = win_idx * dt
                win_idx += 1
                if silent:
                    if run_start is None:
                        run_start = t0
                elif run_start is not None:
                    t1 = t0
                    if t1 - run_start > min_duration:
                        cuts.append((round(run_start, 3), round(t1, 3)))
                    run_start = None
        if len(rest) >= 2:
            samples = array.array("h")
            samples.frombytes(rest[:(len(rest) // 2) * 2])
            if sys.byteorder != "little":
                samples.byteswap()
            hi, lo = max(samples), min(samples)
            pk = hi if hi >= -lo else -lo
            if pk > peak_limit:
                silent = False
            elif pk <= rms_limit:
                silent = True
            else:
                rms = math.sqrt(sum(x * x for x in samples) / len(samples))
                silent = rms <= rms_limit
            t0 = win_idx * dt
            t_end = t0 + len(samples) / SILENCE_RATE
            if silent:
                if run_start is None:
                    run_start = t0
                if t_end - run_start > min_duration:
                    cuts.append((round(run_start, 3), round(t_end, 3)))
                run_start = None
        if run_start is not None:
            t_end = win_idx * dt
            if t_end - run_start > min_duration:
                cuts.append((round(run_start, 3), round(t_end, 3)))
        _, err_bytes = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    if proc.returncode != 0:
        err = (err_bytes or b"").decode("utf-8", errors="replace")
        raise ToolError("silence analysis failed: " + _tail(err))
    return cuts


def analyze_audio(ffmpeg: str, path: str, *,
                  level_pct: float = 2.0, peak_db: float = -40.0,
                  min_duration: float = 5.0,
                  cancel: Cancel = None) -> tuple[float, float, Probe, list[tuple[float, float]]]:
    """Measure LUFS, true peak, stream info and silent sections in one single ffmpeg pass."""
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-i", path,
        "-map", "0:a:0",
        "-af", f"ebur128=peak=true,aresample={SILENCE_RATE},aformat=sample_fmts=s16:channel_layouts=mono",
        "-f", "s16le", "-",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    err_chunks: list[bytes] = []

    def _read_err() -> None:
        while True:
            b = proc.stderr.read(65536)
            if not b:
                break
            err_chunks.append(b)

    err_thread = threading.Thread(target=_read_err, daemon=True)
    err_thread.start()

    pks = array.array("f")
    rmss = array.array("f")
    rest = b""
    tail_dur = 0.0
    try:
        while True:
            if cancel and cancel():
                proc.kill()
                proc.communicate()
                raise Cancelled()
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            data = rest + chunk
            usable = (len(data) // (SILENCE_WIN * 2)) * (SILENCE_WIN * 2)
            if not usable:
                rest = data
                continue
            rest = data[usable:]
            samples = array.array("h")
            samples.frombytes(data[:usable])
            if sys.byteorder != "little":
                samples.byteswap()
            for offset in range(0, len(samples), SILENCE_WIN):
                sl = samples[offset:offset + SILENCE_WIN]
                hi, lo = max(sl), min(sl)
                pk = hi if hi >= -lo else -lo
                rms = math.sqrt(sum(x * x for x in sl) / SILENCE_WIN)
                pks.append(pk)
                rmss.append(rms)
        if len(rest) >= 2:
            samples = array.array("h")
            samples.frombytes(rest[:(len(rest) // 2) * 2])
            if sys.byteorder != "little":
                samples.byteswap()
            hi, lo = max(samples), min(samples)
            pk = hi if hi >= -lo else -lo
            rms = math.sqrt(sum(x * x for x in samples) / len(samples))
            pks.append(pk)
            rmss.append(rms)
            tail_dur = len(samples) / SILENCE_RATE
        proc.wait(timeout=10)
        err_thread.join(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    err = b"".join(err_chunks).decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise ToolError("analysis failed: " + _tail(err))
    i_vals = _I.findall(err)
    tp_vals = _TP.findall(err)
    if not i_vals or not tp_vals:
        raise ToolError("analysis gave no result")
    lufs = -70.0 if i_vals[-1] == "-inf" else float(i_vals[-1])
    tp = -70.0 if tp_vals[-1] == "-inf" else float(tp_vals[-1])
    probe = _parse_probe(err)
    if lufs <= -60 or not probe.duration:
        return lufs, tp, probe, []

    avg_amp = 32768.0 * (10.0 ** (float(lufs) / 20.0))
    rms_limit = avg_amp * (max(0.0, float(level_pct)) / 100.0)
    peak_limit = 32768.0 * (10.0 ** (float(peak_db) / 20.0))
    dt = SILENCE_WIN / SILENCE_RATE
    cuts: list[tuple[float, float]] = []
    run_start: float | None = None
    n_full = len(pks) - (1 if tail_dur > 0 else 0)
    for idx in range(n_full):
        pk = pks[idx]
        silent = False if pk > peak_limit else (True if pk <= rms_limit else rmss[idx] <= rms_limit)
        t0 = idx * dt
        if silent:
            if run_start is None:
                run_start = t0
        elif run_start is not None:
            if t0 - run_start > min_duration:
                cuts.append((round(run_start, 3), round(t0, 3)))
            run_start = None
    if tail_dur > 0:
        idx = n_full
        pk = pks[idx]
        silent = False if pk > peak_limit else (True if pk <= rms_limit else rmss[idx] <= rms_limit)
        t0 = idx * dt
        t_end = t0 + tail_dur
        if silent:
            if run_start is None:
                run_start = t0
            if t_end - run_start > min_duration:
                cuts.append((round(run_start, 3), round(t_end, 3)))
            run_start = None
    if run_start is not None:
        t_end = n_full * dt
        if t_end - run_start > min_duration:
            cuts.append((round(run_start, 3), round(t_end, 3)))
    return lufs, tp, probe, cuts


def kept_segments(duration: float, cuts: list[tuple[float, float]]) -> list[tuple[float, float, bool, bool]]:
    """Return [(start, end, cut_before, cut_after), ...] for the non-silent parts of a file."""
    kept: list[tuple[float, float, bool, bool]] = []
    cursor = 0.0
    for cs, ce in cuts:
        cs = max(cursor, min(duration, cs))
        ce = max(cs, min(duration, ce))
        if cs - cursor >= 0.05:
            kept.append((round(cursor, 3), round(cs, 3), cursor > 0.02, True))
        cursor = ce
    if duration - cursor >= 0.05:
        kept.append((round(cursor, 3), round(duration, 3), cursor > 0.02, False))
    return kept


def temp_path(path: str) -> str:
    """Hidden temporary name in the same folder (same file system, so rename is atomic)."""
    folder, name = os.path.split(path)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    return os.path.join(folder, f".{stem}.nrm-tmp{'.' + ext if ext else ''}")


def transcode_args(src: str, dst: str, *, ext: str, probe_: Probe, family: str, method: str,
                   gain: float, kbps: int | None, cuts: list[tuple[float, float]] | None = None,
                   fade_in: float = 0.0, fade_out: float = 0.0) -> list[str]:
    """ffmpeg command: audio gain (volume filter) and optional silence removal; video/subtitles copied."""
    kept = kept_segments(probe_.duration or 0.0, cuts) if cuts and probe_.duration else []
    if kept and cuts:
        parts: list[str] = []
        for idx, (s, e, cut_before, cut_after) in enumerate(kept):
            seg_len = max(0.01, e - s)
            chain = [f"atrim=start={s:.3f}:end={e:.3f}", "asetpts=PTS-STARTPTS"]
            fi = min(max(0.0, float(fade_in)), seg_len * 0.5 if cut_after and fade_out > 0 else seg_len) if cut_before else 0.0
            fo = min(max(0.0, float(fade_out)), seg_len * 0.5 if cut_before and fade_in > 0 else seg_len) if cut_after else 0.0
            if fi >= 0.01:
                chain.append(f"afade=t=in:st=0:d={fi:.3f}")
            if fo >= 0.01:
                chain.append(f"afade=t=out:st={max(0.0, seg_len - fo):.3f}:d={fo:.3f}")
            parts.append(f"[0:a:0]{','.join(chain)}[s{idx}]")
        if len(kept) == 1:
            label = "[s0]"
        else:
            inputs = "".join(f"[s{i}]" for i in range(len(kept)))
            parts.append(f"{inputs}concat=n={len(kept)}:v=0:a=1[cat]")
            label = "[cat]"
        if abs(gain) >= 0.05:
            parts.append(f"{label}volume={gain:.2f}dB[out]")
            label = "[out]"
        args = ["-filter_complex", ";".join(parts), "-map", label, "-map_metadata", "0", "-map_chapters", "-1"]
    else:
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
    if not (kept and cuts):
        args += ["-af", f"volume={gain:.2f}dB"]
    return ["-hide_banner", "-nostdin", "-y", "-v", "error", "-i", src, *args, dst]


def transcode(ffmpeg: str, src: str, dst: str, *, ext: str, probe_: Probe, family: str, method: str,
              gain: float, kbps: int | None, cancel: Cancel = None,
              cuts: list[tuple[float, float]] | None = None,
              fade_in: float = 0.0, fade_out: float = 0.0) -> None:
    cmd = [ffmpeg, *transcode_args(src, dst, ext=ext, probe_=probe_, family=family, method=method,
                                   gain=gain, kbps=kbps, cuts=cuts, fade_in=fade_in, fade_out=fade_out)]
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


def edit_args(src: str, dst: str, *, probe_: Probe, duration: float,
              cut: tuple[float, float] | None, fade_in: float, fade_out: float,
              new_duration: float, kbps: int | None) -> list[str]:
    """ffmpeg command for the editor: remove a part, then fade in and out; audio only."""
    parts: list[str] = []
    label = "[0:a:0]"
    if cut:
        start, end = cut
        if start <= 0.005:                     # only the tail is kept
            parts.append(f"[0:a:0]atrim=start={end:.3f},asetpts=PTS-STARTPTS[kept]")
        elif end >= duration - 0.005:          # only the head is kept
            parts.append(f"[0:a:0]atrim=end={start:.3f},asetpts=PTS-STARTPTS[kept]")
        else:
            parts.append(f"[0:a:0]atrim=end={start:.3f},asetpts=PTS-STARTPTS[head]")
            parts.append(f"[0:a:0]atrim=start={end:.3f},asetpts=PTS-STARTPTS[tail]")
            parts.append("[head][tail]concat=n=2:v=0:a=1[kept]")
        label = "[kept]"
    effects = []
    if fade_in > 0:
        effects.append(f"afade=t=in:st=0:d={fade_in:.3f}")
    if fade_out > 0:
        effects.append(f"afade=t=out:st={max(0.0, new_duration - fade_out):.3f}:d={fade_out:.3f}")
    if effects:
        parts.append(f"{label}{','.join(effects)}[out]")
        label = "[out]"
    args = ["-hide_banner", "-nostdin", "-y", "-v", "error", "-i", src,
            "-filter_complex", ";".join(parts), "-map", label,
            "-map_metadata", "0", "-map_chapters", "-1"]
    codec = (probe_.codec or "").lower()
    family = audio_family(codec)
    if family is None:
        raise ToolError(f"editing {codec or 'this format'} is not supported")
    if family == "lossless" or codec.startswith("pcm_"):
        args += ["-c:a", probe_.codec]
        if codec == "flac":
            args += ["-sample_fmt", "s32" if (probe_.sample_fmt or "").startswith("s32") else "s16"]
    else:
        bitrate = kbps or DEFAULT_KBPS.get(family, 192)
        args += ["-c:a", ENCODER[family], "-b:a", f"{bitrate}k"]
    args.append(dst)
    return args


def edit(ffmpeg: str, src: str, dst: str, *, probe_: Probe, duration: float,
         cut: tuple[float, float] | None, fade_in: float, fade_out: float,
         new_duration: float, kbps: int | None, cancel: Cancel = None) -> None:
    cmd = [ffmpeg, *edit_args(src, dst, probe_=probe_, duration=duration, cut=cut,
                              fade_in=fade_in, fade_out=fade_out, new_duration=new_duration, kbps=kbps)]
    rc, err = run_tool(cmd, cancel)
    if rc != 0:
        raise ToolError("editing failed: " + _tail(err))


def verify_edit(ffmpeg: str, path: str, source: Probe, expected_duration: float,
                cancel: Cancel = None) -> None:
    """The edited file must decode without errors and have the length the edit asks for."""
    rc, err = run_tool([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
                        "-map", "0:a", "-f", "null", "-"], cancel)
    if rc != 0 or err.strip():
        raise ToolError("verification failed: " + _tail(err))
    out = probe(ffmpeg, path, cancel)
    if out.audio_streams != 1:
        raise ToolError("verification failed: audio stream count changed")
    if audio_family((out.codec or "").lower()) != audio_family((source.codec or "").lower()):
        raise ToolError(f"verification failed: the codec changed from {source.codec} to {out.codec}")
    if out.duration is None or abs(out.duration - expected_duration) > 0.6:
        raise ToolError(f"verification failed: the new length is {out.duration or 0:.1f} s "
                        f"instead of {expected_duration:.1f} s")


def verify(ffmpeg: str, path: str, source: Probe, cancel: Cancel = None,
           expected_duration: float | None = None) -> None:
    """The new file must decode without errors and match the expected length and streams."""
    rc, err = run_tool([ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
                        "-map", "0:a", "-f", "null", "-"], cancel)
    if rc != 0 or err.strip():
        raise ToolError("verification failed: " + _tail(err))
    out = probe(ffmpeg, path, cancel)
    if expected_duration is None and out.audio_streams != source.audio_streams:
        raise ToolError("verification failed: audio stream count changed")
    if expected_duration is not None and out.audio_streams != 1:
        raise ToolError("verification failed: audio stream count changed")
    if source.video_streams and out.video_streams != source.video_streams:
        raise ToolError("verification failed: video stream lost")
    target_dur = expected_duration if expected_duration is not None else source.duration
    if target_dur and out.duration is not None and abs(out.duration - target_dur) > 0.6:
        raise ToolError("verification failed: duration changed")
