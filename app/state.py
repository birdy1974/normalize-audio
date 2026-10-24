"""Settings, schedule, run state and Home Assistant configuration (stored as JSON in the database)."""
from __future__ import annotations

import copy
import secrets
import threading

from .db import Database

DEFAULT_SETTINGS = {
    # mode: what the app does on its own
    #   off   nothing is scanned or normalized (manual reads still work)
    #   bulk  only a run started by the user checks and normalizes enabled folders; no periodic scan
    #   force like bulk, but a started run checks and normalizes all files in enabled folders again (even if already checked)
    #   auto  new and changed files are found every "scanEvery" minutes and normalized by themselves
    "general": {
        "dryRun": False, "preserveMtime": True, "mode": "bulk",
        "trimSilence": False, "silenceLevelPct": 2.0, "silencePeakDb": -40.0,
        "silenceMinDuration": 5.0, "silenceFadeOut": 0.5, "silenceFadeIn": 0.5,
        "coverAuto": False, "coverSources": "both", "discogsToken": "", "useLocalCover": True,
    },
    "audio": {
        "target": -16.0, "truePeak": -1.0, "hysteresis": 0.5, "threshold": 6.0, "override": False,
        "lossy": "gain", "writeMode": "inplace", "outputDir": "/data/normalized-audio",
    },
    "video": {
        "target": -16.0, "truePeak": -1.0, "hysteresis": 0.5, "threshold": 6.0, "override": False,
        "writeMode": "inplace", "outputDir": "/data/normalized-video",
    },
}

RANGES = {
    "target": (-30.0, -10.0), "truePeak": (-6.0, -1.0), "hysteresis": (0.0, 6.0), "threshold": (1.0, 12.0),
    "silenceLevelPct": (0.1, 50.0), "silencePeakDb": (-80.0, -10.0),
    "silenceMinDuration": (0.5, 600.0), "silenceFadeOut": (0.0, 30.0), "silenceFadeIn": (0.0, 30.0),
}
MODES = ("off", "bulk", "force", "auto")
LOSSY_MODES = ("gain", "reencode", "skip")
WRITE_MODES = ("inplace", "backup", "output")
ON_CLOSE = ("finish", "pause")
COVER_SOURCES = ("both", "discogs", "open")

DEFAULT_RUN = {"mode": "stopped", "paused": False, "types": {"audio": True, "video": True},
               "lastFinished": None, "lastScan": None}

DEFAULT_SENSORS = [
    {"path": "status", "name": "Status", "unit": "", "deviceClass": "", "icon": "mdi:waveform", "enabled": True, "group": "Overall"},
    {"path": "mode", "name": "Mode", "unit": "", "deviceClass": "", "icon": "mdi:cog-sync-outline", "enabled": True, "group": "Overall"},
    {"path": "last_scan", "name": "Last file scan", "unit": "", "deviceClass": "timestamp", "icon": "mdi:folder-search-outline", "enabled": False, "group": "Overall"},
    {"path": "next_scan", "name": "Next file scan", "unit": "", "deviceClass": "timestamp", "icon": "mdi:magnify-scan", "enabled": False, "group": "Overall"},
    {"path": "progress_percent", "name": "Progress", "unit": "%", "deviceClass": "", "icon": "mdi:progress-check", "enabled": True, "group": "Overall"},
    {"path": "errors", "name": "Errors", "unit": "errors", "deviceClass": "", "icon": "mdi:alert-circle", "enabled": True, "group": "Overall"},
    {"path": "current_file", "name": "Current file", "unit": "", "deviceClass": "", "icon": "mdi:file-music-outline", "enabled": False, "group": "Overall"},
    {"path": "last_run_finished", "name": "Last run finished", "unit": "", "deviceClass": "timestamp", "icon": "mdi:history", "enabled": False, "group": "Overall"},
]
for _t, _label in (("audio", "Audio"), ("video", "Video")):
    DEFAULT_SENSORS += [
        {"path": f"{_t}.directories_enabled", "name": f"{_label} folders enabled", "unit": "folders", "deviceClass": "", "icon": "mdi:folder-multiple", "enabled": True, "group": _label},
        {"path": f"{_t}.files_total", "name": f"{_label} files in scope", "unit": "files", "deviceClass": "", "icon": "mdi:file-multiple", "enabled": True, "group": _label},
        {"path": f"{_t}.files_pending", "name": f"{_label} files to do", "unit": "files", "deviceClass": "", "icon": "mdi:file-clock", "enabled": True, "group": _label},
        {"path": f"{_t}.files_checked", "name": f"{_label} files checked", "unit": "files", "deviceClass": "", "icon": "mdi:file-check", "enabled": True, "group": _label},
        {"path": f"{_t}.files_changed", "name": f"{_label} files changed", "unit": "files", "deviceClass": "", "icon": "mdi:volume-plus", "enabled": True, "group": _label},
        {"path": f"{_t}.files_parked", "name": f"{_label} files parked", "unit": "files", "deviceClass": "", "icon": "mdi:file-alert", "enabled": True, "group": _label},
        {"path": f"{_t}.target_lufs", "name": f"{_label} target loudness", "unit": "LUFS", "deviceClass": "", "icon": "mdi:tune-vertical", "enabled": False, "group": _label},
        {"path": f"{_t}.next_schedule_change", "name": f"{_label} next schedule change", "unit": "", "deviceClass": "timestamp", "icon": "mdi:calendar-clock", "enabled": False, "group": _label},
    ]


