"""Background filesystem watcher (Option E) powered by watchfiles (Rust notify / Linux inotify).

Watches configured audio/video roots for added, renamed, or deleted files and folders,
ignoring hidden/Synology (@eaDir, #recycle) paths and in-place file modifications so
normalizer/tag/cover writes do not trigger redundant rescans. Degrades gracefully when
inotify watches are unavailable or exhausted (e.g. low fs.inotify.max_user_watches on NAS).
"""
from __future__ import annotations

import logging
import os
import threading

from .media import EXTENSIONS, MEDIA
from .scanner import SKIP_PREFIXES

log = logging.getLogger("normalizer.watcher")

try:
    from watchfiles import Change, watch as _wf_watch
except Exception:  # pragma: no cover - optional fallback if wheel unavailable
    Change = None  # type: ignore[assignment]
    _wf_watch = None  # type: ignore[assignment]

_IGNORED_SIDECAR_EXTS = frozenset({
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "ico", "svg",
    "cue", "log", "txt", "nfo", "md", "pdf", "sfv", "md5", "sha1", "sha256",
    "srt", "sub", "idx", "ass", "ssa", "vtt", "lrc",
    "m3u", "m3u8", "pls", "xspf",
    "db", "sqlite", "sqlite3", "ini", " desktop", "json", "xml", "yaml", "yml",
    "tmp", "temp", "part", "partial", "crdownload", "bak", "orig", "swp", "lock",
})


def coalesce_target_dirs(targets: set[str | None], max_individual: int = 12) -> list[str | None]:
    """Collapse nested folder targets into minimal top-most subtrees (or [None] for root incremental)."""
    if not targets:
        return []
    if None in targets or "." in targets:
        return [None]
    ordered = sorted((t.strip("/") for t in targets if t and t != "."), key=lambda p: (p.count("/"), p))
    kept: list[str] = []
    for cand in ordered:
        if any(cand == prev or cand.startswith(prev + "/") for prev in kept):
            continue
        kept.append(cand)
    if len(kept) > max_individual:
        return [None]
    return kept  # type: ignore[return-value]


class LibraryWatcher:
    def __init__(
        self,
        cfg,
        db,
        on_targets_fn,
        stop_event: threading.Event,
        *,
        debounce_ms: int = 1600,
        step_ms: int = 150,
    ):
        self.cfg = cfg
        self.db = db
        self.on_targets_fn = on_targets_fn
        self.stop_event = stop_event
        self.debounce_ms = debounce_ms
        self.step_ms = step_ms
        self._thread: threading.Thread | None = None
        self._active = False
        self._disabled_reason: str | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    def start(self) -> None:
        if _wf_watch is None or os.environ.get("DISABLE_FS_WATCHER", "").strip() in ("1", "true", "yes"):
            self._disabled_reason = "disabled"
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="fs-watcher", daemon=True)
        self._thread.start()

    def join(self, timeout: float = 2.0) -> None:
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)

    def _resolve_roots(self) -> dict[str, str]:
        roots: dict[str, str] = {}
        for m in MEDIA:
            r = os.path.abspath(self.cfg.root(m))
            if os.path.isdir(r):
                roots[m] = r
        return roots

    def _match_media(self, abs_path: str, roots: dict[str, str]) -> tuple[str, str] | None:
        best: tuple[str, str] | None = None
        best_len = -1
        for m, r in roots.items():
            if abs_path == r or abs_path.startswith(r + os.sep):
                if len(r) > best_len:
                    best = (m, r)
                    best_len = len(r)
        return best

    def _is_relevant_event(self, change, raw_path: str, roots: dict[str, str]) -> bool:
        # In-place content/tag/gain writes emit Change.modified (2); only structural
        # additions/deletions/renames (added=1, deleted=3) alter folder/file membership.
        if Change is not None and change == Change.modified:
            return False
        abs_path = os.path.abspath(raw_path)
        matched = self._match_media(abs_path, roots)
        if matched is None:
            return False
        media, root = matched
        if abs_path == root:
            return True
        rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
        parts = rel.split("/")
        for part in parts:
            if not part or part.startswith(SKIP_PREFIXES):
                return False
        leaf = parts[-1]
        dot = leaf.rfind(".")
        if dot > 0:
            ext = leaf[dot + 1 :].lower()
            if ext in EXTENSIONS[media]:
                return True
            if ext in _IGNORED_SIDECAR_EXTS and not os.path.isdir(abs_path):
                return False
        return True

    def _targets_from_batch(
        self, changes: set[tuple[object, str]], roots: dict[str, str]
    ) -> dict[str, list[str | None]]:
        grouped_raw: dict[str, set[str | None]] = {}
        for _chg, raw_path in changes:
            abs_path = os.path.abspath(raw_path)
            matched = self._match_media(abs_path, roots)
            if matched is None:
                continue
            media, root = matched
            if abs_path == root:
                grouped_raw.setdefault(media, set()).add(None)
                continue
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if any(not p or p.startswith(SKIP_PREFIXES) for p in rel.split("/")):
                continue
            if os.path.isdir(abs_path):
                grouped_raw.setdefault(media, set()).add(rel)
                continue
            # Check if rel was a tracked directory that was just deleted on disk
            known_dir = self.db.query_tuples(
                "SELECT 1 FROM dirs WHERE media = ? AND relpath = ? LIMIT 1",
                (media, rel),
            )
            if known_dir:
                grouped_raw.setdefault(media, set()).add(rel)
            else:
                parent_rel = os.path.dirname(rel)
                grouped_raw.setdefault(media, set()).add(parent_rel if parent_rel else None)

        return {m: coalesce_target_dirs(tset) for m, tset in grouped_raw.items() if tset}

    def _run(self) -> None:
        while not self.stop_event.is_set():
            roots = self._resolve_roots()
            if not roots:
                self.stop_event.wait(5.0)
                continue
            watch_paths = list(dict.fromkeys(roots.values()))
            try:
                self._active = True
                for changes in _wf_watch(
                    *watch_paths,
                    watch_filter=lambda chg, p: self._is_relevant_event(chg, p, roots),
                    debounce=self.debounce_ms,
                    step=self.step_ms,
                    stop_event=self.stop_event,
                    raise_interrupt=False,
                    force_polling=False,
                    ignore_permission_denied=True,
                ):
                    if self.stop_event.is_set() or not changes:
                        continue
                    targets_by_media = self._targets_from_batch(changes, roots)
                    if targets_by_media:
                        try:
                            self.on_targets_fn(targets_by_media)
                        except Exception:
                            log.exception("watcher target scan callback failed")
                break
            except (OSError, RuntimeError) as exc:
                self._active = False
                self._disabled_reason = str(exc)
                log.warning("Filesystem watcher disabled (%s); manual/scheduled scans remain active", exc)
                try:
                    self.db.log(
                        "warn",
                        f"Live folder watcher disabled ({exc}); manual Rescan and scheduled scans remain active.",
                    )
                except Exception:
                    pass
                return
            except Exception:
                self._active = False
                log.exception("Unexpected error in filesystem watcher")
                self.stop_event.wait(5.0)
        self._active = False
