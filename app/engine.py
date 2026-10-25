"""Run control, schedule gate, scanning and the per-file pipeline.

Pipeline for one file:  measure -> decide -> (park | skip | ok | apply) -> verify -> atomic commit.
Parked files wait for the user; the run continues with the next file.
"""
from __future__ import annotations

import inspect
import json
import logging
import math
import os
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import cover, ffmpeg as ff, metadata
from . import schedule, state, stats
from .config import Config, tool_path
from .decide import decide
from .media import DEFAULT_KBPS, MEDIA, audio_family
from .scanner import Scanner
from .watcher import LibraryWatcher

log = logging.getLogger("normalizer")
GAIN_TOOLS = ("mp3gain", "aacgain")
_TAG_WARM_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tag-warm")
_TAG_WARM_CHUNK = 32


class FileBusy(RuntimeError):
    """The file is being processed by the run; the request is refused."""


def _clock(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60}:{seconds % 60:02d}"


class Engine:
    def __init__(self, cfg: Config, db):
        self.cfg = cfg
        self.db = db
        self.tz = ZoneInfo(cfg.tz)
        self.scanner = Scanner(cfg, db)
        self.tools = {name for name in GAIN_TOOLS if tool_path(getattr(cfg, name))}
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._scan_exec_lock = threading.Lock()
        self._scanning = 0                     # scans running (the periodic one and manual ones)
        self._fs_seq = 0                       # bumped after scans/watcher syncs so open UI views refresh
        self._inflight: dict[int, dict] = {}    # file id -> {future, relpath, media, approved}
        self._cancel: set[int] = set()
        self._park_on_cancel: set[int] = set()  # approved files stopped by user -> return to 'parked'
        self._action_gen = 0                    # bumped by stop_all_actions() to halt active batches/scans/tools
        self._had_work = False
        self._mode_seen: str | None = None     # the app mode (off / bulk / force / auto) last applied
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._meta_stop = threading.Event()
        self._meta_lock = threading.Lock()
        self._meta_select_lock = threading.Lock()
        self._action_prog_lock = threading.Lock()
        self._action_progress: dict = {
            "active": False,
            "kind": "",
            "label": "",
            "done": 0,
            "total": 0,
            "percent": 0,
            "current_file": "",
        }
        self._approved_batch_total = 0
        self._meta_checking = False
        self._meta_thread: threading.Thread | None = None
        self._meta_progress: dict = {
            "running": False,
            "force": False,
            "total": 0,
            "done": 0,
            "diff": 0,
            "ok": 0,
            "path_diff": 0,
            "current_file": "",
            "stopped": False,
            "started_at": None,
            "finished_at": None,
            "seq": 0,
        }
        self._threads: list[threading.Thread] = []
        self.watcher = LibraryWatcher(cfg, db, self._on_watcher_targets, self._stop)
        self._pool = ThreadPoolExecutor(max_workers=cfg.concurrency, thread_name_prefix="normalizer")
        # restart recovery: files that were being processed go back to the queue
        db.execute("UPDATE files SET status = 'pending' WHERE status = 'processing'")
        self._remove_stale_temp_files()
        state.get_ha(db)                         # creates the endpoint token on first start
        self.apply_mode()                        # off stops a run left over from before the restart; auto starts one

    # ------------------------------------------------------------ helpers

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def abs_path(self, media: str, rel: str) -> str:
        return os.path.join(self.cfg.root(media), rel)

    def window_open(self, media: str, when: datetime | None = None) -> bool:
        return schedule.in_window(state.get_schedule(self.db)[media], when or self.now())

    def mode(self) -> str:
        """What the app does on its own: 'off', 'bulk', 'force' (only a started run works) or 'auto'."""
        return state.get_settings(self.db)["general"]["mode"]

    def scan_interval(self) -> int:
        """Minutes between automatic looks for new, changed and removed files."""
        return state.get_schedule(self.db)["scanEvery"]

    def last_scan(self) -> str | None:
        return state.get_run(self.db).get("lastScan")

    def next_scan(self) -> str | None:
        """When the next automatic scan is due; only Automatic mode has one."""
        if self.mode() != "auto":
            return None
        last = self.last_scan()
        if not last:
            return self.now().replace(microsecond=0).isoformat(timespec="seconds")
        try:
            when = datetime.fromisoformat(last) + timedelta(minutes=self.scan_interval())
        except ValueError:
            return self.now().replace(microsecond=0).isoformat(timespec="seconds")
        return when.isoformat(timespec="seconds")

    def scan_status(self) -> dict:
        """The automatic side of the app, for the dashboard and Home Assistant."""
        return {"mode": self.mode(), "scan_every": self.scan_interval(),
                "last_scan": self.last_scan(), "next_scan": self.next_scan()}

    def apply_mode(self) -> str:
        """Bring the run in line with the mode setting. Called on start and on every tick."""
        mode = self.mode()
        previous, self._mode_seen = self._mode_seen, mode
        if previous == mode:
            return mode
        run = state.get_run(self.db)
        if mode == "off":
            if run["mode"] != "stopped" or run["paused"]:
                state.set_run(self.db, mode="stopped", paused=False)
            with self._lock:                 # stop the work that only the automatic run would do
                for fid, item in self._inflight.items():
                    if not item["approved"]:
                        self._cancel.add(fid)
            if previous is not None:
                self.db.log("info", "Mode off: scanning and normalizing are stopped")
        elif mode == "bulk":
            if previous in ("auto", "force") and (run["mode"] == "active" or run["paused"]):
                state.set_run(self.db, mode="stopped", paused=False)
                with self._lock:
                    for fid, item in self._inflight.items():
                        if not item["approved"]:
                            self._cancel.add(fid)
                self.db.log("info", "Bulk mode: the automatic run is stopped, press Start to normalize the files that are here"
                                    if previous == "auto" else
                                    "Bulk mode: the force run is stopped, press Start to normalize the files that are here")
        elif mode == "force":
            if previous in ("auto", "bulk") and (run["mode"] == "active" or run["paused"]):
                state.set_run(self.db, mode="stopped", paused=False)
                with self._lock:
                    for fid, item in self._inflight.items():
                        if not item["approved"]:
                            self._cancel.add(fid)
                self.db.log("info", "Force mode: the automatic run is stopped, press Start to check and normalize all files in enabled folders again"
                                    if previous == "auto" else
                                    "Force mode: the bulk run is stopped, press Start to check and normalize all files in enabled folders again")
        else:                                # auto
            # switching to automatic starts at once; a restart keeps a pause the user left in place
            state.set_run(self.db, mode="active", **({"paused": False} if previous is not None else {}))
            self.db.log("info", f"Automatic mode is on: new files are checked and normalized every "
                                f"{self.scan_interval()} minutes")
        self._had_work = False
        self._wake.set()
        return mode

    def wake(self) -> None:
        self._wake.set()

    def _new_cancel(self):
        with self._lock:
            gen = self._action_gen
        return lambda: self._stop.is_set() or self._action_gen != gen

    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def scanning(self) -> bool:
        """True while the disk is being walked for new, changed and removed files."""
        with self._scan_lock:
            return self._scanning > 0

    def current_file(self) -> str | None:
        with self._lock:
            items = sorted(self._inflight.values(), key=lambda item: item["approved"])
            return items[0]["relpath"] if items else None

    def next_pending_file(self, types: dict) -> str | None:
        """Return the relpath of the first queued pending file in an enabled folder, if any."""
        for m in MEDIA:
            if not types.get(m):
                continue
            row = self.db.one(
                "SELECT f.relpath FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
                "WHERE d.media = ? AND d.enabled = 1 AND f.status = 'pending' LIMIT 1",
                (m,),
            )
            if row:
                return row["relpath"]
        return None

    def approved_status(self) -> tuple[int, str | None]:
        """Return (count, current_relpath) of approved parked files currently running or waiting."""
        with self._lock:
            inflight = {
                fid: item["relpath"]
                for fid, item in self._inflight.items()
                if item.get("approved")
            }
        rows = self.db.query("SELECT id, relpath FROM files WHERE status = 'approved' ORDER BY id")
        waiting = [r["relpath"] for r in rows if r["id"] not in inflight]
        count = len(inflight) + len(waiting)
        current = next(iter(inflight.values()), None) or (waiting[0] if waiting else None)
        return count, current

    def note_approved(self, added: int) -> None:
        if added <= 0:
            return
        with self._action_prog_lock:
            self._approved_batch_total = max(self._approved_batch_total + added, added)

    def approved_progress(self, pending: int) -> tuple[int, int]:
        with self._action_prog_lock:
            if pending <= 0:
                self._approved_batch_total = 0
                return 0, 0
            tot = max(self._approved_batch_total, pending)
            self._approved_batch_total = tot
            return max(0, tot - pending), tot

    def _set_action_progress(self, kind: str, label: str, done: int, total: int, current_file: str = "") -> None:
        tot = max(1, int(total or 0))
        dn = max(0, min(tot, int(done or 0)))
        pct = int(round((dn / tot) * 100))
        with self._action_prog_lock:
            self._action_progress = {
                "active": True,
                "kind": kind,
                "label": label,
                "done": dn,
                "total": tot,
                "percent": pct,
                "current_file": current_file or "",
            }

    def _clear_action_progress(self, kind: str | None = None) -> None:
        with self._action_prog_lock:
            if kind is None or self._action_progress.get("kind") == kind:
                self._action_progress = {
                    "active": False,
                    "kind": "",
                    "label": "",
                    "done": 0,
                    "total": 0,
                    "percent": 0,
                    "current_file": "",
                }

    def get_action_progress(self) -> dict:
        with self._action_prog_lock:
            return dict(self._action_progress)

    def status(self, media_stats: dict | None = None) -> str:
        mode = self.mode()
        if mode == "off":
            return "off"
        run = state.get_run(self.db)
        if run["mode"] != "active" and mode != "auto":
            return "stopped"            # auto keeps the run active by itself, also before the first tick
        if run["paused"]:
            return "paused"
        types = [m for m in MEDIA if run["types"].get(m)]
        if not types:
            return "idle"
        open_types = [m for m in types if self.window_open(m)]
        if not open_types:
            return "waiting_window"
        # only work in a type whose window is open counts as running
        if self.inflight_count() > 0:
            return "running"
        if media_stats is not None:
            busy = any(media_stats[m]["files_pending"] for m in open_types)
        else:
            busy = any(stats.has_pending(self.db, m) for m in open_types)
        return "running" if busy else "idle"

    # ------------------------------------------------------------ run control

    def _queue_force(self, types: dict) -> int:
        """Queue every file in the enabled folders of the selected media types to be checked again."""
        queued = 0
        with self.db.tx():
            for m in MEDIA:
                if not types.get(m):
                    continue
                queued += self.db.execute(
                    "UPDATE files SET status = 'pending', reason = NULL, lufs = NULL, true_peak = NULL, "
                    "measured_at = NULL, gain_db = NULL, adjust_db = 0, applied_db = NULL, method = NULL, "
                    "changed_at = NULL, silent_sections = 0, silent_duration = 0, silence_trimmed = 0 "
                    "WHERE +media = ? AND status != 'processing' "
                    "AND (status != 'pending' OR measured_at IS NOT NULL OR reason IS NOT NULL OR lufs IS NOT NULL "
                    "OR gain_db IS NOT NULL OR adjust_db != 0 OR applied_db IS NOT NULL "
                    "OR silent_sections != 0 OR silence_trimmed != 0) "
                    "AND +dir IN (SELECT relpath FROM dirs WHERE media = ? AND enabled = 1)",
                    (m, m),
                )
        return queued

    def start(self) -> None:
        mode = self.mode()
        if mode == "off":
            raise ValueError("The app is off: choose Bulk, Force or Automatic mode in Settings first")
        self.apply_mode()
        run = state.get_run(self.db)
        if mode == "force":
            self._queue_force(run["types"])
        # a run that starts with nothing to do finishes at once (and in bulk/force mode stops itself)
        self._had_work = True
        state.set_run(self.db, mode="active", paused=False)
        if mode == "bulk":
            self.db.log("info", "Bulk run started")
        elif mode == "force":
            self.db.log("info", "Force run started: checking all files in enabled folders again")
        else:
            self.db.log("info", "Run started")
        if self._threads:
            self.tick()
        self._wake.set()

    def stop(self) -> None:
        if self.mode() == "auto":
            raise ValueError("Automatic mode keeps watching for new files; pause the run to hold it, "
                             "or choose Off, Bulk or Force in Settings")
        state.set_run(self.db, mode="stopped", paused=False)
        with self._lock:
            for fid, item in self._inflight.items():
                if not item["approved"]:
                    self._cancel.add(fid)
        self.db.log("info", "Run stopped")
        self._wake.set()

    def stop_all_actions(self) -> dict:
        """Stop / kill any currently running action: normalization run, approved parked work,
        metadata check, directory scan, batch checks/executions, and active ffmpeg/gain subprocesses."""
        stopped_items: list[str] = []
        with self._lock:
            self._action_gen += 1
            inflight_ids = list(self._inflight.keys())
            for fid, item in self._inflight.items():
                self._cancel.add(fid)
                if item.get("approved"):
                    self._park_on_cancel.add(fid)

        if self.scanning():
            stopped_items.append("folder scan")

        with self._meta_lock:
            meta_was_running = self._meta_checking
        self._meta_stop.set()
        if meta_was_running:
            stopped_items.append("metadata check")

        reverted_approved = self.db.execute("UPDATE files SET status = 'parked' WHERE status = 'approved'")
        if reverted_approved:
            stopped_items.append(f"{reverted_approved} queued parked file(s)")

        run = state.get_run(self.db)
        mode = self.mode()
        if mode == "auto":
            if run["mode"] == "active" and not run["paused"]:
                state.set_run(self.db, paused=True)
                stopped_items.append("automatic run (paused)")
        else:
            if run["mode"] == "active" or run["paused"]:
                state.set_run(self.db, mode="stopped", paused=False)
                stopped_items.append("normalization run")

        if inflight_ids:
            stopped_items.append(f"{len(inflight_ids)} active file task(s)")

        killed = ff.kill_active_tools()
        if killed and not inflight_ids:
            stopped_items.append(f"{killed} audio tool process(es)")

        self._clear_action_progress()
        with self._action_prog_lock:
            self._approved_batch_total = 0

        self._wake.set()
        summary = ", ".join(stopped_items) if stopped_items else "active request"
        self.db.log("warn", f"Stopped current action ({summary})")
        return {"ok": True, "stopped": stopped_items, "killed_processes": killed, "status": self.status()}

    def toggle_pause(self) -> None:
        run = state.get_run(self.db)
        if run["mode"] != "active":
            return
        paused = not run["paused"]
        state.set_run(self.db, paused=paused)
        what = "Automatic mode" if self.mode() == "auto" else "Run"
        self.db.log("info", f"{what} paused" if paused else f"{what} resumed")
        self._wake.set()
        if not paused and self._threads:
            self.tick()

    def set_types(self, changes: dict) -> None:
        run = state.get_run(self.db)
        for m, value in changes.items():
            if m in MEDIA and value is not None:
                run["types"][m] = bool(value)
        state.set_run(self.db, types=run["types"])
        self._wake.set()

    # ------------------------------------------------------------ threads

    def fs_seq(self) -> int:
        return self._fs_seq

    def start_threads(self) -> None:
        for target, name in ((self._loop, "scheduler"), (self._scan_loop, "scanner")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        self.watcher.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        self._meta_stop.set()
        with self._lock:
            for fid, item in self._inflight.items():
                if not item["approved"]:
                    self._cancel.add(fid)
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.watcher.join(timeout=2.0)
        with self._meta_lock:
            mt = self._meta_thread
        if mt is not None and mt.is_alive():
            mt.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception:  # keep the scheduler alive
                log.exception("scheduler tick failed")
            if not self._stop.is_set():
                self._wake.wait(1.0)

    def _scan_due(self) -> datetime | None:
        """When the next periodic scan is due; None right after a restart."""
        last = self.last_scan()
        if not last:
            return None
        try:
            return datetime.fromisoformat(last) + timedelta(minutes=self.scan_interval())
        except ValueError:
            return None

    def _scan_loop(self) -> None:
        """In Automatic mode, look for new, changed and removed files every `scanEvery` minutes.

        Bulk runs work on the library already loaded by a manual scan; they have no periodic scan.
        The loop wakes up every two seconds, so changing the mode or interval takes effect at once.
        """
        while not self._stop.is_set():
            if self.mode() != "auto":
                self._stop.wait(2.0)
                continue
            due = self._scan_due()
            if due is None or self.now() >= due:
                try:
                    self.scan_now()
                except Exception as exc:  # keep the loop alive; the next scan comes at the next interval
                    log.exception("scan failed")
                    self.db.log("error", f"Scan failed: {exc}")
                continue
            self._stop.wait(min(2.0, max(0.05, (due - self.now()).total_seconds())))

    def _on_watcher_targets(self, targets_by_media: dict[str, list[str | None]]) -> None:
        """Handle debounced directory/file structural events from LibraryWatcher (Option E)."""
        if self._stop.is_set():
            return
        any_new = False
        for m, target_list in targets_by_media.items():
            if self._stop.is_set() or m not in MEDIA:
                break
            has_root = bool(
                self.db.query_tuples("SELECT 1 FROM dirs WHERE media = ? AND relpath = '.' LIMIT 1", (m,))
            )
            if not has_root:
                r = self.scan_now(m, _from_watcher=True)
                if r.get(m, {}).get("new"):
                    any_new = True
                continue
            for tdir in target_list:
                if self._stop.is_set():
                    break
                r = self.scan_now(m, target_dir=tdir, _from_watcher=True)
                if r.get(m, {}).get("new"):
                    any_new = True
        if any_new and self.mode() == "auto":
            self._wake.set()

    def scan_now(
        self,
        media: str | None = None,
        *,
        full: bool = False,
        target_dir: str | None = None,
        _from_watcher: bool = False,
    ) -> dict:
        with self._scan_exec_lock:
            cancel = self._new_cancel()
            self.scanner.cancel_fn = cancel
            targets = [media] if media else list(MEDIA)
            total_targets = max(1, len(targets))
            label = f"Scanning {target_dir}" if target_dir else ("Full rescanning folders" if full else "Scanning folders")

            def _on_scan_prog(done: int, total: int, cur: str = "") -> None:
                self._set_action_progress("scan", label, done, total, cur)

            self.scanner.progress_fn = _on_scan_prog
            self._set_action_progress("scan", label, 0, total_targets, "")
            with self._scan_lock:
                self._scanning += 1
            try:
                if media:
                    if not full and target_dir is None:
                        # Keep 1-positional-arg call for backwards compatibility & test monkeypatches
                        res = {media: self.scanner.scan(media)}
                    else:
                        res = {media: self.scanner.scan(media, full=full, target_dir=target_dir)}
                else:
                    res = self.scanner.scan_all(full=full) if full else self.scanner.scan_all()
                return res
            finally:
                self.scanner.cancel_fn = None
                self.scanner.progress_fn = None
                self._clear_action_progress("scan")
                with self._scan_lock:
                    self._scanning -= 1
                if not cancel():
                    self._fs_seq += 1
                    if target_dir is None and not _from_watcher:
                        # a manual Rescan counts as a scan: the next automatic one starts a full interval later
                        state.set_run(self.db, lastScan=self.now().replace(microsecond=0).isoformat(timespec="seconds"))

    # ------------------------------------------------------------ scheduler

    def tick(self) -> None:
        with self._tick_lock:
            self._tick()

    def _tick(self) -> None:
        mode = self.apply_mode()
        if mode == "off":
            return
        run = state.get_run(self.db)
        sched = state.get_schedule(self.db)
        now = self.now()
        open_ = {m: schedule.in_window(sched[m], now) for m in MEDIA}

        if sched["onClose"] == "pause":
            with self._lock:
                for fid, item in self._inflight.items():
                    if not item["approved"] and not open_[item["media"]]:
                        self._cancel.add(fid)

        automatic = run["mode"] == "active" and not run["paused"]
        inflight = self.inflight_count()
        if inflight < self.cfg.concurrency:
            limit = self.cfg.concurrency + inflight
            # (file id, approved by the user, media). Approved work runs at any time; automatic work
            # only while the run is active, not paused, and the window of its media type is open.
            picks: list[tuple[int, bool, str]] = [
                (r["id"], True, r["media"]) for r in
                self.db.query("SELECT id, media FROM files WHERE status = 'approved' ORDER BY id LIMIT ?", (limit,))]

            if automatic and len(picks) < limit:
                for m in MEDIA:
                    if not (run["types"].get(m) and open_[m]):
                        continue
                    rows = self.db.query(
                        "SELECT f.id FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
                        "WHERE d.media = ? AND d.enabled = 1 AND f.status = 'pending' LIMIT ?",
                        (m, limit))
                    picks += [(r["id"], False, m) for r in rows]

            for fid, approved, _media in picks:
                with self._lock:
                    if len(self._inflight) >= self.cfg.concurrency:
                        break
                    if fid in self._inflight:
                        continue
                self._submit(fid, approved)
                if not approved:
                    self._had_work = True

        # the run is finished when nothing is left to do in the enabled, scheduled types
        if automatic and self._had_work and self.inflight_count() == 0:
            left = any(stats.has_pending(self.db, m) for m in MEDIA if run["types"].get(m))
            if not left:
                self._had_work = False
                state.set_run(self.db, lastFinished=now.isoformat(timespec="seconds"))
                if mode in ("bulk", "force"):
                    # a bulk or force run works through what is there now; files found later wait for the next Start
                    state.set_run(self.db, mode="stopped")
                    what = "Force" if mode == "force" else "Bulk"
                    self.db.log("info", f"{what} run finished: nothing left to check; new files wait for the next Start")
                else:
                    self.db.log("info", "Run finished: nothing left to check; automatic mode keeps watching")

    def _submit(self, fid: int, approved: bool) -> None:
        row = self.db.one("SELECT relpath, media FROM files WHERE id = ?", (fid,))
        if row is None:
            return
        with self._lock:
            future: Future = self._pool.submit(self._process, fid)
            self._inflight[fid] = {"future": future, "relpath": row["relpath"], "media": row["media"], "approved": approved}
        future.add_done_callback(lambda _f, i=fid: self._done(i))

    def _done(self, fid: int) -> None:
        with self._lock:
            self._inflight.pop(fid, None)
            self._cancel.discard(fid)
            self._park_on_cancel.discard(fid)
        self._wake.set()

    # ------------------------------------------------------------ per-file work

    def _process(self, fid: int, extra_cancel=None) -> None:
        row = self.db.one("SELECT * FROM files WHERE id = ?", (fid,))
        if row is None:
            return
        prior = row["status"]
        media, name = row["media"], row["name"]
        self.db.execute("UPDATE files SET status = 'processing', reason = NULL WHERE id = ?", (fid,))

        def cancel() -> bool:
            if extra_cancel and extra_cancel():
                return True
            with self._lock:
                return fid in self._cancel

        try:
            if prior == "approved":
                self._apply_approved(row, cancel)
            else:
                self._check(row, cancel)
        except ff.Cancelled:
            with self._lock:
                park_back = fid in self._park_on_cancel or bool(extra_cancel and extra_cancel())
                self._park_on_cancel.discard(fid)
            if prior == "approved":
                back = "parked" if park_back else "approved"
            else:
                back = "pending"
            self.db.execute("UPDATE files SET status = ? WHERE id = ?", (back, fid))
            self.db.log("info", f"{name}: stopped, back in the queue" if back != "parked" else f"{name}: stopped, returned to parked", media)
        except Exception as exc:
            self.db.execute("UPDATE files SET status = 'error', reason = ? WHERE id = ?", (str(exc)[:300], fid))
            self.db.log("error", f"{name}: {exc}", media)

    def _measure_and_detect(self, media: str, src: str, gen: dict,
                            cancel=None) -> tuple[float, float, ff.Probe, list[tuple[float, float]]]:
        if media == "audio":
            lufs, true_peak, probe, raw_cuts = ff.analyze_audio(
                self.cfg.ffmpeg, src,
                level_pct=gen.get("silenceLevelPct", 2.0),
                peak_db=gen.get("silencePeakDb", -40.0),
                min_duration=gen.get("silenceMinDuration", 5.0),
                cancel=cancel,
            )
            if not raw_cuts or not probe.duration or lufs <= -60 or not ff.kept_segments(probe.duration, raw_cuts):
                return lufs, true_peak, probe, []
            return lufs, true_peak, probe, raw_cuts
        lufs, true_peak, probe = ff.measure_and_probe(self.cfg.ffmpeg, src, cancel)
        return lufs, true_peak, probe, []

    def _check(self, row: dict, cancel) -> None:
        media, fid, rel = row["media"], row["id"], row["relpath"]
        src = self.abs_path(media, rel)
        if not os.path.isfile(src):
            self.db.execute("DELETE FROM files WHERE id = ?", (fid,))
            return
        st = state.get_settings(self.db)
        cfg_m = st[media]
        lufs, true_peak, probe, raw_cuts = self._measure_and_detect(media, src, st["general"], cancel)
        family = audio_family(probe.codec)
        now_ts = time.time()

        if lufs <= -60:
            self.db.execute(
                "UPDATE files SET lufs = ?, true_peak = ?, measured_at = ?, codec = ?, family = ?, "
                "gain_db = 0, silent_sections = 0, silent_duration = 0, status = 'skipped', reason = 'silent' WHERE id = ?",
                (lufs, true_peak, now_ts, probe.codec, family, fid))
            return
        strategy = cfg_m.get("lossy", "reencode") if media == "audio" else "reencode"
        d = decide(media=media, ext=row["ext"], family=family, measured=lufs, true_peak=true_peak,
                   target=cfg_m["target"], ceiling=cfg_m["truePeak"], threshold=cfg_m["threshold"],
                   override=cfg_m["override"], strategy=strategy, tools=self.tools,
                   hysteresis=cfg_m.get("hysteresis", 0.5))
        if d.action == "error":
            raise ff.ToolError(f"unsupported audio codec '{probe.codec}'")
        all_cuts = raw_cuts if d.action != "skip" else []
        sil_count = len(all_cuts)
        sil_dur = round(sum(ce - cs for cs, ce in all_cuts), 2)
        self.db.execute(
            "UPDATE files SET lufs = ?, true_peak = ?, measured_at = ?, codec = ?, family = ?, "
            "gain_db = ?, method = ?, silent_sections = ?, silent_duration = ? WHERE id = ?",
            (lufs, true_peak, now_ts, probe.codec, family, d.gain, d.method, sil_count, sil_dur, fid),
        )

        cuts = all_cuts if st["general"].get("trimSilence") else []
        if media == "audio":
            self.inspect_file_cover(row)
        if st["general"]["dryRun"]:
            self.db.execute("UPDATE files SET status = 'dryrun' WHERE id = ?", (fid,))
            extra = f", {len(cuts)} silent section(s)" if cuts else ""
            self.db.log("info", f"{row['name']}: dry run, would be {d.action}"
                                f" ({d.gain:+.1f} dB{extra})", media)
            return
        if d.action == "ok":
            if cuts:
                eff_method = "volume" if family == "lossless" else "reencode"
                self._apply(row, cfg_m, st, eff_method, 0.0, probe, family, cancel, cuts=cuts)
            else:
                self.db.execute("UPDATE files SET status = 'ok' WHERE id = ?", (fid,))
        elif d.action == "skip":
            self.db.execute("UPDATE files SET status = 'skipped', reason = ? WHERE id = ?", (d.reason, fid))
        elif d.action == "park":
            self.db.execute("UPDATE files SET status = 'parked', reason = ? WHERE id = ?", (d.reason, fid))
            if d.reason == "large_change":
                self.db.log("warn", f"{row['name']}: change {d.gain:+.1f} dB is large, parked for review", media)
        else:
            self._apply(row, cfg_m, st, d.method, d.gain, probe, family, cancel, cuts=cuts)

        if media == "audio" and st["general"].get("coverAuto"):
            fresh = self.db.one("SELECT * FROM files WHERE id = ?", (fid,)) or row
            try:
                self.check_file_cover(fresh, st, online=True)
            except Exception as exc:
                log.debug("cover check during run failed for %s: %s", rel, exc)

    def _detect_empty_sections(self, media: str, src: str, lufs: float, probe: ff.Probe,
                               gen: dict, cancel) -> list[tuple[float, float]]:
        if media != "audio" or not probe.duration or lufs <= -60:
            return []
        cuts = ff.detect_silent_sections(
            self.cfg.ffmpeg, src, lufs=lufs,
            level_pct=gen.get("silenceLevelPct", 2.0),
            peak_db=gen.get("silencePeakDb", -40.0),
            min_duration=gen.get("silenceMinDuration", 5.0),
            cancel=cancel,
        )
        if not cuts or not ff.kept_segments(probe.duration, cuts):
            return []
        return cuts

    def _silent_cuts(self, media: str, src: str, lufs: float, probe: ff.Probe,
                     gen: dict, cancel) -> list[tuple[float, float]]:
        if not gen.get("trimSilence"):
            return []
        return self._detect_empty_sections(media, src, lufs, probe, gen, cancel)

    def _apply_approved(self, row: dict, cancel) -> None:
        media = row["media"]
        st = state.get_settings(self.db)
        cfg_m = st[media]
        if row["gain_db"] is None:
            raise ff.ToolError("no gain stored for this file; check it again")
        gain = round(row["gain_db"] + (row["adjust_db"] or 0.0), 2)
        src = self.abs_path(media, row["relpath"])
        probe = ff.probe(self.cfg.ffmpeg, src, cancel)
        family = row["family"] or audio_family(probe.codec)
        room = cfg_m["truePeak"] - row["true_peak"] if row["true_peak"] is not None else None
        if gain > 0 and room is not None and gain > room:
            gain = round(max(0.0, room), 2)
            self.db.log("warn", f"{row['name']}: gain limited to keep the true peak under {cfg_m['truePeak']:.1f} dBTP",
                        media)
        cuts = self._silent_cuts(media, src, row["lufs"] if row["lufs"] is not None else -16.0,
                                 probe, st["general"], cancel)
        self._apply(row, cfg_m, st, row["method"] or "reencode", gain, probe, family, cancel, cuts=cuts)

    def _apply(self, row, cfg_m, st, method, gain, probe, family, cancel,
               cuts: list[tuple[float, float]] | None = None) -> None:
        media, fid, rel, name, ext = row["media"], row["id"], row["relpath"], row["name"], row["ext"]
        kept = ff.kept_segments(probe.duration or 0.0, cuts) if cuts and probe.duration else []
        has_cuts = bool(kept and cuts)
        if abs(gain) < 0.05 and not has_cuts:
            self.db.execute("UPDATE files SET status = 'ok', gain_db = ? WHERE id = ?", (gain, fid))
            return
        if has_cuts and method in GAIN_TOOLS:
            method = "volume" if family == "lossless" else "reencode"
        src = self.abs_path(media, rel)
        mode = cfg_m["writeMode"]
        if mode == "output":
            final = os.path.join(cfg_m["outputDir"], rel)
        else:
            final = src
        tmp = ff.temp_path(final)
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        applied = round(gain, 2) if abs(gain) >= 0.05 else 0.0
        gen = st["general"]
        prior_cover = cover.extract_cover(src) if media == "audio" and method not in GAIN_TOOLS else None
        try:
            if method in GAIN_TOOLS:
                steps = math.floor(gain / 1.5) if gain > 0 else round(gain / 1.5)
                if steps == 0:
                    self.db.execute("UPDATE files SET status = 'ok', gain_db = ? WHERE id = ?", (gain, fid))
                    return
                applied = round(steps * 1.5, 2)
                shutil.copy2(src, tmp)
                ff.gain_tool(getattr(self.cfg, method), tmp, steps, cancel)
            else:
                kbps = probe.kbps or DEFAULT_KBPS.get(family, 192)
                ff.transcode(self.cfg.ffmpeg, src, tmp, ext=ext, probe_=probe, family=family,
                             method=method, gain=applied, kbps=kbps, cancel=cancel,
                             cuts=cuts if has_cuts else None,
                             fade_in=gen.get("silenceFadeIn", 0.5),
                             fade_out=gen.get("silenceFadeOut", 0.5))
            expected_dur = sum(e - s for s, e, _, _ in kept) if has_cuts else None
            ff.verify(self.cfg.ffmpeg, tmp, probe, cancel, expected_duration=expected_dur)
            self._commit(src, tmp, final, media, rel, mode, st["general"]["preserveMtime"])
            if prior_cover and not cover.inspect_audio(final)["has_cover"]:
                try:
                    cover.embed_cover(final, prior_cover[0], mime=prior_cover[1],
                                      preserve_mtime=st["general"]["preserveMtime"])
                except Exception:
                    pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        now_ts = time.time()
        if has_cuts:
            removed = round(sum(ce - cs for cs, ce in cuts), 2)
            self.db.execute(
                "UPDATE files SET status = 'changed', applied_db = ?, gain_db = ?, method = ?, reason = NULL, "
                "measured_at = COALESCE(measured_at, ?), changed_at = ?, silence_trimmed = 1, "
                "silent_sections = ?, silent_duration = ? WHERE id = ?",
                (applied, gain, method, now_ts, now_ts, len(cuts), removed, fid))
            self.db.log("ok", f"{name}: changed by {applied:+.1f} dB ({method}), "
                              f"removed {len(cuts)} silent section(s) (-{removed:.1f} s)", media)
        else:
            self.db.execute(
                "UPDATE files SET status = 'changed', applied_db = ?, gain_db = ?, method = ?, reason = NULL, "
                "measured_at = COALESCE(measured_at, ?), changed_at = ? WHERE id = ?",
                (applied, gain, method, now_ts, now_ts, fid))
            self.db.log("ok", f"{name}: changed by {applied:+.1f} dB ({method})", media)

    def _commit(self, src: str, tmp: str, final: str, media: str, rel: str, mode: str, preserve: bool) -> None:
        st = os.stat(src)
        if mode == "backup":
            self._backup(src, media, rel)
        os.replace(tmp, final)
        if preserve:
            os.utime(final, ns=(st.st_atime_ns, st.st_mtime_ns))

    def _backup(self, src: str, media: str, rel: str) -> str:
        base = os.path.join(self.cfg.backup_dir, media, rel)
        os.makedirs(os.path.dirname(base), exist_ok=True)
        target, n = base, 1
        while os.path.exists(target):
            target = f"{base}.{n}"
            n += 1
        shutil.copy2(src, target)
        return target

    def _remove_stale_temp_files(self) -> None:
        for m in MEDIA:
            root = self.cfg.root(m)
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    if name.startswith(".") and ".nrm-tmp" in name:
                        try:
                            os.remove(os.path.join(dirpath, name))
                        except OSError:
                            pass

    # ------------------------------------------------------------ on demand

    # ------------------------------------------------------------ editing

    def edit_audio(self, media: str, rel: str, *, fade_in: float = 0.0, fade_out: float = 0.0,
                   cut: tuple[float, float] | None = None) -> dict:
        """Fade in/out or remove a part of one audio file.

        The same safety as the run: a temporary file in the same folder, verification, then an
        atomic replace. The write mode decides about a backup; the file is measured again in the
        next run, because fades and cuts change its level.
        """
        if media != "audio":
            raise ValueError("editing is available for audio files only")
        settings = state.get_settings(self.db)
        if settings["general"]["dryRun"]:
            raise ValueError("Dry run is on: turn it off in Settings to edit files")
        mode = settings["audio"]["writeMode"]
        if mode == "output":
            raise ValueError("Write mode is 'output folder': editing changes the file itself. "
                             "Choose in place (with or without backup) in Settings.")
        root = os.path.realpath(self.cfg.root(media))
        src = os.path.realpath(os.path.join(root, rel))
        if os.path.commonpath([root, src]) != root or not os.path.isfile(src):
            raise ValueError("file not found")
        row = self.db.one("SELECT * FROM files WHERE media = ? AND relpath = ?", (media, rel))
        if row is None:
            raise ValueError("file not found in the library; run a scan first")
        claimed = self.db.execute(
            "UPDATE files SET status = 'processing' WHERE id = ? AND status != 'processing'", (row["id"],))
        if not claimed:
            raise FileBusy("this file is being processed right now; try again when it is done")
        self._set_action_progress("edit_audio", "Editing audio", 0, 1, rel)
        try:
            out = self._edit_audio(src, media, rel, row, fade_in, fade_out, cut, mode,
                                   settings["general"]["preserveMtime"])
            self._set_action_progress("edit_audio", "Editing audio", 1, 1, rel)
            return out
        except BaseException:
            self.db.execute("UPDATE files SET status = ? WHERE id = ?", (row["status"], row["id"]))
            raise
        finally:
            self._clear_action_progress("edit_audio")

    def _edit_audio(self, src: str, media: str, rel: str, row: dict, fade_in: float, fade_out: float,
                    cut: tuple[float, float] | None, mode: str, preserve_mtime: bool) -> dict:
        cancel = self._new_cancel()
        probe = ff.probe(self.cfg.ffmpeg, src, cancel)
        duration = probe.duration
        if not duration or duration <= 0.1:
            raise ValueError("this file is too short to edit")
        fade_in = round(max(0.0, float(fade_in or 0.0)), 2)
        fade_out = round(max(0.0, float(fade_out or 0.0)), 2)
        fade_in = 0.0 if fade_in < 0.05 else fade_in
        fade_out = 0.0 if fade_out < 0.05 else fade_out
        cut_range = None
        if cut is not None:
            start, end = round(float(cut[0]), 2), round(float(cut[1]), 2)
            if not 0 <= start < end <= duration:
                raise ValueError("the part to remove must lie inside the file")
            if duration - (end - start) < 0.1:
                raise ValueError("the whole file would be removed")
            cut_range = (start, end)
        new_duration = duration - ((cut_range[1] - cut_range[0]) if cut_range else 0.0)
        if fade_in <= 0 and fade_out <= 0 and cut_range is None:
            raise ValueError("nothing to do: set a fade or select a part to remove")
        if fade_in > new_duration - 0.05 or fade_out > new_duration - 0.05:
            raise ValueError("a fade is longer than the file (after the cut)")
        if fade_in + fade_out > new_duration:
            raise ValueError("fade in and fade out overlap; shorten one of them")

        tmp = ff.temp_path(src)
        try:
            ff.edit(self.cfg.ffmpeg, src, tmp, probe_=probe, duration=duration,
                    cut=cut_range, fade_in=fade_in, fade_out=fade_out, new_duration=new_duration,
                    kbps=probe.kbps, cancel=cancel)
            ff.verify_edit(self.cfg.ffmpeg, tmp, probe, new_duration, cancel=cancel)
            self._commit(src, tmp, src, media, rel, mode, preserve_mtime)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        st = os.stat(src)
        self.db.execute(
            "UPDATE files SET status = 'pending', reason = NULL, size = ?, mtime_ns = ?, seen_at = ?, "
            "lufs = NULL, true_peak = NULL, measured_at = NULL, gain_db = NULL, adjust_db = 0, "
            "applied_db = NULL, method = NULL, changed_at = NULL, "
            "silent_sections = 0, silent_duration = 0, silence_trimmed = 0 WHERE id = ?",
            (st.st_size, st.st_mtime_ns, time.time(), row["id"]))
        what = []
        if fade_in:
            what.append(f"fade in {fade_in:.1f} s")
        if fade_out:
            what.append(f"fade out {fade_out:.1f} s")
        if cut_range:
            what.append(f"removed {_clock(cut_range[0])}–{_clock(cut_range[1])}")
        self.db.log("ok", f"Edited {row['name']}: {', '.join(what)} (now {_clock(new_duration)})", media)
        return {"ok": True, "name": row["name"], "relpath": rel, "duration": round(new_duration, 3),
                "size": st.st_size, "fade_in": fade_in, "fade_out": fade_out, "cut": list(cut_range or [])}

    def measure(self, media: str, rel: str, cancel=None) -> dict:
        """Level check for one file (baseline). The file is only read."""
        src = self.abs_path(media, rel)
        if not os.path.isfile(src):
            raise ValueError("file not found")
        standalone = not self.get_action_progress().get("active")
        if standalone:
            self._set_action_progress("measure_one", "Checking level", 0, 1, rel)
        cancel = cancel or self._new_cancel()
        try:
            st = state.get_settings(self.db)
            lufs, true_peak, probe, cuts = self._measure_and_detect(media, src, st["general"], cancel)
            name = os.path.basename(rel)
            cfg_m = st[media]
            family = audio_family(probe.codec)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            strategy = cfg_m.get("lossy", "reencode") if media == "audio" else "reencode"
            d = decide(media=media, ext=ext, family=family, measured=lufs, true_peak=true_peak,
                       target=cfg_m["target"], ceiling=cfg_m["truePeak"], threshold=cfg_m["threshold"],
                       override=cfg_m["override"], strategy=strategy, tools=self.tools,
                       hysteresis=cfg_m.get("hysteresis", 0.5))
            gain_db = 0.0 if d.action == "ok" else d.gain
            sil_count = len(cuts)
            sil_dur = round(sum(ce - cs for cs, ce in cuts), 2)
            self.db.execute(
                "UPDATE files SET lufs = ?, true_peak = ?, gain_db = ?, method = ?, "
                "silent_sections = ?, silent_duration = ?, measured_at = ?, codec = ?, family = ? "
                "WHERE media = ? AND relpath = ? AND status != 'processing'",
                (lufs, true_peak, gain_db, d.method, sil_count, sil_dur, time.time(),
                 probe.codec, family, media, rel),
            )
            if media == "audio":
                row = self.db.one("SELECT * FROM files WHERE media = ? AND relpath = ?", (media, rel))
                if row:
                    if st["general"].get("coverAuto") and not st["general"].get("dryRun"):
                        self.check_file_cover(row, st, online=True)
                    else:
                        self.inspect_file_cover(row)
            self.db.log("info", f"Level checked: {name} {lufs:.1f} LUFS, true peak {true_peak:.1f} dBTP"
                                + (f", {sil_count} empty section(s) ({sil_dur:.1f} s)" if sil_count else ""), media)
            if standalone:
                self._set_action_progress("measure_one", "Checking level", 1, 1, rel)
            return {"name": name, "relpath": rel, "lufs": lufs, "true_peak": true_peak,
                    "gain_db": gain_db, "silent_sections": sil_count, "silent_duration": sil_dur,
                    "codec": probe.codec}
        finally:
            if standalone:
                self._clear_action_progress("measure_one")

    def _target_rows(self, media: str, ids: list[int] | None, dir_rel: str | None) -> list[dict]:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            return self.db.query(
                f"SELECT * FROM files WHERE media = ? AND id IN ({placeholders}) ORDER BY name",
                [media, *ids],
            )
        if dir_rel is not None:
            return self.db.query(
                "SELECT * FROM files WHERE media = ? AND dir = ? ORDER BY name LIMIT 500",
                (media, dir_rel),
            )
        return []

    def check_files(self, media: str, ids: list[int] | None = None, dir_rel: str | None = None) -> dict:
        """Check level and empty sections for the selected files (or all files in dir_rel)."""
        cancel = self._new_cancel()
        rows = self._target_rows(media, ids, dir_rel)
        total = max(1, len(rows))
        self._set_action_progress("check_files", "Checking levels", 0, total, "")
        checked = 0
        empty = 0
        stopped = False
        try:
            for idx, r in enumerate(rows):
                if cancel():
                    stopped = True
                    break
                self._set_action_progress("check_files", "Checking levels", idx, total, r["relpath"])
                if r["status"] == "processing":
                    continue
                try:
                    res = self.measure(media, r["relpath"], cancel=cancel)
                    checked += 1
                    if res.get("silent_sections", 0) > 0:
                        empty += 1
                except ff.Cancelled:
                    stopped = True
                    break
                except Exception as exc:
                    self.db.log("error", f"{r['name']}: {exc}", media)
                self._set_action_progress("check_files", "Checking levels", idx + 1, total, r["relpath"])
        finally:
            self._clear_action_progress("check_files")
        out = {"checked": checked, "empty_sections": empty}
        if stopped:
            out["stopped"] = True
        return out

    def execute_files(self, media: str, ids: list[int] | None = None, dir_rel: str | None = None) -> dict:
        """Execute normalization (and empty-section removal when enabled) on the selected files."""
        cancel = self._new_cancel()
        rows = self._target_rows(media, ids, dir_rel)
        total = max(1, len(rows))
        self._set_action_progress("execute_files", "Executing normalization", 0, total, "")
        executed = 0
        changed = 0
        stopped = False
        try:
            for idx, r in enumerate(rows):
                if cancel():
                    stopped = True
                    break
                self._set_action_progress("execute_files", "Executing normalization", idx, total, r["relpath"])
                if r["status"] == "processing":
                    continue
                fid = r["id"]
                if r["status"] == "parked" and r["gain_db"] is not None:
                    self.db.execute("UPDATE files SET status = 'approved' WHERE id = ?", (fid,))
                else:
                    self.db.execute("UPDATE files SET status = 'pending' WHERE id = ?", (fid,))
                self._process(fid, extra_cancel=cancel)
                if cancel():
                    stopped = True
                    break
                executed += 1
                after = self.db.one("SELECT status FROM files WHERE id = ?", (fid,))
                if after and after["status"] == "changed":
                    changed += 1
                self._set_action_progress("execute_files", "Executing normalization", idx + 1, total, r["relpath"])
        finally:
            self._clear_action_progress("execute_files")
        out = {"executed": executed, "changed": changed}
        if stopped:
            out["stopped"] = True
        return out

    # ------------------------------------------------------------ cover art

    def inspect_file_cover(self, row: dict) -> dict:
        """Read local audio tags and embedded cover presence, resolving search metadata
        (including overriding ID3 tags with folder/file name when totally different)."""
        if row.get("media", "audio") != "audio":
            return {"has_cover": 0, "cover_status": None, "artist": "", "album": "", "title": "", "source": ""}
        src = self.abs_path("audio", row["relpath"])
        info = cover.inspect_audio(src)
        meta = cover.resolve_search_metadata(row["relpath"], info)
        has_cover = 1 if info["has_cover"] else 0
        prior_status = row.get("cover_status")
        if has_cover:
            c_status = "embedded" if prior_status == "embedded" else "ok"
            c_candidates = None
        else:
            c_status = prior_status if prior_status in ("choices", "not_found", "skipped") else "missing"
            c_candidates = row.get("cover_candidates") if c_status == "choices" else None
        self.db.execute(
            "UPDATE files SET has_cover = ?, cover_status = ?, cover_candidates = ?, "
            "tag_artist = ?, tag_album = ?, tag_title = ?, cover_query_source = ? WHERE id = ?",
            (has_cover, c_status, c_candidates, meta["artist"], meta["album"], meta["title"], meta["source"], row["id"]),
        )
        return {
            "has_cover": has_cover,
            "cover_status": c_status,
            **meta,
        }

    def _warm_audio_tags_chunk(self, rows_slice: list[dict]) -> None:
        if len(rows_slice) <= 1:
            return
        paths = [
            self.abs_path("audio", r["relpath"])
            for r in rows_slice
            if r.get("media", "audio") == "audio" and r.get("relpath")
        ]
        if len(paths) > 1:
            list(_TAG_WARM_POOL.map(cover.read_raw_tags_cached, paths))

    def _embed_into_row(self, row: dict, image_bytes: bytes, mime: str, st: dict) -> None:
        src = self.abs_path("audio", row["relpath"])
        if not os.path.isfile(src):
            raise ValueError("file not found on disk")
        if st["audio"]["writeMode"] == "backup":
            self._backup(src, "audio", row["relpath"])
        cover.embed_cover(src, image_bytes, mime=mime, preserve_mtime=st["general"].get("preserveMtime", True))
        now_ts = time.time()
        new_current = metadata.read_full_tags(src)
        with self.db.tx():
            self.db.execute(
                "UPDATE files SET has_cover = 1, cover_status = 'embedded', cover_candidates = NULL, cover_updated_at = ? WHERE id = ?",
                (now_ts, row["id"]),
            )
            mrow = self.db.one(
                "SELECT current_json, path_json, online_json, selected_json FROM file_metadata WHERE file_id = ?",
                (row["id"],),
            )
            if mrow:
                try:
                    opts = state.get_metadata_options(self.db)
                    path_meta = json.loads(mrow["path_json"] or "{}") or metadata.parse_path_full(row["relpath"])
                    online_meta = json.loads(mrow["online_json"] or "{}")
                    prior_sel = json.loads(mrow["selected_json"] or "{}")
                    if isinstance(prior_sel, dict):
                        prior_sel.pop("cover", None)
                    cmp = metadata.compare_file_metadata(row["relpath"], new_current, path_meta, online_meta, opts, prior_sel)
                    self.db.execute(
                        "UPDATE file_metadata SET status = ?, current_json = ?, selected_json = ?, diffs_json = ?, path_diff = ?, checked_at = ? WHERE file_id = ?",
                        (
                            cmp["status"],
                            json.dumps(new_current),
                            json.dumps(cmp["selected"]),
                            json.dumps(cmp["diffs"]),
                            cmp["path_diff"],
                            now_ts,
                            row["id"],
                        ),
                    )
                except Exception:
                    pass

    def check_file_cover(self, row: dict, st: dict | None = None, *,
                         online: bool = True, force_refresh: bool = False) -> dict:
        """Inspect a file's cover; if missing and online=True, search Discogs/open sources.
        Single match -> auto-embed; multiple matches -> park in cover_status='choices'."""
        if row["media"] != "audio":
            return {"status": "unsupported", "has_cover": 0, "candidates": []}
        inspected = self.inspect_file_cover(row)
        if inspected["has_cover"] and not force_refresh:
            return {"status": inspected["cover_status"], "has_cover": 1, "candidates": [], **inspected}
        if not online:
            return {"status": inspected["cover_status"], "has_cover": 0, "candidates": [], **inspected}

        st = st or state.get_settings(self.db)
        gen = st["general"]
        src = self.abs_path("audio", row["relpath"])

        if gen.get("useLocalCover", True) and not force_refresh:
            local = cover.find_local_cover(src)
            if local:
                if not gen.get("dryRun", False):
                    data, mime = cover.download_cover(local["image_url"])
                    self._embed_into_row(row, data, mime, st)
                    self.db.log("ok", f"{row['name']}: embedded cover art from {local['title']}", "audio")
                    return {"status": "embedded", "has_cover": 1, "candidates": [local], **inspected}

        sf = state.get_metadata_options(self.db).get("search_from") or "auto"
        if sf == "file":
            q_art, q_alb, q_tit = inspected["path_artist"], inspected["path_album"], inspected["path_title"]
        elif sf == "metadata":
            raw_info = cover.inspect_audio(src)
            q_art = cover._clean_str(raw_info.get("artist") or raw_info.get("album_artist")) or inspected["tag_artist"]
            q_alb = cover._clean_str(raw_info.get("album")) or inspected["tag_album"]
            q_tit = cover._clean_str(raw_info.get("title")) or inspected["tag_title"]
        else:
            q_art, q_alb, q_tit = inspected["artist"], inspected["album"], inspected["title"]

        candidates = cover.search_covers(
            q_art, q_alb, q_tit,
            st, db=self.db, force_refresh=force_refresh,
        )
        if len(candidates) == 1:
            cand = candidates[0]
            if gen.get("dryRun", False):
                self.db.log("info", f"{row['name']}: dry run, would embed cover '{cand['title']}' ({cand['source']})", "audio")
                return {"status": "dryrun", "has_cover": 0, "candidates": candidates, **inspected}
            try:
                data, mime = cover.download_cover(cand["image_url"], token=gen.get("discogsToken", ""))
                self._embed_into_row(row, data, mime, st)
                self.db.log("ok", f"{row['name']}: embedded cover art '{cand['title']}' ({cand['source']})", "audio")
                return {"status": "embedded", "has_cover": 1, "candidates": candidates, **inspected}
            except Exception as exc:
                self.db.log("error", f"{row['name']}: cover download failed ({exc})", "audio")
                return {"status": "error", "has_cover": 0, "candidates": candidates, **inspected}

        if len(candidates) > 1:
            self.db.execute(
                "UPDATE files SET has_cover = 0, cover_status = 'choices', cover_candidates = ? WHERE id = ?",
                (json.dumps(candidates), row["id"]),
            )
            self.db.log(
                "warn",
                f"{row['name']}: {len(candidates)} cover pictures found, parked for cover selection",
                "audio",
            )
            return {"status": "choices", "has_cover": 0, "candidates": candidates, **inspected}

        self.db.execute(
            "UPDATE files SET has_cover = 0, cover_status = 'not_found', cover_candidates = '[]' WHERE id = ?",
            (row["id"],),
        )
        return {"status": "not_found", "has_cover": 0, "candidates": [], **inspected}

    def search_file_covers(self, fid: int, *, artist: str | None = None, album: str | None = None,
                           title: str | None = None, source_mode: str | None = None,
                           query_mode: str | None = None, force_refresh: bool = False) -> dict:
        """Interactive cover search for a file or album (used by the Cover Picker modal)."""
        row = self.db.one("SELECT * FROM files WHERE id = ?", (fid,))
        if not row or row["media"] != "audio":
            raise ValueError("audio file not found")
        src = self.abs_path("audio", row["relpath"])
        inspected = self.inspect_file_cover(row)
        raw_info = cover.inspect_audio(src)
        meta_artist = cover._clean_str(raw_info.get("artist") or raw_info.get("album_artist")) or inspected["tag_artist"]
        meta_album = cover._clean_str(raw_info.get("album")) or inspected["tag_album"]
        meta_title = cover._clean_str(raw_info.get("title")) or inspected["tag_title"]

        eff_mode = query_mode if query_mode in ("auto", "file", "metadata") else (state.get_metadata_options(self.db).get("search_from") or "auto")
        if eff_mode == "file":
            base_artist = inspected["path_artist"]
            base_album = inspected["path_album"]
            base_title = inspected["path_title"]
            eff_query_source = "file"
        elif eff_mode == "metadata":
            base_artist = meta_artist
            base_album = meta_album
            base_title = meta_title
            eff_query_source = "metadata"
        else:
            base_artist = inspected["artist"]
            base_album = inspected["album"]
            base_title = inspected["title"]
            eff_query_source = inspected["source"]

        q_artist = base_artist if artist is None else artist.strip()
        q_album = base_album if album is None else album.strip()
        q_title = base_title if title is None else title.strip()

        st = state.get_settings(self.db)
        if source_mode in ("both", "discogs", "open"):
            st = {**st, "general": {**st["general"], "coverSources": source_mode}}

        candidates = []
        local = cover.find_local_cover(src)
        if local:
            candidates.append(local)
        online = cover.search_covers(q_artist, q_album, q_title, st, db=self.db, force_refresh=force_refresh)
        seen_urls = {c["image_url"] for c in candidates}
        for c in online:
            if c["image_url"] not in seen_urls:
                candidates.append(c)
                seen_urls.add(c["image_url"])

        if not inspected["has_cover"]:
            new_status = "choices" if len(candidates) > 1 else ("not_found" if not candidates else row.get("cover_status") or "missing")
            self.db.execute(
                "UPDATE files SET cover_status = ?, cover_candidates = ?, tag_artist = ?, tag_album = ?, tag_title = ?, cover_query_source = ? WHERE id = ?",
                (new_status, json.dumps(candidates) if candidates else "[]", q_artist, q_album, q_title, eff_query_source, fid),
            )
        # Count how many files belong to the same album/folder for the "Apply to album" button
        album_rows = self._album_rows_for(row, q_album)
        return {
            "file_id": fid,
            "name": row["name"],
            "relpath": row["relpath"],
            "dir": row["dir"],
            "has_cover": inspected["has_cover"],
            "cover_updated_at": row.get("cover_updated_at"),
            "cover_status": row.get("cover_status"),
            "artist": q_artist,
            "album": q_album,
            "title": q_title,
            "query_mode": eff_mode,
            "query_source": eff_query_source,
            "tag_artist": meta_artist,
            "tag_album": meta_album,
            "tag_title": meta_title,
            "path_artist": inspected["path_artist"],
            "path_album": inspected["path_album"],
            "path_title": inspected["path_title"],
            "album_file_count": len(album_rows),
            "candidates": candidates,
        }

    def _album_rows_for(self, row: dict, album_override: str | None = None) -> list[dict]:
        """Return all audio files in the same album (same folder + matching album or all in folder)."""
        album = (album_override if album_override is not None else (row.get("tag_album") or "")).strip()
        folder_rows = self.db.query(
            "SELECT * FROM files WHERE media = 'audio' AND dir = ? ORDER BY name",
            (row["dir"],),
        )
        if not album or len(folder_rows) <= 1:
            return folder_rows
        matching = [r for r in folder_rows if (r.get("tag_album") or "").strip().lower() == album.lower()]
        return matching if len(matching) >= 2 else folder_rows

    def apply_cover(self, fid: int, image_url: str, scope: str = "file",
                    target_ids: list[int] | None = None, only_missing: bool = False) -> dict:
        """Download the selected cover picture and embed it into the file, album, or folder."""
        row = self.db.one("SELECT * FROM files WHERE id = ?", (fid,))
        if not row or row["media"] != "audio":
            raise ValueError("audio file not found")
        st = state.get_settings(self.db)
        if st["general"].get("dryRun"):
            raise ValueError("Dry run is on: turn it off in Settings to write cover art")
        data, mime = cover.download_cover(image_url, token=st["general"].get("discogsToken", ""))

        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            targets = self.db.query(
                f"SELECT * FROM files WHERE media = 'audio' AND id IN ({placeholders}) ORDER BY name",
                target_ids,
            )
        elif scope in ("album", "dir"):
            targets = (
                self.db.query("SELECT * FROM files WHERE media = 'audio' AND dir = ? ORDER BY name", (row["dir"],))
                if scope == "dir"
                else self._album_rows_for(row)
            )
        else:
            targets = [row]

        if only_missing and len(targets) > 1:
            targets = [r for r in targets if not r.get("has_cover")]

        total = max(1, len(targets))
        self._set_action_progress("apply_cover", "Embedding cover art", 0, total, "")
        updated = 0
        try:
            for idx, r in enumerate(targets):
                self._set_action_progress("apply_cover", "Embedding cover art", idx, total, r["relpath"])
                if r["status"] == "processing":
                    continue
                try:
                    self._embed_into_row(r, data, mime, st)
                    updated += 1
                except Exception as exc:
                    self.db.log("error", f"{r['name']}: failed to embed cover ({exc})", "audio")
                self._set_action_progress("apply_cover", "Embedding cover art", idx + 1, total, r["relpath"])
        finally:
            self._clear_action_progress("apply_cover")

        label = row["name"] if len(targets) == 1 else f"{row['dir']} ({updated} files)"
        if updated:
            self.db.log("ok", f"Cover art embedded: {label}", "audio")
        return {"ok": True, "updated": updated, "scope": scope}

    def skip_cover(self, ids: list[int]) -> dict:
        if not ids:
            return {"skipped": 0}
        placeholders = ",".join("?" for _ in ids)
        n = self.db.execute(
            f"UPDATE files SET cover_status = 'skipped', cover_candidates = NULL "
            f"WHERE media = 'audio' AND id IN ({placeholders})",
            ids,
        )
        return {"skipped": n}

    def check_covers_batch(self, ids: list[int] | None = None, dir_rel: str | None = None,
                           enabled_only: bool = False) -> dict:
        """Check and propose/embed missing cover art for selected files, a folder, or all enabled folders."""
        st = state.get_settings(self.db)
        if ids:
            rows = self._target_rows("audio", ids, None)
        elif dir_rel is not None:
            rows = self._target_rows("audio", None, dir_rel)
        elif enabled_only:
            rows = self.db.query(
                "SELECT f.* FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
                "WHERE d.media = 'audio' AND d.enabled = 1 ORDER BY f.dir, f.name"
            )
        else:
            rows = []

        checked = embedded = choices = not_found = present = 0
        cancel = self._new_cancel()
        stopped = False
        total = max(1, len(rows))
        self._set_action_progress("check_covers", "Checking cover art", 0, total, "")
        try:
            for idx, r in enumerate(rows):
                if cancel():
                    stopped = True
                    break
                if idx % _TAG_WARM_CHUNK == 0:
                    self._warm_audio_tags_chunk(rows[idx: idx + _TAG_WARM_CHUNK])
                self._set_action_progress("check_covers", "Checking cover art", idx, total, r["relpath"])
                if r["status"] == "processing":
                    continue
                try:
                    res = self.check_file_cover(r, st, online=True)
                    checked += 1
                    st_code = res.get("status")
                    if st_code == "ok":
                        present += 1
                    elif st_code == "embedded":
                        embedded += 1
                    elif st_code == "choices":
                        choices += 1
                    elif st_code == "not_found":
                        not_found += 1
                except Exception as exc:
                    self.db.log("error", f"{r['name']}: cover check failed ({exc})", "audio")
                self._set_action_progress("check_covers", "Checking cover art", idx + 1, total, r["relpath"])
        finally:
            self._clear_action_progress("check_covers")
        return {
            "checked": checked,
            "present": present,
            "embedded": embedded,
            "choices": choices,
            "not_found": not_found,
            "stopped": stopped,
        }

    def apply_first_cover_all(self, ids: list[int] | None = None) -> dict:
        """Bulk-apply the primary (first) candidate cover to all or selected multi-choice files."""
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = self.db.query(
                f"SELECT * FROM files WHERE media = 'audio' AND cover_status = 'choices' AND id IN ({placeholders}) ORDER BY dir, name",
                ids,
            )
        else:
            rows = self.db.query(
                "SELECT f.* FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
                "WHERE d.media = 'audio' AND d.enabled = 1 AND f.cover_status = 'choices' ORDER BY f.dir, f.name"
            )
        st = state.get_settings(self.db)
        if st["general"].get("dryRun"):
            raise ValueError("Dry run is on: turn it off in Settings to write cover art")
        downloaded: dict[str, tuple[bytes, str]] = {}
        updated = 0
        cancel = self._new_cancel()
        stopped = False
        total = max(1, len(rows))
        self._set_action_progress("apply_first_cover", "Embedding cover art", 0, total, "")
        try:
            for idx, r in enumerate(rows):
                if cancel():
                    stopped = True
                    break
                self._set_action_progress("apply_first_cover", "Embedding cover art", idx, total, r["relpath"])
                if r["status"] == "processing":
                    continue
                try:
                    cands = json.loads(r.get("cover_candidates") or "[]")
                except Exception:
                    cands = []
                if not cands:
                    continue
                url = cands[0].get("image_url")
                if not url:
                    continue
                try:
                    if url not in downloaded:
                        downloaded[url] = cover.download_cover(url, token=st["general"].get("discogsToken", ""))
                    data, mime = downloaded[url]
                    self._embed_into_row(r, data, mime, st)
                    updated += 1
                except Exception as exc:
                    self.db.log("error", f"{r['name']}: failed to embed cover ({exc})", "audio")
                self._set_action_progress("apply_first_cover", "Embedding cover art", idx + 1, total, r["relpath"])
        finally:
            self._clear_action_progress("apply_first_cover")
        if updated:
            self.db.log("ok", f"Bulk embedded cover art into {updated} file(s)", "audio")
        return {"updated": updated}

    # ------------------------------------------------------------ full metadata check & update

    def is_metadata_checking(self) -> bool:
        with self._meta_lock:
            return self._meta_checking

    def get_metadata_progress(self) -> dict:
        with self._meta_lock:
            snap = dict(self._meta_progress)
            snap["running"] = self._meta_checking
            return snap

    def stop_metadata_check(self) -> dict:
        self._meta_stop.set()
        with self._meta_lock:
            was_running = self._meta_checking
        return {"ok": True, "stopping": was_running}

    def start_metadata_check(self, ids: list[int] | None = None, dir_rel: str | None = None,
                             options: dict | None = None, force: bool = False) -> dict:
        """Start a metadata batch check in a background thread so intermediate results stream live."""
        if options:
            state.put_metadata_options(self.db, options)
        with self._meta_lock:
            if self._meta_checking:
                snap = dict(self._meta_progress)
                snap["running"] = True
                snap["already_running"] = True
                return snap
            self._meta_stop.clear()
            self._meta_checking = True
            self._meta_progress = {
                "running": True,
                "force": bool(force),
                "total": 0,
                "done": 0,
                "diff": 0,
                "ok": 0,
                "path_diff": 0,
                "current_file": "",
                "stopped": False,
                "started_at": time.time(),
                "finished_at": None,
                "seq": int(self._meta_progress.get("seq", 0)) + 1,
            }

            def _worker():
                try:
                    self._run_metadata_batch(ids=ids, dir_rel=dir_rel, options=None, force=force)
                except Exception as exc:
                    log.exception("background metadata check failed")
                    self.db.log("error", f"Metadata check failed: {exc}", "audio")
                    with self._meta_lock:
                        self._meta_checking = False
                        self._meta_progress["running"] = False
                        self._meta_progress["finished_at"] = time.time()
                        self._meta_progress["seq"] = int(self._meta_progress.get("seq", 0)) + 1

            t = threading.Thread(target=_worker, name="metadata-checker", daemon=True)
            self._meta_thread = t
            t.start()
            return {"started": True, **dict(self._meta_progress)}

    def check_file_metadata(self, row: dict, options: dict | None = None, st: dict | None = None, *,
                            force_refresh: bool = False, custom_query: dict | None = None,
                            query_mode: str | None = None, cancel=None) -> dict:
        """Check all enabled metadata fields for one audio file against folder/file name and internet sources."""
        if row.get("media", "audio") != "audio":
            raise ValueError("metadata checking is available for audio files only")
        src = self.abs_path("audio", row["relpath"])
        if not os.path.isfile(src):
            raise ValueError("audio file not found on disk")

        opts = options or state.get_metadata_options(self.db)
        st = st or state.get_settings(self.db)

        current = metadata.read_full_tags(src)
        path_meta = metadata.parse_path_full(row["relpath"])
        local_cov = cover.find_local_cover(src)
        if local_cov and local_cov.get("image_url"):
            path_meta["cover"] = local_cov["image_url"]
        resolved = cover.resolve_search_metadata(row["relpath"], current)

        eff_mode = query_mode if query_mode in ("auto", "file", "metadata") else (opts.get("search_from") or "auto")
        if eff_mode == "file":
            base_artist = path_meta.get("artist") or ""
            base_album = path_meta.get("album") or ""
            base_title = path_meta.get("title") or ""
            q_src = "file"
        elif eff_mode == "metadata":
            base_artist = current.get("artist") or current.get("album_artist") or ""
            base_album = current.get("album") or ""
            base_title = current.get("title") or ""
            q_src = "metadata"
        else:
            base_artist = resolved["artist"] or ""
            base_album = resolved["album"] or ""
            base_title = resolved["title"] or ""
            q_src = resolved["source"]

        q_artist = (custom_query.get("artist") if custom_query and "artist" in custom_query else base_artist) or ""
        q_album = (custom_query.get("album") if custom_query and "album" in custom_query else base_album) or ""
        q_title = (custom_query.get("title") if custom_query and "title" in custom_query else base_title) or ""
        q_track = path_meta.get("track") or current.get("track") or ""

        lookup_kwargs: dict = {"db": self.db, "force_refresh": force_refresh}
        if cancel is not None and "cancel" in inspect.signature(metadata.lookup_online_metadata).parameters:
            lookup_kwargs["cancel"] = cancel

        online_meta = metadata.lookup_online_metadata(
            q_artist, q_album, q_title, q_track,
            opts, st, **lookup_kwargs,
        )
        if cancel and cancel():
            raise ff.Cancelled("metadata check stopped")

        now_ts = time.time()
        has_cov = 1 if current.get("cover") == "present" else 0
        with self._meta_select_lock:
            existing_sel: dict | None = None
            if not force_refresh and not custom_query:
                prev = self.db.one(
                    "SELECT m.selected_json, f.cover_query_source "
                    "FROM file_metadata m JOIN files f ON f.id = m.file_id WHERE m.file_id = ?",
                    (row["id"],),
                )
                if prev and prev.get("selected_json"):
                    prev_q_src = prev.get("cover_query_source") or ""
                    if not query_mode or not prev_q_src or prev_q_src == q_src:
                        try:
                            existing_sel = json.loads(prev["selected_json"] or "{}")
                        except Exception:
                            existing_sel = None
            cmp = metadata.compare_file_metadata(
                row["relpath"], current, path_meta, online_meta, opts, existing_sel
            )
            with self.db.tx():
                self.db.execute(
                    "INSERT INTO file_metadata (file_id, status, current_json, path_json, online_json, selected_json, diffs_json, path_diff, checked_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(file_id) DO UPDATE SET status = excluded.status, current_json = excluded.current_json, "
                    "path_json = excluded.path_json, online_json = excluded.online_json, selected_json = excluded.selected_json, "
                    "diffs_json = excluded.diffs_json, path_diff = excluded.path_diff, checked_at = excluded.checked_at",
                    (
                        row["id"],
                        cmp["status"],
                        json.dumps(current),
                        json.dumps(path_meta),
                        json.dumps(online_meta),
                        json.dumps(cmp["selected"]),
                        json.dumps(cmp["diffs"]),
                        cmp["path_diff"],
                        now_ts,
                    ),
                )
                self.db.execute(
                    "UPDATE files SET has_cover = ?, tag_artist = ?, tag_album = ?, tag_title = ?, cover_query_source = ? WHERE id = ?",
                    (has_cov, q_artist or resolved["artist"], q_album or resolved["album"], q_title or resolved["title"], q_src, row["id"]),
                )
        album_rows = self._album_rows_for(row, q_album)
        return {
            "id": row["id"],
            "name": row["name"],
            "relpath": row["relpath"],
            "dir": row["dir"],
            "ext": row["ext"],
            "has_cover": has_cov,
            "cover_updated_at": row.get("cover_updated_at") or now_ts,
            "status": cmp["status"],
            "diffs": cmp["diffs"],
            "diff_count": len(cmp["diffs"]),
            "path_diff": bool(cmp["path_diff"]),
            "path_diff_count": len(cmp["path_mismatches"]),
            "path_mismatches": cmp["path_mismatches"],
            "query_source": q_src,
            "query_mode": eff_mode,
            "online_source": online_meta.get("source") or "",
            "online_score": online_meta.get("score") or 0,
            "current": current,
            "path": path_meta,
            "online": online_meta,
            "selected": cmp["selected"],
            "fields": cmp["fields"],
            "album_file_count": len(album_rows),
            "checked_at": now_ts,
        }

    def check_metadata_batch(self, ids: list[int] | None = None, dir_rel: str | None = None,
                             options: dict | None = None, *, force: bool = False) -> dict:
        """Run metadata check synchronously across selected files, a directory, or all enabled audio directories."""
        with self._meta_lock:
            if self._meta_checking:
                return {"checked": 0, "diff": 0, "ok": 0, "path_diff": 0, "stopped": False, "already_running": True}
            self._meta_stop.clear()
            self._meta_checking = True
            self._meta_progress = {
                "running": True,
                "force": bool(force),
                "total": 0,
                "done": 0,
                "diff": 0,
                "ok": 0,
                "path_diff": 0,
                "current_file": "",
                "stopped": False,
                "started_at": time.time(),
                "finished_at": None,
                "seq": int(self._meta_progress.get("seq", 0)) + 1,
            }
        return self._run_metadata_batch(ids=ids, dir_rel=dir_rel, options=options, force=force)

    def _run_metadata_batch(self, ids: list[int] | None = None, dir_rel: str | None = None,
                            options: dict | None = None, *, force: bool = False) -> dict:
        if options:
            opts = state.put_metadata_options(self.db, options)
        else:
            opts = state.get_metadata_options(self.db)
        st = state.get_settings(self.db)

        if ids:
            rows = self._target_rows("audio", ids, None)
        elif dir_rel is not None:
            rows = self._target_rows("audio", None, dir_rel)
        else:
            all_rows = self.db.query(
                "SELECT f.*, m.status AS _meta_status "
                "FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
                "LEFT JOIN file_metadata m ON m.file_id = f.id "
                "WHERE d.media = 'audio' AND d.enabled = 1 ORDER BY f.dir, f.name"
            )
            if not force:
                sf = opts.get("search_from") or "auto"
                unchecked_rows = [
                    r for r in all_rows
                    if not r.get("_meta_status")
                    or r.get("_meta_status") == "unchecked"
                    or (sf in ("file", "metadata") and r.get("cover_query_source") != sf)
                ]
                rows = unchecked_rows if unchecked_rows else all_rows
            else:
                rows = all_rows

        total = len(rows)
        with self._meta_lock:
            self._meta_progress["total"] = total
            self._meta_progress["seq"] = int(self._meta_progress.get("seq", 0)) + 1

        checked = diff = ok = path_diff = 0
        stopped = False
        try:
            for idx, r in enumerate(rows):
                if self._meta_stop.is_set() or self._stop.is_set():
                    stopped = True
                    break
                if idx % _TAG_WARM_CHUNK == 0:
                    self._warm_audio_tags_chunk(rows[idx: idx + _TAG_WARM_CHUNK])
                if r["status"] == "processing":
                    continue
                with self._meta_lock:
                    self._meta_progress["current_file"] = r["relpath"]
                try:
                    res = self.check_file_metadata(
                        r, opts, st, force_refresh=force, cancel=self._meta_stop.is_set
                    )
                    if self._meta_stop.is_set() or self._stop.is_set():
                        stopped = True
                        break
                    checked += 1
                    if res["status"] == "diff":
                        diff += 1
                    else:
                        ok += 1
                    if res["path_diff"]:
                        path_diff += 1
                    with self._meta_lock:
                        self._meta_progress["done"] = checked
                        self._meta_progress["diff"] = diff
                        self._meta_progress["ok"] = ok
                        self._meta_progress["path_diff"] = path_diff
                        self._meta_progress["seq"] = int(self._meta_progress.get("seq", 0)) + 1
                except ff.Cancelled:
                    stopped = True
                    break
                except Exception as exc:
                    self.db.log("error", f"{r['name']}: metadata check failed ({exc})", "audio")
                    with self._meta_lock:
                        self._meta_progress["seq"] = int(self._meta_progress.get("seq", 0)) + 1
        finally:
            with self._meta_lock:
                self._meta_checking = False
                self._meta_progress["running"] = False
                self._meta_progress["current_file"] = ""
                self._meta_progress["stopped"] = stopped
                self._meta_progress["finished_at"] = time.time()
                self._meta_progress["seq"] = int(self._meta_progress.get("seq", 0)) + 1

        if stopped:
            self.db.log("info", f"Meta data check stopped: {checked} file(s) checked, {diff} with differences", "audio")
        else:
            self.db.log("info", f"Meta data checked: {checked} file(s), {diff} with differences", "audio")
        return {"checked": checked, "diff": diff, "ok": ok, "path_diff": path_diff, "stopped": stopped}

    def get_metadata_results(self) -> dict:
        """Return current metadata check options, summary counts, and comparison rows for enabled audio folders."""
        opts = state.get_metadata_options(self.db)
        rows = self.db.query(
            "SELECT f.id, f.name, f.relpath, f.dir, f.ext, f.has_cover, f.cover_updated_at, f.cover_query_source, "
            "m.status AS meta_status, m.current_json, m.path_json, m.online_json, "
            "m.selected_json, m.diffs_json, m.path_diff, m.checked_at "
            "FROM dirs d CROSS JOIN files f ON f.media = d.media AND f.dir = d.relpath "
            "LEFT JOIN file_metadata m ON m.file_id = f.id "
            "WHERE d.media = 'audio' AND d.enabled = 1 ORDER BY f.dir, f.name"
        )
        items: list[dict] = []
        folders_set: set[str] = set()
        total_in_scope = len(rows)
        checked = diff = ok = path_diff = 0

        for r in rows:
            folders_set.add(r["dir"])
            if not r["meta_status"]:
                continue
            checked += 1
            try:
                current = json.loads(r["current_json"] or "{}")
                path_meta = json.loads(r["path_json"] or "{}")
                online_meta = json.loads(r["online_json"] or "{}")
                existing_sel = json.loads(r["selected_json"] or "{}")
            except Exception:
                continue
            cmp = metadata.compare_file_metadata(r["relpath"], current, path_meta, online_meta, opts, existing_sel)
            if cmp["status"] == "diff":
                diff += 1
            else:
                ok += 1
            if cmp["path_diff"]:
                path_diff += 1
            has_cov = 1 if current.get("cover") == "present" else int(r.get("has_cover") or 0)
            items.append({
                "id": r["id"],
                "name": r["name"],
                "relpath": r["relpath"],
                "dir": r["dir"],
                "ext": r["ext"],
                "has_cover": has_cov,
                "cover_updated_at": r.get("cover_updated_at") or r["checked_at"],
                "status": cmp["status"],
                "diffs": cmp["diffs"],
                "diff_count": len(cmp["diffs"]),
                "path_diff": bool(cmp["path_diff"]),
                "path_diff_count": len(cmp["path_mismatches"]),
                "path_mismatches": cmp["path_mismatches"],
                "query_source": r.get("cover_query_source") or cmp.get("query_source") or "tags",
                "online_source": online_meta.get("source") or "",
                "online_score": online_meta.get("score") or 0,
                "current": current,
                "path": path_meta,
                "online": online_meta,
                "selected": cmp["selected"],
                "fields": cmp["fields"],
                "checked_at": r["checked_at"],
            })

        dirs_stat = self.db.one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(enabled), 0) AS enabled "
            "FROM dirs WHERE media = 'audio' AND relpath != '.'"
        )
        enabled_dirs = dirs_stat["enabled"] if dirs_stat else 0
        total_dirs = dirs_stat["total"] if dirs_stat else 0
        return {
            "options": opts,
            "meta_checking": self.is_metadata_checking(),
            "meta_progress": self.get_metadata_progress(),
            "files": items,
            "folders": sorted(folders_set),
            "enabled_folders": enabled_dirs,
            "total_folders": total_dirs,
            "total_files_in_scope": total_in_scope,
            "checked_count": checked,
            "diff_count": diff,
            "path_diff_count": path_diff,
            "counts": {
                "total_in_scope": total_in_scope,
                "checked": checked,
                "diff": diff,
                "ok": ok,
                "path_diff": path_diff,
                "directories_enabled": enabled_dirs,
                "directories_total": total_dirs,
            },
        }

    def select_metadata_choice(self, ids: list[int], field: str | None = None,
                               choice: str = "online", custom_value: str | None = None,
                               updates_map: dict[str, Any] | None = None) -> dict:
        """Update the chosen source ('current', 'path', 'discogs', 'musicbrainz', 'open', 'online', 'custom')
        for one field, multiple fields (custom or source-mode), or all fields, keeping file_metadata in sync."""
        if not ids:
            return {"updated": 0, "files_updated": {}}
        if field == "all":
            field = None
        with self._meta_select_lock:
            opts = state.get_metadata_options(self.db)
            placeholders = ",".join("?" for _ in ids)
            rows = self.db.query(
                f"SELECT f.id AS file_id, f.relpath, m.current_json, m.path_json, m.online_json, m.selected_json, m.diffs_json "
                f"FROM files f JOIN file_metadata m ON m.file_id = f.id "
                f"WHERE f.id IN ({placeholders})",
                ids,
            )

            def _apply_one_field(selected_dict: dict, f_key: str, f_choice: str, f_custom: str | None,
                                 cur_d: dict, path_d: dict, online_d: dict, by_src_d: dict) -> None:
                if f_key not in state.METADATA_FIELDS:
                    return
                if f_choice == "custom":
                    val = str(f_custom if f_custom is not None else "").strip()
                    selected_dict[f_key] = {"choice": "custom", "value": val}
                elif f_choice == "current":
                    selected_dict[f_key] = {"choice": "current", "value": str(cur_d.get(f_key) or "").strip()}
                elif f_choice == "path":
                    p_val = str(path_d.get(f_key) or "").strip()
                    if p_val:
                        selected_dict[f_key] = {"choice": "path", "value": p_val}
                elif f_choice in ("discogs", "musicbrainz", "open"):
                    s_val = str(by_src_d[f_choice].get(f_key) or "").strip()
                    if s_val:
                        selected_dict[f_key] = {"choice": f_choice, "value": s_val}
                elif f_choice == "online":
                    d_val = str(by_src_d["discogs"].get(f_key) or "").strip()
                    m_val = str(by_src_d["musicbrainz"].get(f_key) or "").strip()
                    i_val = str(by_src_d["open"].get(f_key) or "").strip()
                    o_val = d_val or m_val or i_val or str(online_d.get(f_key) or "").strip()
                    picked_src = (
                        "discogs" if d_val else ("musicbrainz" if m_val else ("open" if i_val else "online"))
                    )
                    if o_val:
                        selected_dict[f_key] = {"choice": picked_src, "value": o_val}

            updated = 0
            files_updated: dict[str, dict[str, Any]] = {}
            with self.db.tx():
                for r in rows:
                    try:
                        current = json.loads(r["current_json"] or "{}")
                        path_meta = json.loads(r["path_json"] or "{}")
                        online_meta = json.loads(r["online_json"] or "{}")
                        selected = json.loads(r["selected_json"] or "{}")
                    except Exception:
                        continue

                    by_src = metadata.extract_online_by_source(online_meta)

                    if updates_map and isinstance(updates_map, dict):
                        for fk, fspec in updates_map.items():
                            if isinstance(fspec, dict):
                                m_choice = str(fspec.get("choice") or "custom").strip()
                                m_val = fspec.get("value")
                                _apply_one_field(selected, fk, m_choice, m_val, current, path_meta, online_meta, by_src)
                            else:
                                _apply_one_field(selected, fk, "custom", str(fspec if fspec is not None else ""), current, path_meta, online_meta, by_src)
                    else:
                        target_fields = [field] if field else list(opts.get("fields") or state.METADATA_FIELDS)
                        for f in target_fields:
                            _apply_one_field(selected, f, choice, custom_value, current, path_meta, online_meta, by_src)

                    cmp = metadata.compare_file_metadata(r["relpath"], current, path_meta, online_meta, opts, selected)
                    self.db.execute(
                        "UPDATE file_metadata SET status = ?, selected_json = ?, diffs_json = ?, path_diff = ? WHERE file_id = ?",
                        (cmp["status"], json.dumps(cmp["selected"]), json.dumps(cmp["diffs"]), cmp["path_diff"], r["file_id"]),
                    )
                    files_updated[str(r["file_id"])] = {
                        "status": cmp["status"],
                        "diffs": cmp["diffs"],
                        "diff_count": len(cmp["diffs"]),
                        "path_diff": bool(cmp["path_diff"]),
                        "path_diff_count": len(cmp["path_mismatches"]),
                        "path_mismatches": cmp["path_mismatches"],
                        "selected": cmp["selected"],
                        "fields": cmp["fields"],
                    }
                    updated += 1
            return {"updated": updated, "files_updated": files_updated}

    def apply_metadata(self, ids: list[int], field_overrides: dict | None = None,
                       scope: str = "file", only_fields: list[str] | None = None) -> dict:
        """Write selected metadata changes to the specified audio file(s) (or whole album),
        strictly preserving each file's timestamp on disk and syncing both files & file_metadata tables."""
        if not ids:
            return {"updated": 0, "errors": []}
        st = state.get_settings(self.db)
        if st["general"].get("dryRun"):
            raise ValueError("Dry run is on: turn it off in Settings to update metadata")
        opts = state.get_metadata_options(self.db)
        enabled_fields = opts.get("fields_map") or {f: True for f in state.METADATA_FIELDS}
        token = st["general"].get("discogsToken", "")
        allowed_fields = {f for f in only_fields if f in state.METADATA_FIELDS} if only_fields else None

        if scope == "album" and len(ids) == 1:
            first_row = self.db.one("SELECT * FROM files WHERE id = ?", (ids[0],))
            if first_row:
                album_rows = self._album_rows_for(first_row)
                ids = [r["id"] for r in album_rows]

        placeholders = ",".join("?" for _ in ids)
        rows = self.db.query(
            f"SELECT f.*, m.current_json, m.path_json, m.online_json, m.selected_json "
            f"FROM files f LEFT JOIN file_metadata m ON m.file_id = f.id "
            f"WHERE f.media = 'audio' AND f.id IN ({placeholders}) ORDER BY f.dir, f.name",
            ids,
        )
        ALBUM_SHARED_FIELDS = ("artist", "album", "album_artist", "year", "genre", "cover")
        updated = 0
        unchanged = 0
        errors: list[str] = []
        cancel = self._new_cancel()
        total = max(1, len(rows))
        self._set_action_progress("apply_metadata", "Updating metadata", 0, total, "")
        try:
            return self._apply_metadata_rows(rows, total, scope, field_overrides, allowed_fields, token, st, opts, cancel)
        finally:
            self._clear_action_progress("apply_metadata")

    def _apply_metadata_rows(self, rows: list[dict], total: int, scope: str, field_overrides: dict | None,
                             allowed_fields: set[str] | None, token: str, st: dict, opts: dict, cancel) -> dict:
        ALBUM_SHARED_FIELDS = ("artist", "album", "album_artist", "year", "genre", "cover")
        updated = 0
        unchanged = 0
        errors: list[str] = []
        cover_downloads: dict[str, tuple[bytes, str]] = {}
        for idx, r in enumerate(rows):
            if cancel():
                break
            if idx % _TAG_WARM_CHUNK == 0:
                self._warm_audio_tags_chunk(rows[idx: idx + _TAG_WARM_CHUNK])
            self._set_action_progress("apply_metadata", "Updating metadata", idx, total, r["relpath"])
            if r["status"] == "processing":
                continue
            src = self.abs_path("audio", r["relpath"])
            if not os.path.isfile(src):
                continue
            # Always read live tags from disk so stale cached tags never suppress a needed write
            live_current = metadata.read_full_tags(src)
            try:
                path_meta = json.loads(r["path_json"] or "{}") if r.get("path_json") else metadata.parse_path_full(r["relpath"])
                online_meta = json.loads(r["online_json"] or "{}") if r.get("online_json") else {}
                selected = json.loads(r["selected_json"] or "{}") if r.get("selected_json") else {}
            except Exception:
                path_meta = metadata.parse_path_full(r["relpath"])
                online_meta = {}
                selected = {}
            if not path_meta:
                path_meta = metadata.parse_path_full(r["relpath"])
            by_src = metadata.extract_online_by_source(online_meta)

            working_selected = dict(selected)
            if field_overrides and isinstance(field_overrides, dict):
                for fk, fv in field_overrides.items():
                    if fk not in state.METADATA_FIELDS:
                        continue
                    if scope == "album" and len(rows) > 1 and idx > 0 and fk not in ALBUM_SHARED_FIELDS:
                        continue
                    if isinstance(fv, dict):
                        working_selected[fk] = fv
                    else:
                        working_selected[fk] = {"choice": "custom", "value": str(fv)}

            updates: dict[str, Any] = {}
            for f in state.METADATA_FIELDS:
                if allowed_fields is not None and f not in allowed_fields:
                    continue
                sel_item = working_selected.get(f)
                if not sel_item or not isinstance(sel_item, dict):
                    continue
                choice = str(sel_item.get("choice") or "current").strip()
                val = str(sel_item.get("value") or "").strip()
                if choice == "current":
                    continue
                if not val and choice != "custom":
                    if choice in ("discogs", "musicbrainz", "open"):
                        val = str(by_src[choice].get(f) or "").strip()
                    elif choice == "path":
                        val = str(path_meta.get(f) or "").strip()
                    elif choice == "online":
                        val = (
                            str(by_src["discogs"].get(f) or "").strip()
                            or str(by_src["musicbrainz"].get(f) or "").strip()
                            or str(by_src["open"].get(f) or "").strip()
                            or str(online_meta.get(f) or "").strip()
                        )
                if f == "cover":
                    # Allow embedding OR replacing cover art whenever a valid cover URL/source is selected
                    if val and val.startswith(("http://", "https://", "data:", "local://")):
                        updates["cover"] = val
                elif choice == "custom":
                    if metadata._values_differ(f, str(live_current.get(f) or ""), val):
                        updates[f] = val
                elif val and metadata._values_differ(f, str(live_current.get(f) or ""), val):
                    updates[f] = val

            if not updates:
                unchanged += 1
                # Even if disk already had the desired values, ensure DB tables match live disk state
                now_ts = time.time()
                preserved_sel: dict[str, Any] = {}
                for fk, fv in working_selected.items():
                    if not isinstance(fv, dict):
                        continue
                    if allowed_fields is None or fk in allowed_fields:
                        preserved_sel[fk] = {"choice": "current", "value": str(live_current.get(fk) or "")}
                    else:
                        preserved_sel[fk] = fv
                cmp = metadata.compare_file_metadata(r["relpath"], live_current, path_meta, online_meta, opts, preserved_sel or None)
                with self.db.tx():
                    self.db.execute(
                        "INSERT INTO file_metadata (file_id, status, current_json, path_json, online_json, selected_json, diffs_json, path_diff, checked_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(file_id) DO UPDATE SET status = excluded.status, current_json = excluded.current_json, "
                        "path_json = excluded.path_json, online_json = excluded.online_json, "
                        "selected_json = excluded.selected_json, diffs_json = excluded.diffs_json, "
                        "path_diff = excluded.path_diff, checked_at = excluded.checked_at",
                        (
                            r["id"],
                            cmp["status"],
                            json.dumps(live_current),
                            json.dumps(path_meta),
                            json.dumps(online_meta),
                            json.dumps(cmp["selected"]),
                            json.dumps(cmp["diffs"]),
                            cmp["path_diff"],
                            now_ts,
                        ),
                    )
                continue

            wrote_disk = False
            try:
                if st["audio"]["writeMode"] == "backup":
                    self._backup(src, "audio", r["relpath"])
                prefetched: tuple[bytes, str] | None = None
                cov_url = str(updates.get("cover") or "")
                if cov_url and cov_url.startswith(("http://", "https://", "data:", "local://")):
                    if cov_url in cover_downloads:
                        prefetched = cover_downloads[cov_url]
                    else:
                        try:
                            dl = cover.download_cover(cov_url, token=token)
                            if not dl[0] or len(dl[0]) < 16:
                                raise ValueError("invalid cover image data")
                            cover_downloads[cov_url] = dl
                            prefetched = dl
                        except Exception as dl_exc:
                            tag_only = {k: v for k, v in updates.items() if k != "cover"}
                            if not tag_only:
                                raise
                            metadata.write_full_tags(src, tag_only, discogs_token=token, preserve_mtime=True)
                            raise metadata.CoverUpdateError(
                                f"tags updated, but cover art failed ({dl_exc})",
                                tags_written=True,
                            ) from dl_exc
                metadata.write_full_tags(
                    src,
                    updates,
                    discogs_token=token,
                    preserve_mtime=True,
                    prefetched_cover=prefetched,
                )
                wrote_disk = True
            except metadata.CoverUpdateError as warn_exc:
                wrote_disk = warn_exc.tags_written
                msg = f"{r['name']}: {warn_exc}"
                errors.append(msg)
                self.db.log("warn", msg, "audio")
            except Exception as exc:
                msg = f"{r['name']}: failed to update metadata ({exc})"
                errors.append(msg)
                self.db.log("error", msg, "audio")

            if wrote_disk:
                now_ts = time.time()
                st_after = os.stat(src)
                new_current = metadata.read_full_tags(src)
                preserved_sel = {}
                for fk, fv in working_selected.items():
                    if not isinstance(fv, dict):
                        continue
                    if fk in updates:
                        preserved_sel[fk] = {"choice": "current", "value": str(new_current.get(fk) or "")}
                    else:
                        preserved_sel[fk] = fv
                for fk in updates:
                    if fk not in preserved_sel:
                        preserved_sel[fk] = {"choice": "current", "value": str(new_current.get(fk) or "")}
                cmp = metadata.compare_file_metadata(
                    r["relpath"], new_current, path_meta, online_meta, opts, preserved_sel or None
                )
                has_cov = 1 if new_current.get("cover") == "present" else 0
                c_stat = (
                    "embedded"
                    if ("cover" in updates and has_cov)
                    else ("ok" if has_cov else (r.get("cover_status") if r.get("cover_status") in ("choices", "not_found", "skipped") else "missing"))
                )
                c_cands = None if has_cov else r.get("cover_candidates")
                cov_ts = now_ts if "cover" in updates else (r.get("cover_updated_at") or now_ts)
                resolved_after = cover.resolve_search_metadata(r["relpath"], new_current)
                with self.db.tx():
                    self.db.execute(
                        "INSERT INTO file_metadata (file_id, status, current_json, path_json, online_json, selected_json, diffs_json, path_diff, checked_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(file_id) DO UPDATE SET status = excluded.status, current_json = excluded.current_json, "
                        "path_json = excluded.path_json, online_json = excluded.online_json, "
                        "selected_json = excluded.selected_json, diffs_json = excluded.diffs_json, "
                        "path_diff = excluded.path_diff, checked_at = excluded.checked_at",
                        (
                            r["id"],
                            cmp["status"],
                            json.dumps(new_current),
                            json.dumps(path_meta),
                            json.dumps(online_meta),
                            json.dumps(cmp["selected"]),
                            json.dumps(cmp["diffs"]),
                            cmp["path_diff"],
                            now_ts,
                        ),
                    )
                    self.db.execute(
                        "UPDATE files SET size = ?, mtime_ns = ?, has_cover = ?, cover_status = ?, cover_candidates = ?, cover_updated_at = ?, "
                        "tag_artist = ?, tag_album = ?, tag_title = ?, cover_query_source = ? WHERE id = ?",
                        (
                            st_after.st_size,
                            st_after.st_mtime_ns,
                            has_cov,
                            c_stat,
                            c_cands,
                            cov_ts,
                            new_current.get("artist") or resolved_after["artist"] or r.get("tag_artist") or "",
                            new_current.get("album") or resolved_after["album"] or r.get("tag_album") or "",
                            new_current.get("title") or resolved_after["title"] or r.get("tag_title") or "",
                            resolved_after["source"],
                            r["id"],
                        ),
                    )
                updated += 1
            self._set_action_progress("apply_metadata", "Updating metadata", idx + 1, total, r["relpath"])

        if updated:
            self.db.log("ok", f"Updated ID3/metadata tags in {updated} file(s) (timestamps preserved)", "audio")
        return {"updated": updated, "unchanged": unchanged, "errors": errors}

