"""SQLite persistence: one file in the config volume, survives restarts."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dirs (
    media TEXT NOT NULL,
    relpath TEXT NOT NULL,
    parent TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (media, relpath)
);
CREATE INDEX IF NOT EXISTS dirs_parent ON dirs(media, parent);
CREATE INDEX IF NOT EXISTS dirs_enabled ON dirs(media, enabled, relpath);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media TEXT NOT NULL,
    relpath TEXT NOT NULL,
    dir TEXT NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    codec TEXT,
    family TEXT,
    lufs REAL,
    true_peak REAL,
    measured_at REAL,
    gain_db REAL,
    adjust_db REAL NOT NULL DEFAULT 0,
    applied_db REAL,
    method TEXT,
    changed_at REAL,
    silent_sections INTEGER NOT NULL DEFAULT 0,
    silent_duration REAL NOT NULL DEFAULT 0,
    silence_trimmed INTEGER NOT NULL DEFAULT 0,
    has_cover INTEGER,
    cover_status TEXT,
    cover_candidates TEXT,
    tag_artist TEXT,
    tag_album TEXT,
    tag_title TEXT,
    cover_query_source TEXT,
    seen_at REAL NOT NULL,
    UNIQUE (media, relpath)
);
CREATE INDEX IF NOT EXISTS files_status ON files(media, status);
CREATE INDEX IF NOT EXISTS files_dir ON files(media, dir);
CREATE INDEX IF NOT EXISTS files_dir_status ON files(media, dir, status);
CREATE TABLE IF NOT EXISTS cover_cache (
    query_key TEXT PRIMARY KEY,
    candidates TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_metadata (
    file_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unchecked',
    current_json TEXT NOT NULL DEFAULT '{}',
    path_json TEXT NOT NULL DEFAULT '{}',
    online_json TEXT NOT NULL DEFAULT '{}',
    selected_json TEXT NOT NULL DEFAULT '{}',
    diffs_json TEXT NOT NULL DEFAULT '[]',
    path_diff INTEGER NOT NULL DEFAULT 0,
    checked_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS file_metadata_status ON file_metadata(status);
CREATE TABLE IF NOT EXISTS metadata_cache (
    query_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    media TEXT,
    msg TEXT NOT NULL
);
"""

LOG_KEEP = 500


class Database:
    """A single connection guarded by a lock; the workload is small."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA cache_size=-16000")
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(files)").fetchall()}
            for col, ddl in (
                ("silent_sections", "INTEGER NOT NULL DEFAULT 0"),
                ("silent_duration", "REAL NOT NULL DEFAULT 0"),
                ("silence_trimmed", "INTEGER NOT NULL DEFAULT 0"),
                ("has_cover", "INTEGER"),
                ("cover_status", "TEXT"),
                ("cover_candidates", "TEXT"),
                ("tag_artist", "TEXT"),
                ("tag_album", "TEXT"),
                ("tag_title", "TEXT"),
                ("cover_query_source", "TEXT"),
            ):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE files ADD COLUMN {col} {ddl}")
            self._conn.execute("CREATE INDEX IF NOT EXISTS files_cover_status ON files(media, cover_status)")

    def query(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()) -> dict | None:
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql: str, args=()) -> int:
        with self._lock:
            return self._conn.execute(sql, args).rowcount

    def executemany(self, sql: str, seq) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)

    def insert(self, sql: str, args=()) -> int:
        with self._lock:
            return self._conn.execute(sql, args).lastrowid

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def get_json(self, key: str, default=None):
        row = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_json(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def log(self, level: str, msg: str, media: str | None = None) -> None:
        with self._lock, self.tx():
            self._conn.execute(
                "INSERT INTO log (ts, level, media, msg) VALUES (?, ?, ?, ?)", (time.time(), level, media, msg)
            )
            self._conn.execute("DELETE FROM log WHERE id <= (SELECT MAX(id) FROM log) - ?", (LOG_KEEP,))

    def log_list(self, limit: int = 60) -> list[dict]:
        return self.query("SELECT ts, level, media, msg FROM log ORDER BY id DESC LIMIT ?", (limit,))

    def clear_log(self) -> None:
        self.execute("DELETE FROM log")
