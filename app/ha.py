"""Home Assistant interface: the status payload, the REST sensor YAML and the connection test."""
from __future__ import annotations

import json

import httpx

from . import schedule, stats, state
from .media import MEDIA

MAIN_NAME = "Audio Normalizer"
MAIN_ATTRS = ["progress_percent", "errors", "current_file", "last_run_finished", "audio", "video"]
ENTITY = "sensor.audio_normalizer"


def payload(engine) -> dict:
    db = engine.db
    now = engine.now()
    sched = state.get_schedule(db)
    settings = state.get_settings(db)
    out: dict = {}
    total = checked = errors = 0
    for m in MEDIA:
        s = stats.media_stats(db, m)
        grid = sched[m]
        nxt = schedule.next_change(grid, now)
        out[m] = {
            "in_schedule_window": schedule.in_window(grid, now),
            "directories_total": s["directories_total"],
            "directories_enabled": s["directories_enabled"],
            "files_total": s["files_total"],
            "files_pending": s["files_pending"],
            "files_checked": s["files_checked"],
            "files_changed": s["files_changed"],
            "files_parked": s["files_parked"],
            "files_skipped": s["files_skipped"],
            "target_lufs": settings[m]["target"],
            "next_schedule_change": nxt.isoformat(timespec="minutes") if nxt else None,
        }
        total += s["files_total"]
        checked += s["files_checked"]
        errors += s["files_errors"]
    return {
        "status": engine.status(),
        "progress_percent": int(round(100 * checked / total)) if total else 0,
        "errors": errors,
        "current_file": engine.current_file(),
        "last_run_finished": state.get_run(db)["lastFinished"],
        **out,
    }


def enabled_paths(ha_cfg: dict) -> list[str]:
    return [s["path"] for s in ha_cfg["sensors"] if s["enabled"]]


def yaml(ha_cfg: dict, port: int) -> str:
    """configuration.yaml for Home Assistant: one REST sensor, one entity per enabled value."""
    lines = [
        "rest:",
        f'  - resource: "http://<NAS-IP>:{port}/api/v1/ha/status"',
        "    headers:",
        f'      Authorization: "Bearer {ha_cfg["endpointToken"]}"',
        "    scan_interval: 60",
        "    sensor:",
        "      # main entity: state = run status, attributes = full payload (used by the Lovelace card)",
        f'      - name: "{MAIN_NAME}"',
        "        unique_id: audio_normalizer_main",
        '        value_template: "{{ value_json.status }}"',
        "        json_attributes:",
    ]
    lines += [f"          - {attr}" for attr in MAIN_ATTRS]
    lines.append("        icon: mdi:waveform")
    for s in ha_cfg["sensors"]:
        if not s["enabled"]:
            continue
        uid = "audio_normalizer_" + s["path"].replace(".", "_")
        lines.append("      # optional single sensor")
        lines.append(f"      - name: {json.dumps(s['name'], ensure_ascii=False)}")   # JSON string = valid YAML quoting
        lines.append(f"        unique_id: {uid}")
        lines.append(f'        value_template: "{{{{ value_json.{s["path"]} }}}}"')
        if s.get("unit"):
            lines.append(f'        unit_of_measurement: "{s["unit"]}"')
        if s.get("deviceClass"):
            lines.append(f"        device_class: {s['deviceClass']}")
        if s.get("icon"):
            lines.append(f"        icon: {s['icon']}")
    return "\n".join(lines) + "\n"


def card_yaml(ha_cfg: dict) -> str:
    lines = ["type: custom:audio-normalizer-card", f"entity: {ENTITY}", "title: Audio Normalizer", "fields:"]
    lines += [f"  - {p}" for p in enabled_paths(ha_cfg)]
    return "\n".join(lines) + "\n"


def test_connection(url: str, token: str) -> dict:
    """Checks the Home Assistant URL and access token (GET /api/config)."""
    if not url:
        return {"ok": False, "message": "Enter the Home Assistant URL first."}
    if not token:
        return {"ok": False, "message": "Enter the long-lived access token first."}
    try:
        r = httpx.get(url.rstrip("/") + "/api/config", headers={"Authorization": f"Bearer {token}"}, timeout=5.0)
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"Home Assistant not reached: {exc.__class__.__name__}"}
    if r.status_code == 401:
        return {"ok": False, "message": "Home Assistant rejected the token (401)."}
    if r.status_code != 200:
        return {"ok": False, "message": f"Unexpected answer from Home Assistant: HTTP {r.status_code}"}
    version = r.json().get("version", "?")
    return {"ok": True, "message": f"Connected to Home Assistant {version}"}
