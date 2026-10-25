"""Numbers for the dashboard and Home Assistant: folders, files in scope, to do, checked, changed, parked."""
from __future__ import annotations

import threading
import weakref

from .db import Database

_stats_lock = threading.Lock()
_stats_cache: weakref.WeakKeyDictionary[Database, dict[str, tuple[int, dict]]] = weakref.WeakKeyDictionary()


def has_pending(db: Database, media: str) -> bool:
    row = db.one(
        "SELECT 1 FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
        "WHERE d.media = ? AND d.enabled = 1 AND f.status IN ('pending', 'approved', 'processing') LIMIT 1",
        (media,),
    )
    return row is not None


def media_stats(db: Database, media: str) -> dict:
    seq_before = getattr(db, "data_seq", None)
    if seq_before is not None:
        with _stats_lock:
            per_db = _stats_cache.get(db)
            cached = per_db.get(media) if per_db else None
        if cached is not None and cached[0] == seq_before:
            return dict(cached[1])

    with db._lock:
        seq = getattr(db, "data_seq", None)
        if seq is not None:
            with _stats_lock:
                per_db = _stats_cache.get(db)
                cached = per_db.get(media) if per_db else None
            if cached is not None and cached[0] == seq:
                return dict(cached[1])

        total = pending = checked = changed = parked = skipped = errors = empty_sections = empty_changed = 0
        cover_present = cover_embedded = cover_missing = cover_choices = cover_not_found = 0
        rows = db.query_tuples(
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
        for status, measured, n, has_empty, empty_chg, cov_pres, cov_emb, cov_miss, cov_ch, cov_nf in rows:
            total += n
            empty_sections += has_empty
            empty_changed += empty_chg
            cover_present += cov_pres
            cover_embedded += cov_emb
            cover_missing += cov_miss
            cover_choices += cov_ch
            cover_not_found += cov_nf
            if measured:
                checked += n
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
        drow = db.query_tuples(
            "SELECT COUNT(*), COALESCE(SUM(enabled), 0) FROM dirs WHERE media = ? AND relpath != '.'",
            (media,),
        )
        dirs_total, dirs_enabled = drow[0] if drow else (0, 0)
        meta_diff = 0
        if media == "audio":
            mrow = db.query_tuples(
                "SELECT COUNT(*) FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
                "JOIN file_metadata m ON m.file_id = f.id "
                "WHERE d.media = 'audio' AND d.enabled = 1 AND m.status = 'diff'"
            )
            meta_diff = mrow[0][0] if mrow else 0
        result = {
            "directories_total": dirs_total,
            "directories_enabled": dirs_enabled,
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
        seq_after = getattr(db, "data_seq", None)
        if seq_after is not None:
            with _stats_lock:
                _stats_cache.setdefault(db, {})[media] = (seq_after, dict(result))
        return result
