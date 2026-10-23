"""Numbers for the dashboard and Home Assistant: folders, files in scope, to do, checked, changed, parked."""
from __future__ import annotations

from .db import Database


def has_pending(db: Database, media: str) -> bool:
    row = db.one(
        "SELECT 1 FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
        "WHERE d.media = ? AND d.enabled = 1 AND f.status IN ('pending', 'approved', 'processing') LIMIT 1",
        (media,),
    )
    return row is not None


def media_stats(db: Database, media: str) -> dict:
    total = pending = checked = changed = parked = skipped = errors = empty_sections = empty_changed = 0
    cover_present = cover_embedded = cover_missing = cover_choices = cover_not_found = 0
    rows = db.query(
        "SELECT f.status, (f.measured_at IS NOT NULL) AS measured, COUNT(*) AS n, "
        "COALESCE(SUM(CASE WHEN f.silent_sections > 0 OR f.silence_trimmed = 1 THEN 1 ELSE 0 END), 0) AS has_empty, "
        "COALESCE(SUM(CASE WHEN f.silence_trimmed = 1 OR (f.status = 'changed' AND f.silent_sections > 0) THEN 1 ELSE 0 END), 0) AS empty_chg, "
        "COALESCE(SUM(CASE WHEN f.has_cover = 1 THEN 1 ELSE 0 END), 0) AS cov_pres, "
        "COALESCE(SUM(CASE WHEN f.cover_status = 'embedded' THEN 1 ELSE 0 END), 0) AS cov_emb, "
        "COALESCE(SUM(CASE WHEN f.has_cover = 0 AND COALESCE(f.cover_status, 'missing') NOT IN ('embedded', 'skipped') THEN 1 ELSE 0 END), 0) AS cov_miss, "
        "COALESCE(SUM(CASE WHEN f.cover_status = 'choices' THEN 1 ELSE 0 END), 0) AS cov_choices, "
        "COALESCE(SUM(CASE WHEN f.cover_status = 'not_found' THEN 1 ELSE 0 END), 0) AS cov_nf "
        "FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
        "WHERE d.media = ? AND d.enabled = 1 GROUP BY f.status, measured",
        (media,),
    )
    for r in rows:
        n = r["n"]
        total += n
        empty_sections += r["has_empty"]
        empty_changed += r["empty_chg"]
        cover_present += r["cov_pres"]
        cover_embedded += r["cov_emb"]
        cover_missing += r["cov_miss"]
        cover_choices += r["cov_choices"]
        cover_not_found += r["cov_nf"]
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
    meta_diff = 0
    if media == "audio":
        mrow = db.one(
            "SELECT COUNT(*) AS n FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
            "JOIN file_metadata m ON m.file_id = f.id "
            "WHERE d.media = 'audio' AND d.enabled = 1 AND m.status = 'diff'"
        )
        meta_diff = mrow["n"] if mrow else 0
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
        "files_empty_sections": empty_sections,
        "files_empty_changed": empty_changed,
        "files_cover_present": cover_present,
        "files_cover_embedded": cover_embedded,
        "files_cover_missing": cover_missing,
        "files_cover_choices": cover_choices,
        "files_cover_not_found": cover_not_found,
        "files_cover_parked": cover_choices + cover_not_found,
        "files_meta_diff": meta_diff,
    }
