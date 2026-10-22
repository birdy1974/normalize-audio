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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import ffmpeg as ff
from . import folders, schedule, state, stats
from .config import Config, tool_path
from .decide import decide
from .media import DEFAULT_KBPS, MEDIA, audio_family
from .scanner import Scanner

log = logging.getLogger("normalizer")
GAIN_TOOLS = ("mp3gain", "aacgain")


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
        self._scan_lock = threading.Lock()
        self._scanning = 0                     # scans running (the periodic one and manual ones)
        self._inflight: dict[int, dict] = {}    # file id -> {future, relpath, media, approved}
        self._cancel: set[int] = set()
        self._had_work = False
        self._mode_seen: str | None = None     # the app mode (off / bulk / auto) last applied
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
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
        """What the app does on its own: 'off', 'bulk' (only a started run works) or 'auto'."""
        return state.get_settings(self.db)["general"]["mode"]

    def scan_interval(self) -> int:
        """Minutes between two looks for new, changed and removed files (Settings > Scheduling)."""
        return state.get_schedule(self.db)["scanEvery"]

    def last_scan(self) -> str | None:
        return state.get_run(self.db).get("lastScan")

    def next_scan(self) -> str | None:
        """When the folders are looked at next; None when the app is off."""
        if self.mode() == "off":
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
            if previous == "auto" and run["mode"] == "active":
                state.set_run(self.db, mode="stopped", paused=False)
                self.db.log("info", "Bulk mode: the automatic run is stopped, press Start to normalize the files that are here")
        else:                                # auto
            # switching to automatic starts at once; a restart keeps a pause the user left in place
            state.set_run(self.db, mode="active", **({"paused": False} if previous is not None else {}))
            self.db.log("info", f"Automatic mode is on: new files are checked and normalized every "
                                f"{self.scan_interval()} minutes")
        self._had_work = False
        return mode

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
        busy = self.inflight_count() > 0 or any(stats.media_stats(self.db, m)["files_pending"] for m in open_types)
        return "running" if busy else "idle"

    # ------------------------------------------------------------ run control

    def start(self) -> None:
        if self.mode() == "off":
            raise ValueError("The app is off: choose Bulk or Automatic mode in Settings first")
        # a run that starts with nothing to do finishes at once (and in bulk mode stops itself)
        self._had_work = True
        state.set_run(self.db, mode="active", paused=False)
        self.db.log("info", "Bulk run started" if self.mode() == "bulk" else "Run started")

    def stop(self) -> None:
        if self.mode() == "auto":
            raise ValueError("Automatic mode keeps watching for new files; pause the run to hold it, "
                             "or choose Off or Bulk in Settings")
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
        what = "Automatic mode" if self.mode() == "auto" else "Run"
        self.db.log("info", f"{what} paused" if paused else f"{what} resumed")

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
        """Look for new, changed and removed files every `scanEvery` minutes, unless the app is off.

        The loop wakes up every two seconds, so turning the app off, changing the interval in the
        settings or starting automatic mode takes effect at once instead of after the old wait.
        """
        while not self._stop.is_set():
            if self.mode() == "off":
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
            # a manual Rescan counts as a scan: the next automatic one starts a full interval later
            state.set_run(self.db, lastScan=self.now().replace(microsecond=0).isoformat(timespec="seconds"))

    # ------------------------------------------------------------ scheduler

    def tick(self) -> None:
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
                if mode == "bulk":
                    # a bulk run works through what is there now; files found later wait for the next Start
                    state.set_run(self.db, mode="stopped")
                    self.db.log("info", "Bulk run finished: nothing left to check; new files wait for the next Start")
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
        try:
            return self._edit_audio(src, media, rel, row, fade_in, fade_out, cut, mode,
                                    settings["general"]["preserveMtime"])
        except BaseException:
            self.db.execute("UPDATE files SET status = ? WHERE id = ?", (row["status"], row["id"]))
            raise

    def _edit_audio(self, src: str, media: str, rel: str, row: dict, fade_in: float, fade_out: float,
                    cut: tuple[float, float] | None, mode: str, preserve_mtime: bool) -> dict:
        probe = ff.probe(self.cfg.ffmpeg, src)
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
                    kbps=probe.kbps)
            ff.verify_edit(self.cfg.ffmpeg, tmp, probe, new_duration)
            self._commit(src, tmp, src, media, rel, mode, preserve_mtime)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

        st = os.stat(src)
        self.db.execute(
            "UPDATE files SET status = 'pending', reason = NULL, size = ?, mtime_ns = ?, seen_at = ?, "
            "lufs = NULL, true_peak = NULL, measured_at = NULL, gain_db = NULL, adjust_db = 0, "
            "applied_db = NULL, method = NULL, changed_at = NULL WHERE id = ?",
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