def _merge(defaults: dict, stored: dict) -> dict:
    out = copy.deepcopy(defaults)
    for scope, values in stored.items():
        if scope in out and isinstance(values, dict):
            out[scope].update(values)
    return out


# ---------------------------------------------------------------- settings

def get_settings(db: Database) -> dict:
    return _merge(DEFAULT_SETTINGS, db.get_json("settings", {}))


def _validate(key: str, value):
    if key in RANGES:
        lo, hi = RANGES[key]
        number = float(value)
        if not lo <= number <= hi:
            raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
        return round(number, 1)
    if key in ("override", "dryRun", "preserveMtime", "trimSilence", "coverAuto", "useLocalCover"):
        return bool(value)
    if key == "coverSources":
        if value not in COVER_SOURCES:
            raise ValueError("coverSources must be both, discogs or open")
        return value
    if key == "discogsToken":
        return str(value or "").strip()
    if key == "mode":
        if value not in MODES:
            raise ValueError("mode must be off, bulk, force or auto")
        return value
    if key == "lossy":
        if value not in LOSSY_MODES:
            raise ValueError("lossy must be gain, reencode or skip")
        return value
    if key == "writeMode":
        if value not in WRITE_MODES:
            raise ValueError("writeMode must be inplace, backup or output")
        return value
    if key == "outputDir":
        text = str(value).strip()
        if not text.startswith("/"):
            raise ValueError("outputDir must be an absolute path inside the container, e.g. /data/normalized-audio")
        return text
    raise ValueError(f"unknown setting {key}")


def put_settings(db: Database, patch: dict) -> dict:
    current = get_settings(db)
    for scope, values in patch.items():
        if scope not in current or not isinstance(values, dict):
            raise ValueError(f"unknown settings group {scope}")
        for key, value in values.items():
            if key not in current[scope]:
                raise ValueError(f"unknown setting {scope}.{key}")
            current[scope][key] = _validate(key, value)
    db.set_json("settings", current)
    return current


# ---------------------------------------------------------------- schedule

def default_schedule() -> dict:
    def grid(fn):
        return [[bool(fn(d, h)) for h in range(24)] for d in range(7)]

    return {
        # audio: nights (22:00-06:00) and weekends; video: weekends (heavier work)
        "audio": grid(lambda d, h: h >= 22 or h < 6 or d >= 5),
        "video": grid(lambda d, h: d >= 5),
        "scanEvery": 15,
        "onClose": "finish",
    }


def get_schedule(db: Database) -> dict:
    return _merge_schedule(db.get_json("schedule", {}))


def _merge_schedule(stored: dict) -> dict:
    out = default_schedule()
    out.update({k: v for k, v in stored.items() if k in out})
    return out


def _check_grid(value) -> list:
    if not (isinstance(value, list) and len(value) == 7 and all(isinstance(r, list) and len(r) == 24 for r in value)):
        raise ValueError("a schedule is 7 days x 24 hours")
    return [[bool(x) for x in row] for row in value]


