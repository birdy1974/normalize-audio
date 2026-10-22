"""Run control, schedule gate, scanning and the per-file pipeline.

Pipeline for one file:  measure -> decide -> (park | skip | ok | apply) -> verify -> atomic commit.
Parked files wait for the user; the run continues with the next file.
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ffmpeg as ff
from . import folders, schedule, state, stats
from .config import Config, tool_path
from .decide import decide
from .media import DEFAULT_KBPS, MEDIA, audio_family
from .scanner import Scanner

log = logging.getLogger("normalizer")
GAIN_TOOLS = ("mp3gain", "aacgain")


class Engine:
    def __init__(self, cfg: Config, db):
        self.cfg = cfg
        self.db = db
        self.tz = ZoneInfo(cfg.tz)
        self.scanner = Scanner(cfg, db)
        self.tools = {name for name in GAIN_TOOLS if tool_path(getattr(cfg, name))}
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._scanning = 0                     # scans running (the periodic one and manual ones)
        self._inflight: dict[int, dict] = {}    # file id -> {future, relpath, media, approved}
        self._cancel: set[int] = set()
        self._had_work = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pool = ThreadPoolExecutor(max_workers=cfg.concurrency, thread_name_prefix="normalizer")
        # restart recovery: files that were being processed go back to the queue
        db.execute("UPDATE files SET status = 'pending' WHERE status = 'processing'")
        self._remove_stale_temp_files()
        state.get_ha(db)                         # creates the endpoint token on first start

    # ------------------------------------------------------------ helpers

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def abs_path(self, media: str, rel: str) -> str:
        return os.path.join(self.cfg.root(media), rel)

    def window_open(self, media: str, when: datetime | None = None) -> bool:
        return schedule.in_window(state.get_schedule(self.db)[media], when or self.now())

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

    def status(self) -> str:
        run = state.get_run(self.db)
        if run["mode"] != "active":
            return "stopped"
        if run["paused"]:
            return "paused"
        types = [m for m in MEDIA if run["types"].get(m)]
        if not types:
            return "idle"
        open_types = [m for m in types if self.window_open(m)]
        if not open_types:
            return "waiting_window"
        # only work in a type whose window is open counts as running
        busy = self.inflight_count() > 0 or any(stats.media_stats(self.db, m)["files_pending"] for m in open_types)
        return "running" if busy else "idle"

    # ------------------------------------------------------------ run control

    def start(self) -> None:
        state.set_run(self.db, mode="active", paused=False)
        self.db.log("info", "Run started")

    def stop(self) -> None:
        state.set_run(self.db, mode="stopped", paused=False)
        with self._lock:
            for fid, item in self._inflight.items():
                if not item["approved"]:
                    self._cancel.add(fid)
        self.db.log("info", "Run stopped")

    def toggle_pause(self) -> None:
        run = state.get_run(self.db)
        if run["mode"] != "active":
            return
        paused = not run["paused"]
        state.set_run(self.db, paused=paused)
        self.db.log("info", "Run paused" if paused else "Run resumed")

    def set_types(self, changes: dict) -> None:
        run = state.get_run(self.db)
        for m, value in changes.items():
            if m in MEDIA and value is not None:
                run["types"][m] = bool(value)
        state.set_run(self.db, types=run["types"])

    # ------------------------------------------------------------ threads

    def start_threads(self) -> None:
        for target, name in ((self._loop, "scheduler"), (self._scan_loop, "scanner")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for fid, item in self._inflight.items():
                if not item["approved"]:
                    self._cancel.add(fid)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # keep the scheduler alive
                log.exception("scheduler tick failed")
            self._stop.wait(1.0)

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_now()
            except Exception as exc:
                log.exception("scan failed")
                self.db.log("error", f"Scan failed: {exc}")
            minutes = state.get_schedule(self.db)["scanEvery"]
            self._stop.wait(minutes * 60)

    def scan_now(self, media: str | None = None) -> dict:
        with self._scan_lock:
            self._scanning += 1
        try:
            if media:
                return {media: self.scanner.scan(media)}
            return self.scanner.scan_all()
        finally:
            with self._scan_lock:
                self._scanning -= 1

    # ------------------------------------------------------------ scheduler

    def tick(self) -> None:
        run = state.get_run(self.db)
        sched = state.get_schedule(self.db)
        now = self.now()
        open_ = {m: schedule.in_window(sched[m], now) for m in MEDIA}

        if sched["onClose"] == "pause":
            with self._lock:
                for fid, item in self._inflight.items():
                    if not item["approved"] and not open_[item["media"]]:
                        self._cancel.add(fid)

        # (file id, approved by the user, media). Approved work runs at any time; automatic work
        # only while the run is active, not paused, and the window of its media type is open.
        picks: list[tuple[int, bool, str]] = [
            (r["id"], True, r["media"]) for r in
            self.db.query("SELECT id, media FROM files WHERE status = 'approved' ORDER BY id LIMIT 50")]

        automatic = run["mode"] == "active" and not run["paused"]
        if automatic:
            for m in MEDIA:
                if not (run["types"].get(m) and open_[m]):
                    continue
                enabled = folders.enabled_set(self.db, m)
                rows = self.db.query(
                    "SELECT id, dir FROM files WHERE media = ? AND status = 'pending' ORDER BY id LIMIT 300", (m,))
                picks += [(r["id"], False, m) for r in rows if r["dir"] in enabled]

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
            left = sum(stats.media_stats(self.db, m)["files_pending"] for m in MEDIA if run["types"].get(m))
            if left == 0:
                self._had_work = False
                state.set_run(self.db, lastFinished=now.isoformat(timespec="seconds"))
                self.db.log("info", "Run finished: nothing left to check in the enabled folders")

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

    # ------------------------------------------------------------ per-file work

    def _process(self, fid: int) -> None:
        row = self.db.one("SELECT * FROM files WHERE id = ?", (fid,))
        if row is None:
            return
        prior = row["status"]
        media, name = row["media"], row["name"]
        self.db.execute("UPDATE files SET status = 'processing', reason = NULL WHERE id = ?", (fid,))

        def cancel() -> bool:
            return fid in self._cancel

        try:
            if prior == "approved":
                self._apply_approved(row, cancel)
            else:
                self._check(row, cancel)
        except ff.Cancelled:
            back = "approved" if prior == "approved" else "pending"
            self.db.execute("UPDATE files SET status = ? WHERE id = ?", (back, fid))
            self.db.log("info", f"{name}: stopped at window end, back in the queue", media)
        except Exception as exc:
            self.db.execute("UPDATE files SET status = 'error', reason = ? WHERE id = ?", (str(exc)[:300], fid))
            self.db.log("error", f"{name}: {exc}", media)

    def _check(self, row: dict, cancel) -> None:
        media, fid, rel = row["media"], row["id"], row["relpath"]
        src = self.abs_path(media, rel)
        if not os.path.isfile(src):
            self.db.execute("DELETE FROM files WHERE id = ?", (fid,))
            return
        st = state.get_settings(self.db)
        cfg_m = st[media]
        lufs, true_peak = ff.measure(self.cfg.ffmpeg, src, cancel)
        probe = ff.probe(self.cfg.ffmpeg, src, cancel)
        family = audio_family(probe.codec)
        self.db.execute(
            "UPDATE files SET lufs = ?, true_peak = ?, measured_at = ?, codec = ?, family = ? WHERE id = ?",
            (lufs, true_peak, time.time(), probe.codec, family, fid))

        if lufs <= -60:
            self.db.execute("UPDATE files SET status = 'skipped', reason = 'silent' WHERE id = ?", (fid,))
            return
        strategy = cfg_m.get("lossy", "reencode") if media == "audio" else "reencode"
        d = decide(media=media, ext=row["ext"], family=family, measured=lufs, true_peak=true_peak,
                   target=cfg_m["target"], ceiling=cfg_m["truePeak"], threshold=cfg_m["threshold"],
                   override=cfg_m["override"], strategy=strategy, tools=self.tools)
        if d.action == "error":
            raise ff.ToolError(f"unsupported audio codec '{probe.codec}'")
        self.db.execute("UPDATE files SET gain_db = ?, method = ? WHERE id = ?", (d.gain, d.method, fid))

        if st["general"]["dryRun"]:
            self.db.execute("UPDATE files SET status = 'dryrun' WHERE id = ?", (fid,))
            self.db.log("info", f"{row['name']}: dry run, would be {d.action}"
                                f" ({d.gain:+.1f} dB)", media)
            return
        if d.action == "ok":
            self.db.execute("UPDATE files SET status = 'ok' WHERE id = ?", (fid,))
        elif d.action == "skip":
            self.db.execute("UPDATE files SET status = 'skipped', reason = ? WHERE id = ?", (d.reason, fid))
        elif d.action == "park":
            self.db.execute("UPDATE files SET status = 'parked', reason = ? WHERE id = ?", (d.reason, fid))
            if d.reason == "large_change":
                self.db.log("warn", f"{row['name']}: change {d.gain:+.1f} dB is large, parked for review", media)
        else:
            self._apply(row, cfg_m, st, d.method, d.gain, probe, family, cancel)

    def _apply_approved(self, row: dict, cancel) -> None:
        media = row["media"]
        st = state.get_settings(self.db)
        cfg_m = st[media]
        if row["gain_db"] is None:
            raise ff.ToolError("no gain stored for this file; check it again")
        gain = round(row["gain_db"] + (row["adjust_db"] or 0.0), 2)
        probe = ff.probe(self.cfg.ffmpeg, self.abs_path(media, row["relpath"]), cancel)
        family = row["family"] or audio_family(probe.codec)
        room = cfg_m["truePeak"] - row["true_peak"] if row["true_peak"] is not None else None
        if gain > 0 and room is not None and gain > room:
            gain = round(max(0.0, room), 2)
            self.db.log("warn", f"{row['name']}: gain limited to keep the true peak under {cfg_m['truePeak']:.1f} dBTP",
                        media)
        self._apply(row, cfg_m, st, row["method"] or "reencode", gain, probe, family, cancel)

    def _apply(self, row, cfg_m, st, method, gain, probe, family, cancel) -> None:
        media, fid, rel, name, ext = row["media"], row["id"], row["relpath"], row["name"], row["ext"]
        if abs(gain) < 0.05:
            self.db.execute("UPDATE files SET status = 'ok', gain_db = ? WHERE id = ?", (gain, fid))
            return
        src = self.abs_path(media, rel)
        mode = cfg_m["writeMode"]
        if mode == "output":
            final = os.path.join(cfg_m["outputDir"], rel)
        else:
            final = src
        tmp = ff.temp_path(final)
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        applied = round(gain, 2)
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
                             method=method, gain=applied, kbps=kbps, cancel=cancel)
            ff.verify(self.cfg.ffmpeg, tmp, probe, cancel)
            self._commit(src, tmp, final, media, rel, mode, st["general"]["preserveMtime"])
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        self.db.execute(
            "UPDATE files SET status = 'changed', applied_db = ?, gain_db = ?, method = ?, reason = NULL, "
            "changed_at = ? WHERE id = ?",
            (applied, gain, method, time.time(), fid))
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

    def measure(self, media: str, rel: str) -> dict:
        """Level check for one file (baseline). The file is only read."""
        src = self.abs_path(media, rel)
        if not os.path.isfile(src):
            raise ValueError("file not found")
        lufs, true_peak = ff.measure(self.cfg.ffmpeg, src)
        probe = ff.probe(self.cfg.ffmpeg, src)
        name = os.path.basename(rel)
        self.db.execute("UPDATE files SET lufs = ?, true_peak = ?, measured_at = ?, codec = ? "
                        "WHERE media = ? AND relpath = ? AND status != 'processing'",
                        (lufs, true_peak, time.time(), probe.codec, media, rel))
        self.db.log("info", f"Level checked: {name} {lufs:.1f} LUFS, true peak {true_peak:.1f} dBTP", media)
        return {"name": name, "relpath": rel, "lufs": lufs, "true_peak": true_peak, "codec": probe.codec}
