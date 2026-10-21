"""Settings, schedule, run state and Home Assistant configuration (stored as JSON in the database)."""
from __future__ import annotations

import copy
import secrets

from .db import Database

DEFAULT_SETTINGS = {
    "general": {"dryRun": False, "preserveMtime": True},
    "audio": {
        "target": -16.0, "truePeak": -1.0, "threshold": 6.0, "override": False,
        "lossy": "gain", "writeMode": "inplace", "outputDir": "/data/normalized-audio",
    },
    "video": {
        "target": -16.0, "truePeak": -1.0, "threshold": 6.0, "override": False,
        "writeMode": "inplace", "outputDir": "/data/normalized-video",
    },
}

RANGES = {"target": (-30.0, -10.0), "truePeak": (-6.0, -1.0), "threshold": (1.0, 12.0)}
LOSSY_MODES = ("gain", "reencode", "skip")
WRITE_MODES = ("inplace", "backup", "output")
ON_CLOSE = ("finish", "pause")

DEFAULT_RUN = {"mode": "stopped", "paused": False, "types": {"audio": True, "video": True}, "lastFinished": None}

DEFAULT_SENSORS = [
    {"path": "status", "name": "Status", "unit": "", "deviceClass": "", "icon": "mdi:waveform", "enabled": True, "group": "Overall"},
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
    if key in ("override", "dryRun", "preserveMtime"):
        return bool(value)
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


def set_run(db: Database, **changes) -> dict:
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