def put_schedule(db: Database, patch: dict) -> dict:
    current = get_schedule(db)
    for key in ("audio", "video"):
        if key in patch:
            current[key] = _check_grid(patch[key])
    if "scanEvery" in patch:
        current["scanEvery"] = max(1, min(1440, int(patch["scanEvery"])))
    if "onClose" in patch:
        if patch["onClose"] not in ON_CLOSE:
            raise ValueError("onClose must be finish or pause")
        current["onClose"] = patch["onClose"]
    db.set_json("schedule", current)
    return current


# ---------------------------------------------------------------- run state

def get_run(db: Database) -> dict:
    stored = db.get_json("run", {})
    out = copy.deepcopy(DEFAULT_RUN)
    out.update({k: v for k, v in stored.items() if k in out})
    out["types"] = {**DEFAULT_RUN["types"], **stored.get("types", {})}
    return out


_run_lock = threading.Lock()      # the scheduler, the scanner and the API all update the run state


def set_run(db: Database, **changes) -> dict:
    with _run_lock:                # read-merge-write, so two threads cannot drop each other's change
        run = get_run(db)
        run.update(changes)
        db.set_json("run", run)
        return run


# ---------------------------------------------------------------- Home Assistant

def get_ha(db: Database) -> dict:
    stored = db.get_json("ha", {})
    out = {
        "url": "http://homeassistant.local:8123",
        "token": "",                       # Home Assistant long-lived access token (connection test)
        "endpointToken": "",               # token the HA REST sensor sends to this app
        "sensors": copy.deepcopy(DEFAULT_SENSORS),
    }
    out.update({k: v for k, v in stored.items() if k in out})
    # keep the sensor list in the default order; the user can change the enabled flag and the name
    saved = {s.get("path"): s for s in stored.get("sensors", []) if isinstance(s, dict)}
    out["sensors"] = []
    for default in DEFAULT_SENSORS:
        item = saved.get(default["path"], {})
        name = item.get("name")
        enabled = item.get("enabled")
        out["sensors"].append(dict(
            default,
            name=name if isinstance(name, str) and name else default["name"],
            default_name=default["name"],
            enabled=bool(enabled) if enabled is not None else default["enabled"],
        ))
    if not out["endpointToken"]:
        out["endpointToken"] = secrets.token_urlsafe(32)
        db.set_json("ha", {**stored, "endpointToken": out["endpointToken"]})
    return out


MAX_SENSOR_NAME = 80


def clean_sensor_name(value) -> str:
    """A sensor name as Home Assistant shows it: one line, no control characters, at most 80 characters."""
    text = " ".join(str(value or "").split())
    if not text:
        raise ValueError("a sensor name cannot be empty")
    if len(text) > MAX_SENSOR_NAME:
        raise ValueError(f"a sensor name can have at most {MAX_SENSOR_NAME} characters")
    if not text.isprintable():
        raise ValueError("a sensor name can only contain printable characters")
    return text


def save_ha(db: Database, patch: dict) -> dict:
    current = get_ha(db)
    if "url" in patch:
        url = str(patch["url"]).strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            raise ValueError("the Home Assistant URL must start with http:// or https://")
        current["url"] = url
    if "token" in patch and patch["token"] is not None:
        current["token"] = str(patch["token"]).strip()
    if "sensors" in patch:
        known = {d["path"] for d in DEFAULT_SENSORS}
        by_path = {s["path"]: s for s in current["sensors"]}
        for item in patch["sensors"]:
            path = item.get("path")
            if path not in known:
                continue                       # unknown sensors are ignored
            if "enabled" in item:
                by_path[path]["enabled"] = bool(item["enabled"])
            if "name" in item:
                by_path[path]["name"] = clean_sensor_name(item["name"])
        current["sensors"] = list(by_path.values())
    db.set_json("ha", {k: current[k] for k in ("url", "token", "endpointToken", "sensors")})
    return get_ha(db)


def rotate_endpoint_token(db: Database) -> dict:
    current = get_ha(db)
    current["endpointToken"] = secrets.token_urlsafe(32)
    db.set_json("ha", {k: current[k] for k in ("url", "token", "endpointToken", "sensors")})
    return get_ha(db)


# ---------------------------------------------------------------- Metadata check options

