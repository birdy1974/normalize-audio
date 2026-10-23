"""Pure decision logic: what to do with one file. No I/O, so it is easy to test."""
from __future__ import annotations

from dataclasses import dataclass

from .media import AACGAIN_EXTS

STEP_DB = 1.5          # gain step of mp3gain / aacgain
NO_CHANGE_DB = 0.5     # smaller changes are not worth a write


@dataclass(frozen=True)
class Decision:
    action: str                      # ok | apply | park | skip | error
    method: str | None = None        # volume | reencode | mp3gain | aacgain
    gain: float = 0.0                # dB, after the true-peak limit
    reason: str | None = None        # large_change | lossy_no_gain_tool | lossy_skipped | unsupported
    capped: bool = False             # gain was reduced to keep the true peak under the ceiling


def limit_gain(gain_raw: float, true_peak: float | None, ceiling: float) -> tuple[float, bool]:
    """Positive gain may not push the true peak above the ceiling."""
    if gain_raw <= 0 or true_peak is None:
        return round(gain_raw, 2), False
    room = ceiling - true_peak
    if gain_raw <= room:
        return round(gain_raw, 2), False
    return round(max(0.0, room), 2), True


def decide(*, media: str, ext: str, family: str | None, measured: float, true_peak: float | None,
           target: float, ceiling: float, threshold: float, override: bool,
           strategy: str, tools: set[str], hysteresis: float = NO_CHANGE_DB) -> Decision:
    if family is None:
        return Decision("error", reason="unsupported")
    raw_gain = round(target - measured, 2)
    gain, capped = limit_gain(raw_gain, true_peak, ceiling)
    if abs(raw_gain) <= hysteresis or abs(gain) <= hysteresis or abs(gain) < 0.05:
        return Decision("ok", None, gain, None, capped)

    if family == "lossless":
        method = "volume"                       # same codec, gain applied to the samples
    elif media == "video":
        method = "reencode"                     # gain-only is impossible inside a video container
    elif strategy == "skip":
        return Decision("skip", None, gain, "lossy_skipped", capped)
    elif strategy == "reencode":
        method = "reencode"
    else:                                       # strategy "gain": lossless gain tool if installed
        tool = {"mp3": "mp3gain", "aac": "aacgain"}.get(family)
        if tool == "aacgain" and ext not in AACGAIN_EXTS:
            tool = None
        if tool and tool in tools:
            method = tool
        else:
            return Decision("park", "reencode", gain, "lossy_no_gain_tool", capped)

    if abs(gain) > threshold and not override:
        return Decision("park", method, gain, "large_change", capped)
    return Decision("apply", method, gain, None, capped)
