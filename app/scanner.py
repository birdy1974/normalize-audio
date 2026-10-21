"""Walks a base location and keeps the files and folders tables in step with the disk.

New files are queued; files whose size or time stamp changed are queued again;
files that disappeared are removed. Hidden, Synology (@eaDir) and recycle folders are skipped.
"""
from __future__ import annotations

import os
import time

from .db import Database
from .media import EXTENSIONS, MEDIA

SKIP_PREFIXES = (".", "@", "#")


class Scanner:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db

    def scan(self, media: str) -> dict:
        root = self.cfg.root(media)
        if not os.path.isdir(root):
            self.db.log("error", f"{root} is not available. Check the volume mount in docker-compose.yml.", media)
            return {"available": False}
        now = time.time()
        dirs: dict[str, str] = {}      # folder relpath -> parent relpath
        files: dict[str, tuple] = {}   # file relpath -> (dir, name, ext, size, mtime_ns)
        wanted = EXTENSIONS[media]
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(SKIP_PREFIXES))
            here = os.path.relpath(dirpath, root).replace(os.sep, "/")
            if here == ".":
                here = "."
            for d in dirnames:
                child = d if here == "." else f"{here}/{d}"
                dirs[child] = here
            for name in filenames:
                if name.startswith(SKIP_PREFIXES) or "." not in name:
                    continue
                ext = name.rsplit(".", 1)[1].lower()
                if ext not in wanted:
                    continue
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                rel = name if here == "." else f"{here}/{name}"
                files[rel] = (here, name, ext, st.st_size, st.st_mtime_ns)

        new = changed = removed = 0
        with self.db.tx():
            existing = {r["relpath"]: r for r in self.db.query(
                "SELECT id, relpath, size, mtime_ns, status FROM files WHERE media = ?", (media,))}
            inserts = []
            for rel, (here, name, ext, size, mtime) in files.items():
                old = existing.pop(rel, None)
                if old is None:
                    inserts.append((media, rel, here, name, ext, size, mtime, now))
                    new += 1
                elif old["size"] != size or old["mtime_ns"] != mtime:
                    if old["status"] != "processing":
                        self.db.execute(
                            "UPDATE files SET dir=?, name=?, ext=?, size=?, mtime_ns=?, seen_at=?, status='pending', "
                            "reason=NULL, codec=NULL, family=NULL, lufs=NULL, true_peak=NULL, measured_at=NULL, "
                            "gain_db=NULL, adjust_db=0, applied_db=NULL, method=NULL WHERE id=?",
                            (here, name, ext, size, mtime, now, old["id"]))
                        changed += 1
                    else:
                        self.db.execute("UPDATE files SET seen_at=? WHERE id=?", (now, old["id"]))
                else:
                    self.db.execute("UPDATE files SET seen_at=? WHERE id=?", (now, old["id"]))
            if inserts:
                self.db.executemany(
                    "INSERT INTO files (media, relpath, dir, name, ext, size, mtime_ns, status, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)", inserts)
            for old in existing.values():
                if old["status"] != "processing":
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

        if new or removed:
            parts = []
            if new:
                parts.append(f"{new} new")
            if removed:
                parts.append(f"{removed} removed")
            self.db.log("info", f"Scan: {', '.join(parts)} {media} file(s)", media)
        return {"available": True, "files": len(files), "new": new, "changed": changed,
                "removed": removed, "folders": len(dirs)}

    def scan_all(self) -> dict:
        return {m: self.scan(m) for m in MEDIA}


ROOT_KEY = "."