METADATA_FIELDS = ("artist", "title", "album", "album_artist", "year", "track", "genre", "cover")
METADATA_SOURCES = ("discogs", "musicbrainz", "open")

DEFAULT_METADATA_OPTIONS = {
    "fields": {f: True for f in METADATA_FIELDS},
    "checkPath": True,
    "searchFrom": "auto",
    "sources": {s: True for s in METADATA_SOURCES},
}


def _norm_search_from(val) -> str:
    s = str(val or "").strip().lower()
    if s in ("file", "path", "filename", "folder"):
        return "file"
    if s in ("metadata", "meta", "tags", "tag", "current"):
        return "metadata"
    return "auto"


def _normalize_meta_options(raw: dict) -> dict:
    fields_map = {f: True for f in METADATA_FIELDS}
    raw_fields = raw.get("fields")
    if isinstance(raw_fields, dict):
        for f in METADATA_FIELDS:
            if f in raw_fields:
                fields_map[f] = bool(raw_fields[f])
    elif isinstance(raw_fields, (list, tuple, set)):
        chosen = {str(x) for x in raw_fields if str(x) in METADATA_FIELDS}
        fields_map = {f: (f in chosen) for f in METADATA_FIELDS}

    check_path = True
    if "check_path_vs_tags" in raw and raw["check_path_vs_tags"] is not None:
        check_path = bool(raw["check_path_vs_tags"])
    elif "checkPath" in raw and raw["checkPath"] is not None:
        check_path = bool(raw["checkPath"])

    sources_map = {s: True for s in METADATA_SOURCES}
    raw_sources = raw.get("sources")
    if isinstance(raw_sources, dict):
        for s in METADATA_SOURCES:
            if s in raw_sources:
                sources_map[s] = bool(raw_sources[s])
    elif isinstance(raw_sources, (list, tuple, set)):
        chosen_s = {str(x) for x in raw_sources if str(x) in METADATA_SOURCES}
        sources_map = {s: (s in chosen_s) for s in METADATA_SOURCES}

    search_from = _norm_search_from(raw.get("search_from") if "search_from" in raw else raw.get("searchFrom"))

    return {
        "fields": [f for f in METADATA_FIELDS if fields_map.get(f, True)],
        "fields_map": fields_map,
        "check_path_vs_tags": check_path,
        "checkPath": check_path,
        "search_from": search_from,
        "searchFrom": search_from,
        "sources": [s for s in METADATA_SOURCES if sources_map.get(s, True)],
        "sources_map": sources_map,
        "visible_fields": _norm_vis_fields(raw.get("visible_fields")),
        "only_diff_rows": bool(raw.get("only_diff_rows", False)),
        "col_widths": _norm_col_widths(raw.get("col_widths")),
    }


def _norm_vis_fields(val) -> list[str]:
    if isinstance(val, (list, tuple, set)):
        picked = [f for f in METADATA_FIELDS if f in {str(x) for x in val}]
        if picked:
            return picked
    return list(METADATA_FIELDS)


def _norm_col_widths(val) -> dict[str, int]:
    out: dict[str, int] = {}
    if isinstance(val, dict):
        for k, v in val.items():
            if k in ("pick", "file", "path", "field", "current", "discogs", "musicbrainz", "open", "custom"):
                try:
                    n = int(round(float(v)))
                    if 32 <= n <= 1200:
                        out[k] = n
                except (TypeError, ValueError):
                    pass
    return out


def get_metadata_options(db: Database) -> dict:
    stored = db.get_json("metadata_options", {})
    return _normalize_meta_options(stored if isinstance(stored, dict) else {})


def put_metadata_options(db: Database, patch: dict) -> dict:
    stored = db.get_json("metadata_options", {})
    merged = dict(stored if isinstance(stored, dict) else {})
    if isinstance(patch, dict):
        merged.update(patch)
    norm = _normalize_meta_options(merged)
    db.set_json(
        "metadata_options",
        {
            "fields": norm["fields_map"],
            "checkPath": norm["checkPath"],
            "searchFrom": norm["search_from"],
            "sources": norm["sources_map"],
            "visible_fields": norm["visible_fields"],
            "only_diff_rows": norm["only_diff_rows"],
            "col_widths": norm["col_widths"],
        },
    )
    return norm

