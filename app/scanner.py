"""Walks a base location and keeps the files and folders tables in step with the disk.

New files and folders are added; files and folders that disappeared are removed;
files and folders that were already scanned are left untouched.
Hidden, Synology (@eaDir) and recycle folders are skipped.
"""
from __future__ import annotations

import os
import time

from .db import Database
from .media import EXTENSIONS, MEDIA

SKIP_PREFIXES = (".", "@", "#")


def _is_storable(name: str) -> bool:
    """SQLite (and UTF-8 in general) cannot store the lone surrogates Python uses
    (via surrogateescape) to represent file names that are not valid UTF-8 on disk."""
    return not any(0xD800 <= ord(c) <= 0xDFFF for c in name)


class Scanner:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.cancel_fn = None
        self.progress_fn = None

    def scan(self, media: str, cancel=None) -> dict:
        cancel = cancel or self.cancel_fn
        progress_fn = self.progress_fn
        root = self.cfg.root(media)
        if not os.path.isdir(root):
            self.db.log("error", f"{root} is not available. Check the volume mount in docker-compose.yml.", media)
            return {"available": False}
        now = time.time()
        dirs: dict[str, str] = {}      # folder relpath -> parent relpath
        files: dict[str, tuple] = {}   # file relpath -> (dir, name, ext)
        wanted = EXTENSIONS[media]
        skipped = 0
        try:
            top_entries = [e for e in os.listdir(root) if not e.startswith(SKIP_PREFIXES)]
        except OSError:
            top_entries = []
        est_total = max(1, len(top_entries) + 1)
        walked_top = 0
        if progress_fn:
            progress_fn(0, est_total, "")
        for dirpath, dirnames, filenames in os.walk(root):
            if cancel and cancel():
                self.db.log("warn", f"Scan stopped for {media}", media)
                return {"available": True, "stopped": True, "files": len(files), "new": 0, "changed": 0,
                        "removed": 0, "folders": len(dirs), "skipped": skipped}
            good_dirnames = []
            for d in dirnames:
                if d.startswith(SKIP_PREFIXES):
                    continue
                if not _is_storable(d):
                    skipped += 1
                    continue
                good_dirnames.append(d)
            dirnames[:] = sorted(good_dirnames)
            here = os.path.relpath(dirpath, root).replace(os.sep, "/")
            if here == ".":
                here = "."
            elif "/" not in here:
                walked_top += 1
                if progress_fn:
                    progress_fn(min(walked_top, est_total - 1), est_total, here)
            for d in dirnames:
                child = d if here == "." else f"{here}/{d}"
                dirs[child] = here
            for name in filenames:
                if name.startswith(SKIP_PREFIXES) or "." not in name:
                    continue
                if not _is_storable(name):
                    skipped += 1
                    continue
                ext = name.rsplit(".", 1)[1].lower()
                if ext not in wanted:
                    continue
                rel = name if here == "." else f"{here}/{name}"
                files[rel] = (here, name, ext)

        total_sync = max(1, len(files))
        if progress_fn:
            progress_fn(0, total_sync, "")

        new = removed = 0
        with self.db.tx():
            existing = {r["relpath"]: r for r in self.db.query(
                "SELECT id, relpath, status FROM files WHERE media = ?", (media,))}
            inserts = []
            for rel, (here, name, ext) in files.items():
                old = existing.pop(rel, None)
                if old is None:
                    inserts.append((media, rel, here, name, ext, 0, 0, now))
                    new += 1
            if inserts:
                self.db.executemany(
                    "INSERT INTO files (media, relpath, dir, name, ext, size, mtime_ns, status, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)", inserts)
            for old in existing.values():
                if old["status"] != "processing":
                    self.db.execute("DELETE FROM file_metadata WHERE file_id = ?", (old["id"],))
                    self.db.execute("DELETE FROM files WHERE id = ?", (old["id"],))
                    removed += 1

            known = {r["relpath"]: r["enabled"] for r in self.db.query(
                "SELECT relpath, enabled FROM dirs WHERE media = ?", (media,))}
            if ROOT_KEY not in known:
                self.db.execute("INSERT INTO dirs (media, relpath, parent, enabled) VALUES (?, '.', '.', 1)", (media,))
                known[ROOT_KEY] = 1
            for child in sorted(dirs, key=lambda p: (p.count("/"), p)):
                parent = dirs[child]
                if child not in known:
                    enabled = known.get(parent, 1)     # new folders inherit the parent's state
                    self.db.execute("INSERT INTO dirs (media, relpath, parent, enabled) VALUES (?, ?, ?, ?)",
                                    (media, child, parent, enabled))
                    known[child] = enabled
            gone = [k for k in known if k != ROOT_KEY and k not in dirs]
            for k in gone:
                self.db.execute("DELETE FROM dirs WHERE media = ? AND relpath = ?", (media, k))

        if progress_fn:
            progress_fn(total_sync, total_sync, "")

        if new or removed:
            parts = []
            if new:
                parts.append(f"{new} new")
            if removed:
                parts.append(f"{removed} removed")
            self.db.log("info", f"Scan: {', '.join(parts)} {media} file(s)", media)
        if skipped:
            self.db.log(
                "error",
                f"Scan: skipped {skipped} {media} file(s)/folder(s) with a name that is not valid UTF-8; "
                "rename them on disk to include them.",
                media,
            )
        return {"available": True, "files": len(files), "new": new, "changed": 0,
                "removed": removed, "folders": len(dirs), "skipped": skipped}

    def scan_all(self, cancel=None) -> dict:
        cancel = cancel or self.cancel_fn
        results = {}
        for m in MEDIA:
            if cancel and cancel():
                results[m] = {"available": True, "stopped": True, "files": 0, "new": 0, "changed": 0, "removed": 0, "folders": 0, "skipped": 0}
                continue
            try:
                results[m] = self.scan(m)
            except Exception as exc:
                self.db.log("error", f"Scan failed for {m}: {exc}", m)
                results[m] = {"available": False}
        return results


ROOT_KEY = "."
