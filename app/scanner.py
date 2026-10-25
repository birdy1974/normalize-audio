"""Walks a base location and keeps the files and folders tables in step with the disk.

New files and folders are added; files and folders that disappeared are removed;
files and folders that were already scanned are left untouched.
Hidden, Synology (@eaDir) and recycle folders are skipped.

Includes:
- Option A: Incremental directory (mtime_ns, ctime_ns) quick scan with POSIX coarse-clock
  racy-clean window and explicit full=True fallback.
- Option B: Fast bytes-mode os.scandir iterative walker + bounded ThreadPoolExecutor
  subtree parallelism (safe on Synology DS918+ HDD RAID and non-AVX J3455 CPUs).
- Option C: Targeted single-folder / subtree scan (target_dir) with B-tree range scoping.
- Option D: Raw-tuple cursor diffing and chunked bulk SQLite inserts/updates/deletes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import time

from .db import Database
from .media import EXTENSIONS, MEDIA

SKIP_PREFIXES = (".", "@", "#")
_SKIP_FIRST_BYTES = (ord("."), ord("@"), ord("#"))
ROOT_KEY = "."

# Safety window (2 seconds) against coarse filesystem clock ticks / same-second mutations
# (the classic Git "racy-clean" index edge case): directories modified within the last 2s
# are always re-read with os.scandir() rather than trusted solely by cached timestamps.
RACY_WINDOW_NS = 2_000_000_000
_SQL_CHUNK = 400
_HIGH_LATENCY_DIR_NS = 1_500_000  # 1.5 ms: cold HDD / SMB / NFS threshold for parallel fan-out
_SCAN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dir-scan")


def _configured_scan_workers(root_elapsed_ns: int = 0) -> int:
    raw = os.environ.get("SCAN_WORKERS", "").strip()
    if raw:
        try:
            return max(1, min(4, int(raw)))
        except ValueError:
            pass
    if root_elapsed_ns >= _HIGH_LATENCY_DIR_NS:
        return min(3, max(1, (os.cpu_count() or 2)))
    return 1


def _is_storable(name: str) -> bool:
    """SQLite (and UTF-8 in general) cannot store the lone surrogates Python uses
    (via surrogateescape) to represent file names that are not valid UTF-8 on disk."""
    if name.isascii():
        return True
    return not any(0xD800 <= ord(c) <= 0xDFFF for c in name)


def _normalize_target_dir(target_dir: str | None) -> str | None:
    if target_dir is None:
        return None
    norm = target_dir.strip().replace("\\", "/").strip("/")
    if not norm or norm == ".":
        return None
    parts = [p for p in norm.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ValueError("Invalid target folder path")
    if any(p.startswith(SKIP_PREFIXES) or not _is_storable(p) for p in parts):
        raise ValueError("Skipped or invalid folder path cannot be scanned")
    return "/".join(parts)


def _stat_sig(abs_b: bytes) -> tuple[int, int] | None:
    try:
        st = os.stat(abs_b)
    except OSError:
        return None
    # Verify it is still a directory (st_mode S_IFDIR == 0o040000)
    if (st.st_mode & 0o170000) != 0o040000:
        return None
    return (st.st_mtime_ns, st.st_ctime_ns)


def _lstat_dir_or_link(abs_b: bytes) -> tuple[str, int, int]:
    """Single-syscall lstat check for incremental traversal.

    Returns ('dir', mtime_ns, ctime_ns), ('link_dir', 0, 0), or ('gone', 0, 0).
    """
    try:
        st = os.lstat(abs_b)
    except OSError:
        return ("gone", 0, 0)
    fmt = st.st_mode & 0o170000
    if fmt == 0o040000:
        return ("dir", st.st_mtime_ns, st.st_ctime_ns)
    if fmt == 0o120000 and os.path.isdir(abs_b):
        return ("link_dir", 0, 0)
    return ("gone", 0, 0)


def _scan_dir_once(
    abs_b: bytes,
    here: str,
    wanted: set[str],
) -> tuple[tuple[int, int] | None, list[tuple[bytes, str]], dict[str, tuple[str, str, str]], int] | None:
    """Read one directory in bytes mode using getdents64 d_type.

    Captures stat signature *before* reading entries so concurrent mutations during scandir
    cannot leave a newer cached signature paired with an older entry list.
    Returns None if the directory disappeared or could not be opened.
    """
    sig = _stat_sig(abs_b)
    if sig is None:
        return None
    child_dirs: list[tuple[bytes, str]] = []
    direct_files: dict[str, tuple[str, str, str]] = {}
    local_skipped = 0
    try:
        with os.scandir(abs_b) as it:
            for entry in it:
                nb = entry.name
                if not nb or nb[0] in _SKIP_FIRST_BYTES:
                    continue
                try:
                    is_link = entry.is_symlink()
                    is_dir = entry.is_dir(follow_symlinks=True) if is_link else entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    try:
                        name = nb.decode("utf-8")
                    except UnicodeDecodeError:
                        local_skipped += 1
                        continue
                    child_rel = name if here == "." else f"{here}/{name}"
                    # Match os.walk(followlinks=False): record symlinked folder, do not descend into it.
                    child_dirs.append((b"" if is_link else entry.path, child_rel))
                else:
                    if b"." not in nb:
                        continue
                    try:
                        name = nb.decode("utf-8")
                    except UnicodeDecodeError:
                        local_skipped += 1
                        continue
                    dot = name.rfind(".")
                    if dot <= 0:
                        continue
                    ext = name[dot + 1:].lower()
                    if ext not in wanted:
                        continue
                    rel = name if here == "." else f"{here}/{name}"
                    direct_files[rel] = (here, name, ext)
    except OSError:
        return None
    return sig, child_dirs, direct_files, local_skipped


def _chunked(seq: list, size: int = _SQL_CHUNK):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class Scanner:
    def __init__(self, cfg, db: Database):
        self.cfg = cfg
        self.db = db
        self.cancel_fn = None
        self.progress_fn = None

    def scan(
        self,
        media: str,
        cancel=None,
        *,
        full: bool = False,
        target_dir: str | None = None,
    ) -> dict:
        cancel = cancel or self.cancel_fn
        progress_fn = self.progress_fn
        root = self.cfg.root(media)
        if not os.path.isdir(root):
            self.db.log("error", f"{root} is not available. Check the volume mount in docker-compose.yml.", media)
            return {"available": False}

        norm_target = _normalize_target_dir(target_dir)
        if norm_target is not None:
            return self._scan_target(media, root, norm_target, cancel, progress_fn)

        # Load existing directory metadata outside a write transaction to decide
        # whether we can run an incremental (mtime_ns, ctime_ns) scan.
        known_rows = self.db.query_tuples(
            "SELECT relpath, parent, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok "
            "FROM dirs WHERE media = ?",
            (media,),
        )
        known_dirs: dict[str, tuple[str, int, int, int, int]] = {
            r[0]: (r[1], r[2], r[3], r[4], r[5]) for r in known_rows
        }
        root_meta = known_dirs.get(ROOT_KEY)
        can_incremental = (not full) and (root_meta is not None) and (root_meta[4] == 1)
        if can_incremental:
            return self._scan_incremental(media, root, known_dirs, cancel, progress_fn)
        return self._scan_full(media, root, known_dirs, cancel, progress_fn)

    # ------------------------------------------------------------ full scan (Option B + D)

    def _scan_full(
        self,
        media: str,
        root: str,
        known_dirs: dict[str, tuple[str, int, int, int, int]],
        cancel,
        progress_fn,
    ) -> dict:
        now = time.time()
        scan_started_ns = time.time_ns()
        root_b = os.fsencode(root)
        wanted = EXTENSIONS[media]

        # dir relpath -> (parent, mtime_ns, ctime_ns, clean_ok)
        dir_records: dict[str, tuple[str, int, int, int]] = {}
        files: dict[str, tuple[str, str, str]] = {}
        skipped = 0

        t_probe = time.perf_counter_ns()
        root_res = _scan_dir_once(root_b, ROOT_KEY, wanted)
        probe_ns = time.perf_counter_ns() - t_probe
        if root_res is None:
            self.db.log("error", f"{root} is not available. Check the volume mount in docker-compose.yml.", media)
            return {"available": False}
        root_sig, top_children, root_files, root_skipped = root_res
        skipped += root_skipped
        files.update(root_files)
        root_clean = 1 if (root_skipped == 0 and (scan_started_ns - max(root_sig)) >= RACY_WINDOW_NS) else 0
        dir_records[ROOT_KEY] = (ROOT_KEY, root_sig[0], root_sig[1], root_clean)

        top_recurse: list[tuple[bytes, str]] = []
        for child_b, child_rel in top_children:
            if not child_b:
                dir_records[child_rel] = (ROOT_KEY, 0, 0, 0)
            else:
                top_recurse.append((child_b, child_rel))

        est_total = max(1, len(top_recurse) + 1)
        if progress_fn:
            progress_fn(0, est_total, "")

        def _walk_subtree(start_item: tuple[bytes, str]):
            sub_dirs: dict[str, tuple[str, int, int, int]] = {}
            sub_files: dict[str, tuple[str, str, str]] = {}
            sub_skip = 0
            stack: list[tuple[bytes, str, str]] = [(start_item[0], start_item[1], ROOT_KEY)]
            while stack:
                if cancel and cancel():
                    return None
                abs_b, here, parent = stack.pop()
                res = _scan_dir_once(abs_b, here, wanted)
                if res is None:
                    continue
                sig, children, dfiles, lskip = res
                sub_skip += lskip
                clean_ok = 1 if (lskip == 0 and (scan_started_ns - max(sig)) >= RACY_WINDOW_NS) else 0
                sub_dirs[here] = (parent, sig[0], sig[1], clean_ok)
                if dfiles:
                    sub_files.update(dfiles)
                for cb, crel in children:
                    if not cb:
                        sub_dirs[crel] = (here, 0, 0, 0)
                    else:
                        stack.append((cb, crel, here))
            return sub_dirs, sub_files, sub_skip, start_item[1]

        workers = _configured_scan_workers(probe_ns)
        walked_top = 0
        cancelled = False
        if cancel and cancel():
            cancelled = True
        elif workers > 1 and len(top_recurse) >= 8:
            for out in _SCAN_POOL.map(_walk_subtree, top_recurse):
                if out is None or (cancel and cancel()):
                    cancelled = True
                    break
                s_dirs, s_files, s_skip, top_rel = out
                dir_records.update(s_dirs)
                files.update(s_files)
                skipped += s_skip
                walked_top += 1
                if progress_fn:
                    progress_fn(min(walked_top, est_total - 1), est_total, top_rel)
        else:
            for item in top_recurse:
                out = _walk_subtree(item)
                if out is None:
                    cancelled = True
                    break
                s_dirs, s_files, s_skip, top_rel = out
                dir_records.update(s_dirs)
                files.update(s_files)
                skipped += s_skip
                walked_top += 1
                if progress_fn:
                    progress_fn(min(walked_top, est_total - 1), est_total, top_rel)

        if cancelled:
            self.db.log("warn", f"Scan stopped for {media}", media)
            return {
                "available": True,
                "stopped": True,
                "files": len(files),
                "new": 0,
                "changed": 0,
                "removed": 0,
                "folders": max(0, len(dir_records) - 1),
                "skipped": skipped,
            }

        total_sync = max(1, len(files))
        if progress_fn:
            progress_fn(0, total_sync, "")

        new = removed = 0
        spared_processing_dirs: set[str] = set()
        with self.db.tx():
            existing_rows = self.db.query_tuples(
                "SELECT relpath, id, status, dir FROM files WHERE media = ?", (media,)
            )
            existing = {r[0]: (r[1], r[2], r[3]) for r in existing_rows}
            inserts = []
            for rel, (here, name, ext) in files.items():
                old = existing.pop(rel, None)
                if old is None:
                    inserts.append((media, rel, here, name, ext, 0, 0, now))
                    new += 1
            if inserts:
                self.db.executemany(
                    "INSERT INTO files (media, relpath, dir, name, ext, size, mtime_ns, status, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    inserts,
                )

            remove_ids = []
            for fid, st_status, fdir in existing.values():
                if st_status != "processing":
                    remove_ids.append(fid)
                else:
                    spared_processing_dirs.add(fdir)
            removed = len(remove_ids)
            for chunk in _chunked(remove_ids):
                ph = ",".join("?" * len(chunk))
                self.db.execute(f"DELETE FROM file_metadata WHERE file_id IN ({ph})", chunk)
                self.db.execute(f"DELETE FROM files WHERE id IN ({ph})", chunk)

            self._sync_dirs_table(media, dir_records, spared_processing_dirs)

        if progress_fn:
            progress_fn(total_sync, total_sync, "")

        total_folders = max(0, len(dir_records) - 1)
        self._log_scan_outcome(media, new, removed, skipped)
        return {
            "available": True,
            "files": len(files),
            "new": new,
            "changed": 0,
            "removed": removed,
            "folders": total_folders,
            "skipped": skipped,
        }

    # ------------------------------------------------------------ incremental scan (Option A + B + D)

    def _scan_incremental(
        self,
        media: str,
        root: str,
        known_dirs: dict[str, tuple[str, int, int, int, int]],
        cancel,
        progress_fn,
    ) -> dict:
        now = time.time()
        scan_started_ns = time.time_ns()
        root_b = os.fsencode(root)
        wanted = EXTENSIONS[media]

        known_children: dict[str, list[str]] = {}
        for rel, (parent, _en, _mt, _ct, _ok) in known_dirs.items():
            if rel != ROOT_KEY:
                known_children.setdefault(parent, []).append(rel)

        dir_records: dict[str, tuple[str, int, int, int]] = {}
        rescanned_files: dict[str, dict[str, tuple[str, str, str]]] = {}
        skipped = 0

        def _is_unchanged(here: str, sig: tuple[int, int]) -> bool:
            kd = known_dirs.get(here)
            if kd is None or kd[4] != 1 or sig[0] <= 0:
                return False
            if kd[2] != sig[0] or kd[3] != sig[1]:
                return False
            if (scan_started_ns - max(sig)) < RACY_WINDOW_NS:
                return False
            return True

        # Check root "."
        t_probe = time.perf_counter_ns()
        root_sig = _stat_sig(root_b)
        probe_ns = time.perf_counter_ns() - t_probe
        if root_sig is None:
            self.db.log("error", f"{root} is not available. Check the volume mount in docker-compose.yml.", media)
            return {"available": False}

        top_items: list[tuple[bytes, str]] = []
        if _is_unchanged(ROOT_KEY, root_sig):
            dir_records[ROOT_KEY] = (ROOT_KEY, root_sig[0], root_sig[1], 1)
            for child_rel in known_children.get(ROOT_KEY, ()):
                top_items.append((os.path.join(root_b, os.fsencode(child_rel)), child_rel))
        else:
            root_res = _scan_dir_once(root_b, ROOT_KEY, wanted)
            if root_res is None:
                self.db.log("error", f"{root} is not available. Check the volume mount in docker-compose.yml.", media)
                return {"available": False}
            rsig, top_children, root_files, root_skipped = root_res
            skipped += root_skipped
            rescanned_files[ROOT_KEY] = root_files
            rclean = 1 if (root_skipped == 0 and (scan_started_ns - max(rsig)) >= RACY_WINDOW_NS) else 0
            dir_records[ROOT_KEY] = (ROOT_KEY, rsig[0], rsig[1], rclean)
            for cb, crel in top_children:
                if not cb:
                    dir_records[crel] = (ROOT_KEY, 0, 0, 0)
                else:
                    top_items.append((cb, crel))

        est_total = max(1, len(top_items) + 1)
        if progress_fn:
            progress_fn(0, est_total, "")

        def _walk_inc_subtree(start_item: tuple[bytes, str]):
            sub_dirs: dict[str, tuple[str, int, int, int]] = {}
            sub_rescanned: dict[str, dict[str, tuple[str, str, str]]] = {}
            sub_skip = 0
            stack: list[tuple[bytes, str, str]] = [(start_item[0], start_item[1], ROOT_KEY)]
            while stack:
                if cancel and cancel():
                    return None
                abs_b, here, parent = stack.pop()
                kind, mt, ct = _lstat_dir_or_link(abs_b)
                if kind == "link_dir":
                    sub_dirs[here] = (parent, 0, 0, 0)
                    continue
                if kind == "gone":
                    # Directory vanished on disk; omitting it from sub_dirs marks it & descendants gone.
                    continue
                sig = (mt, ct)
                if _is_unchanged(here, sig):
                    sub_dirs[here] = (parent, mt, ct, 1)
                    for child_rel in known_children.get(here, ()):
                        leaf = child_rel.rsplit("/", 1)[-1]
                        stack.append((abs_b + b"/" + os.fsencode(leaf), child_rel, here))
                else:
                    res = _scan_dir_once(abs_b, here, wanted)
                    if res is None:
                        continue
                    rsig, children, dfiles, lskip = res
                    sub_skip += lskip
                    clean_ok = 1 if (lskip == 0 and (scan_started_ns - max(rsig)) >= RACY_WINDOW_NS) else 0
                    sub_dirs[here] = (parent, rsig[0], rsig[1], clean_ok)
                    sub_rescanned[here] = dfiles
                    for cb, crel in children:
                        if not cb:
                            sub_dirs[crel] = (here, 0, 0, 0)
                        else:
                            stack.append((cb, crel, here))
            return sub_dirs, sub_rescanned, sub_skip, start_item[1]

        workers = _configured_scan_workers(probe_ns)
        walked_top = 0
        cancelled = False
        if cancel and cancel():
            cancelled = True
        elif workers > 1 and len(top_items) >= 16:
            for out in _SCAN_POOL.map(_walk_inc_subtree, top_items):
                if out is None or (cancel and cancel()):
                    cancelled = True
                    break
                s_dirs, s_rescanned, s_skip, top_rel = out
                dir_records.update(s_dirs)
                rescanned_files.update(s_rescanned)
                skipped += s_skip
                walked_top += 1
                if progress_fn:
                    progress_fn(min(walked_top, est_total - 1), est_total, top_rel)
        else:
            for item in top_items:
                out = _walk_inc_subtree(item)
                if out is None:
                    cancelled = True
                    break
                s_dirs, s_rescanned, s_skip, top_rel = out
                dir_records.update(s_dirs)
                rescanned_files.update(s_rescanned)
                skipped += s_skip
                walked_top += 1
                if progress_fn:
                    progress_fn(min(walked_top, est_total - 1), est_total, top_rel)

        if cancelled:
            self.db.log("warn", f"Scan stopped for {media}", media)
            return {
                "available": True,
                "stopped": True,
                "files": 0,
                "new": 0,
                "changed": 0,
                "removed": 0,
                "folders": max(0, len(dir_records) - 1),
                "skipped": skipped,
            }

        gone_dirs = [k for k in known_dirs if k != ROOT_KEY and k not in dir_records]
        dirty_dirs = list(rescanned_files.keys()) + gone_dirs

        # Fast zero-write path when every folder on disk matched its cached signature!
        if not dirty_dirs and len(dir_records) == len(known_dirs):
            total_files = self.db.query_tuples(
                "SELECT COUNT(*) FROM files WHERE media = ?", (media,)
            )[0][0]
            if progress_fn:
                progress_fn(est_total, est_total, "")
            self._log_scan_outcome(media, 0, 0, skipped)
            return {
                "available": True,
                "files": total_files,
                "new": 0,
                "changed": 0,
                "removed": 0,
                "folders": max(0, len(dir_records) - 1),
                "skipped": skipped,
            }

        new = removed = 0
        spared_processing_dirs: set[str] = set()
        with self.db.tx():
            if dirty_dirs:
                existing_by_dir: dict[str, dict[str, tuple[int, str]]] = {}
                for chunk in _chunked(dirty_dirs):
                    ph = ",".join("?" * len(chunk))
                    rows = self.db.query_tuples(
                        f"SELECT dir, relpath, id, status FROM files WHERE media = ? AND dir IN ({ph})",
                        [media, *chunk],
                    )
                    for fdir, frel, fid, fst in rows:
                        existing_by_dir.setdefault(fdir, {})[frel] = (fid, fst)

                inserts = []
                remove_ids = []
                for drel, dfiles in rescanned_files.items():
                    old_map = existing_by_dir.pop(drel, {})
                    for frel, (here, name, ext) in dfiles.items():
                        old = old_map.pop(frel, None)
                        if old is None:
                            inserts.append((media, frel, here, name, ext, 0, 0, now))
                            new += 1
                    for fid, fst in old_map.values():
                        if fst != "processing":
                            remove_ids.append(fid)
                        else:
                            spared_processing_dirs.add(drel)

                # Any remaining entries in existing_by_dir belong to gone_dirs
                for drel, old_map in existing_by_dir.items():
                    for fid, fst in old_map.values():
                        if fst != "processing":
                            remove_ids.append(fid)
                        else:
                            spared_processing_dirs.add(drel)

                if inserts:
                    self.db.executemany(
                        "INSERT INTO files (media, relpath, dir, name, ext, size, mtime_ns, status, seen_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                        inserts,
                    )
                removed = len(remove_ids)
                for chunk in _chunked(remove_ids):
                    ph = ",".join("?" * len(chunk))
                    self.db.execute(f"DELETE FROM file_metadata WHERE file_id IN ({ph})", chunk)
                    self.db.execute(f"DELETE FROM files WHERE id IN ({ph})", chunk)

            self._sync_dirs_table(media, dir_records, spared_processing_dirs)
            total_files = self.db.query_tuples(
                "SELECT COUNT(*) FROM files WHERE media = ?", (media,)
            )[0][0]

        if progress_fn:
            progress_fn(est_total, est_total, "")

        total_folders = max(0, len(dir_records) - 1)
        self._log_scan_outcome(media, new, removed, skipped)
        return {
            "available": True,
            "files": total_files,
            "new": new,
            "changed": 0,
            "removed": removed,
            "folders": total_folders,
            "skipped": skipped,
        }

    # ------------------------------------------------------------ targeted folder scan (Option C)

    def _scan_target(
        self,
        media: str,
        root: str,
        target_rel: str,
        cancel,
        progress_fn,
    ) -> dict:
        now = time.time()
        scan_started_ns = time.time_ns()
        wanted = EXTENSIONS[media]
        abs_target = os.path.join(root, *target_rel.split("/"))
        abs_target_b = os.fsencode(abs_target)

        if progress_fn:
            progress_fn(0, 1, target_rel)

        sub_dirs: dict[str, tuple[str, int, int, int]] = {}
        sub_files: dict[str, tuple[str, str, str]] = {}
        skipped = 0

        if os.path.isdir(abs_target):
            parent_of_target = target_rel.rsplit("/", 1)[0] if "/" in target_rel else ROOT_KEY
            stack: list[tuple[bytes, str, str]] = [(abs_target_b, target_rel, parent_of_target)]
            while stack:
                if cancel and cancel():
                    self.db.log("warn", f"Scan stopped for {media}", media)
                    return {
                        "available": True,
                        "stopped": True,
                        "files": len(sub_files),
                        "new": 0,
                        "changed": 0,
                        "removed": 0,
                        "folders": len(sub_dirs),
                        "skipped": skipped,
                    }
                abs_b, here, parent = stack.pop()
                res = _scan_dir_once(abs_b, here, wanted)
                if res is None:
                    continue
                sig, children, dfiles, lskip = res
                skipped += lskip
                clean_ok = 1 if (lskip == 0 and (scan_started_ns - max(sig)) >= RACY_WINDOW_NS) else 0
                sub_dirs[here] = (parent, sig[0], sig[1], clean_ok)
                if dfiles:
                    sub_files.update(dfiles)
                for cb, crel in children:
                    if not cb:
                        sub_dirs[crel] = (here, 0, 0, 0)
                    else:
                        stack.append((cb, crel, here))

        # Exact B-tree prefix range: '/' is ASCII 47 and '0' is ASCII 48,
        # so (col >= target + '/' AND col < target + '0') matches every descendant path
        # without SQL LIKE '%' / '_' wildcard pitfalls.
        lo = target_rel + "/"
        hi = target_rel + "0"

        new = removed = 0
        spared_processing_dirs: set[str] = set()
        with self.db.tx():
            existing_rows = self.db.query_tuples(
                "SELECT relpath, id, status, dir FROM files "
                "WHERE media = ? AND (dir = ? OR (dir >= ? AND dir < ?))",
                (media, target_rel, lo, hi),
            )
            existing = {r[0]: (r[1], r[2], r[3]) for r in existing_rows}
            inserts = []
            for frel, (here, name, ext) in sub_files.items():
                old = existing.pop(frel, None)
                if old is None:
                    inserts.append((media, frel, here, name, ext, 0, 0, now))
                    new += 1
            if inserts:
                self.db.executemany(
                    "INSERT INTO files (media, relpath, dir, name, ext, size, mtime_ns, status, seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                    inserts,
                )

            remove_ids = []
            for fid, fst, fdir in existing.values():
                if fst != "processing":
                    remove_ids.append(fid)
                else:
                    spared_processing_dirs.add(fdir)
            removed = len(remove_ids)
            for chunk in _chunked(remove_ids):
                ph = ",".join("?" * len(chunk))
                self.db.execute(f"DELETE FROM file_metadata WHERE file_id IN ({ph})", chunk)
                self.db.execute(f"DELETE FROM files WHERE id IN ({ph})", chunk)

            # Ensure root and any ancestor folders exist (placeholder ancestors keep scanned_ok=0
            # so a subsequent full/incremental scan still inspects sibling folders under them).
            anc_parts = target_rel.split("/")
            anc_chain = [ROOT_KEY] + ["/".join(anc_parts[:i]) for i in range(1, len(anc_parts))]
            ph_anc = ",".join("?" * len(anc_chain))
            known_anc = {
                r[0]: r[1]
                for r in self.db.query_tuples(
                    f"SELECT relpath, enabled FROM dirs WHERE media = ? AND relpath IN ({ph_anc})",
                    [media, *anc_chain],
                )
            }
            if ROOT_KEY not in known_anc:
                self.db.execute(
                    "INSERT INTO dirs (media, relpath, parent, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok) "
                    "VALUES (?, '.', '.', 1, 0, 0, 0)",
                    (media,),
                )
                known_anc[ROOT_KEY] = 1
            if sub_dirs:
                for i in range(1, len(anc_parts)):
                    anc = "/".join(anc_parts[:i])
                    anc_parent = "/".join(anc_parts[: i - 1]) if i > 1 else ROOT_KEY
                    if anc not in known_anc:
                        en = known_anc.get(anc_parent, 1)
                        self.db.execute(
                            "INSERT INTO dirs (media, relpath, parent, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok) "
                            "VALUES (?, ?, ?, ?, 0, 0, 0)",
                            (media, anc, anc_parent, en),
                        )
                        known_anc[anc] = en

            # Sync target_rel and its descendants in dirs
            scoped_known = {
                r[0]: (r[1], r[2], r[3], r[4])
                for r in self.db.query_tuples(
                    "SELECT relpath, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok "
                    "FROM dirs WHERE media = ? AND (relpath = ? OR (relpath >= ? AND relpath < ?))",
                    (media, target_rel, lo, hi),
                )
            }
            enabled_lookup = dict(known_anc)
            for k, v in scoped_known.items():
                enabled_lookup[k] = v[0]

            dir_inserts = []
            dir_updates = []
            for child in sorted(sub_dirs, key=lambda p: (p.count("/"), p)):
                parent, mt, ct, ok = sub_dirs[child]
                if child in spared_processing_dirs:
                    ok = 0
                if child not in scoped_known:
                    en = enabled_lookup.get(parent, 1)
                    enabled_lookup[child] = en
                    dir_inserts.append((media, child, parent, en, mt, ct, ok))
                else:
                    _en, old_mt, old_ct, old_ok = scoped_known[child]
                    if (old_mt, old_ct, old_ok) != (mt, ct, ok):
                        dir_updates.append((mt, ct, ok, media, child))

            if dir_inserts:
                self.db.executemany(
                    "INSERT INTO dirs (media, relpath, parent, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    dir_inserts,
                )
            if dir_updates:
                self.db.executemany(
                    "UPDATE dirs SET dir_mtime_ns = ?, dir_ctime_ns = ?, scanned_ok = ? "
                    "WHERE media = ? AND relpath = ?",
                    dir_updates,
                )

            gone = [k for k in scoped_known if k not in sub_dirs]
            for chunk in _chunked(gone):
                ph = ",".join("?" * len(chunk))
                self.db.execute(f"DELETE FROM dirs WHERE media = ? AND relpath IN ({ph})", [media, *chunk])

            # If target_rel itself was deleted on disk, mark its immediate parent scanned_ok=0
            if not sub_dirs:
                parent_rel = anc_chain[-1]
                self.db.execute(
                    "UPDATE dirs SET scanned_ok = 0 WHERE media = ? AND relpath = ?",
                    (media, parent_rel),
                )

            total_files = self.db.query_tuples("SELECT COUNT(*) FROM files WHERE media = ?", (media,))[0][0]
            total_folders = self.db.query_tuples(
                "SELECT COUNT(*) FROM dirs WHERE media = ? AND relpath != '.'", (media,)
            )[0][0]

        if progress_fn:
            progress_fn(1, 1, target_rel)

        self._log_scan_outcome(media, new, removed, skipped, target_rel=target_rel)
        return {
            "available": True,
            "files": total_files,
            "new": new,
            "changed": 0,
            "removed": removed,
            "folders": total_folders,
            "skipped": skipped,
            "target": target_rel,
        }

    # ------------------------------------------------------------ shared helpers

    def _sync_dirs_table(
        self,
        media: str,
        dir_records: dict[str, tuple[str, int, int, int]],
        spared_processing_dirs: set[str],
    ) -> None:
        known = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in self.db.query_tuples(
                "SELECT relpath, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok FROM dirs WHERE media = ?",
                (media,),
            )
        }
        enabled_map = {k: v[0] for k, v in known.items()}

        root_parent, root_mt, root_ct, root_ok = dir_records.get(ROOT_KEY, (ROOT_KEY, 0, 0, 0))
        if ROOT_KEY in spared_processing_dirs:
            root_ok = 0
        if ROOT_KEY not in known:
            self.db.execute(
                "INSERT INTO dirs (media, relpath, parent, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok) "
                "VALUES (?, '.', '.', 1, ?, ?, ?)",
                (media, root_mt, root_ct, root_ok),
            )
            enabled_map[ROOT_KEY] = 1
        else:
            _en, old_mt, old_ct, old_ok = known[ROOT_KEY]
            if (old_mt, old_ct, old_ok) != (root_mt, root_ct, root_ok):
                self.db.execute(
                    "UPDATE dirs SET dir_mtime_ns = ?, dir_ctime_ns = ?, scanned_ok = ? "
                    "WHERE media = ? AND relpath = '.'",
                    (root_mt, root_ct, root_ok, media),
                )

        dir_inserts = []
        dir_updates = []
        for child in sorted((k for k in dir_records if k != ROOT_KEY), key=lambda p: (p.count("/"), p)):
            parent, mt, ct, ok = dir_records[child]
            if child in spared_processing_dirs:
                ok = 0
            if child not in known:
                enabled = enabled_map.get(parent, 1)  # new folders inherit the parent's state
                enabled_map[child] = enabled
                dir_inserts.append((media, child, parent, enabled, mt, ct, ok))
            else:
                _en, old_mt, old_ct, old_ok = known[child]
                if (old_mt, old_ct, old_ok) != (mt, ct, ok):
                    dir_updates.append((mt, ct, ok, media, child))

        if dir_inserts:
            self.db.executemany(
                "INSERT INTO dirs (media, relpath, parent, enabled, dir_mtime_ns, dir_ctime_ns, scanned_ok) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                dir_inserts,
            )
        if dir_updates:
            self.db.executemany(
                "UPDATE dirs SET dir_mtime_ns = ?, dir_ctime_ns = ?, scanned_ok = ? "
                "WHERE media = ? AND relpath = ?",
                dir_updates,
            )

        gone = [k for k in known if k != ROOT_KEY and k not in dir_records]
        for chunk in _chunked(gone):
            ph = ",".join("?" * len(chunk))
            self.db.execute(f"DELETE FROM dirs WHERE media = ? AND relpath IN ({ph})", [media, *chunk])

    def _log_scan_outcome(
        self, media: str, new: int, removed: int, skipped: int, target_rel: str | None = None
    ) -> None:
        if new or removed:
            parts = []
            if new:
                parts.append(f"{new} new")
            if removed:
                parts.append(f"{removed} removed")
            scope = f" ({target_rel})" if target_rel else ""
            self.db.log("info", f"Scan{scope}: {', '.join(parts)} {media} file(s)", media)
        if skipped:
            self.db.log(
                "error",
                f"Scan: skipped {skipped} {media} file(s)/folder(s) with a name that is not valid UTF-8; "
                "rename them on disk to include them.",
                media,
            )

    def scan_all(self, cancel=None, *, full: bool = False) -> dict:
        cancel = cancel or self.cancel_fn
        results = {}
        for m in MEDIA:
            if cancel and cancel():
                results[m] = {
                    "available": True,
                    "stopped": True,
                    "files": 0,
                    "new": 0,
                    "changed": 0,
                    "removed": 0,
                    "folders": 0,
                    "skipped": 0,
                }
                continue
            try:
                results[m] = self.scan(m, full=full) if full else self.scan(m)
            except Exception as exc:
                self.db.log("error", f"Scan failed for {m}: {exc}", m)
                results[m] = {"available": False}
        return results
