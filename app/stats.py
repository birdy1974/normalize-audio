"""Numbers for the dashboard and Home Assistant: folders, files in scope, to do, checked, changed, parked."""
from __future__ import annotations

from . import folders
from .db import Database


def media_stats(db: Database, media: str) -> dict:
    enabled = folders.enabled_set(db, media)
    total = pending = checked = changed = parked = skipped = errors = 0
    rows = db.query(
        "SELECT dir, status, (measured_at IS NOT NULL) AS measured, COUNT(*) AS n "
        "FROM files WHERE media = ? GROUP BY dir, status, measured", (media,))
    for r in rows:
        if r["dir"] not in enabled:
            continue
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
    dirs = db.query("SELECT enabled FROM dirs WHERE media = ? AND relpath != '.'", (media,))
    return {
        "directories_total": len(dirs),
        "directories_enabled": sum(1 for d in dirs if d["enabled"]),
        "files_total": total,
        "files_pending": pending,
        "files_checked": checked,
        "files_changed": changed,
        "files_parked": parked,
        "files_skipped": skipped,
        "files_errors": errors,
    }
