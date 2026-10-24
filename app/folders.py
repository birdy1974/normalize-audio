"""Folder tree: enabled/disabled per folder. Toggling a folder sets its whole subtree."""
from __future__ import annotations

from .db import Database

ROOT = "."


def enabled_set(db: Database, media: str) -> set[str]:
    return {r["relpath"] for r in db.query("SELECT relpath FROM dirs WHERE media = ? AND enabled = 1", (media,))}


def set_enabled(db: Database, media: str, rel: str, enabled: bool) -> None:
    value = 1 if enabled else 0
    if rel == ROOT:
        db.execute("UPDATE dirs SET enabled = ? WHERE media = ?", (value, media))
        return
    prefix = rel + "/"
    db.execute(
        "UPDATE dirs SET enabled = ? WHERE media = ? AND (relpath = ? OR substr(relpath, 1, ?) = ?)",
        (value, media, rel, len(prefix), prefix),
    )


def set_all(db: Database, media: str, enabled: bool) -> None:
    db.execute("UPDATE dirs SET enabled = ? WHERE media = ?", (1 if enabled else 0, media))


def invert_all(db: Database, media: str) -> None:
    """Invert the enabled/disabled selection of all folders for the given media type."""
    db.execute("UPDATE dirs SET enabled = CASE WHEN enabled = 1 THEN 0 ELSE 1 END WHERE media = ?", (media,))


def _subtree_files(db: Database, media: str, rel: str) -> int:
    prefix = rel + "/"
    return db.one(
        "SELECT COUNT(*) AS n FROM files WHERE media = ? AND (dir = ? OR substr(dir, 1, ?) = ?)",
        (media, rel, len(prefix), prefix),
    )["n"]


def info(db: Database, media: str, rel: str) -> dict | None:
    row = db.one("SELECT relpath, parent, enabled FROM dirs WHERE media = ? AND relpath = ?", (media, rel))
    if row is None:
        return None
    prefix = rel + "/" if rel != ROOT else ""
    mixed = False
    if prefix:
        mixed = db.one(
            "SELECT COUNT(*) AS n FROM dirs WHERE media = ? AND substr(relpath, 1, ?) = ? AND enabled != ?",
            (media, len(prefix), prefix, row["enabled"]),
        )["n"] > 0
    return {
        "relpath": rel,
        "name": media_root_name(rel),
        "enabled": bool(row["enabled"]),
        "mixed": mixed,
        "files": _subtree_files(db, media, rel),
        "direct_files": db.one("SELECT COUNT(*) AS n FROM files WHERE media = ? AND dir = ?", (media, rel))["n"],
    }


def media_root_name(rel: str) -> str:
    return rel if rel == ROOT else rel.rsplit("/", 1)[-1]


def children(db: Database, media: str, parent: str = ROOT) -> list[dict]:
    rows = db.query(
        "SELECT relpath, enabled FROM dirs WHERE media = ? AND parent = ? AND relpath != ? ORDER BY relpath",
        (media, parent, ROOT),
    )
    out = []
    for r in rows:
        rel = r["relpath"]
        prefix = rel + "/"
        mixed = db.one(
            "SELECT COUNT(*) AS n FROM dirs WHERE media = ? AND substr(relpath, 1, ?) = ? AND enabled != ?",
            (media, len(prefix), prefix, r["enabled"]),
        )["n"] > 0
        has_kids = db.one("SELECT COUNT(*) AS n FROM dirs WHERE media = ? AND parent = ?", (media, rel))["n"] > 0
        out.append({
            "relpath": rel,
            "name": media_root_name(rel),
            "enabled": bool(r["enabled"]),
            "mixed": mixed,
            "has_children": has_kids,
            "files": _subtree_files(db, media, rel),
        })
    return out
