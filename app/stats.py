"""Numbers for the dashboard and Home Assistant: folders, files in scope, to do, checked, changed, parked."""
from __future__ import annotations

from .db import Database


def has_pending(db: Database, media: str) -> bool:
    row = db.one(
        "SELECT 1 FROM files f JOIN dirs d ON d.media = f.media AND d.relpath = f.dir "
        "WHERE f.media = ? AND f.status IN ('pending', 'approved', 'processing') AND d.enabled = 1 LIMIT 1",
        (media,),
    )
    return row is not None


def media_stats(db: Database, media: str) -> dict:
    total = pending = checked = changed = parked = skipped = errors = 0
    rows = db.query(
        "SELECT f.status, (f.measured_at IS NOT NULL) AS measured, COUNT(*) AS n "
        "FROM files f JOIN dirs d ON d.media = f.media AND d.relpath = f.dir "
        "WHERE f.media = ? AND d.enabled = 1 GROUP BY f.status, measured",
        (media,),
    )
    for r in rows:
        n = r["n"]
        total += n
        if r["measured"]:
            checked += n
        status = r["status"]
        if status in ("pending", "approved", "processing"):
            pending += n
        elif status == "changed":
            changed += n
        elif status == "parked":
            parked += n
        elif status == "skipped":
            skipped += n
        elif status == "error":
            errors += n
    dirs = db.one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(enabled), 0) AS enabled "
        "FROM dirs WHERE media = ? AND relpath != '.'",
        (media,),
    )
    return {
        "directories_total": dirs["total"] if dirs else 0,
        "directories_enabled": dirs["enabled"] if dirs else 0,
        "files_total": total,
        "files_pending": pending,
        "files_checked": checked,
        "files_changed": changed,
        "files_parked": parked,
        "files_skipped": skipped,
        "files_errors": errors,
    }
