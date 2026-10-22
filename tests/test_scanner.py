"""Scanner robustness: names that are not valid UTF-8 must not crash scan_all()."""
from __future__ import annotations

import os

from app.db import Database
from app.scanner import Scanner
from conftest import make, tone


def test_scan_skips_non_utf8_names_instead_of_crashing(cfg, tmp_path):
    audio_root = cfg.root("audio")

    # A normal, well-formed file.
    tone(os.path.join(audio_root, "good.mp3"), 0.0, seconds=0.2)

    # A file name that is not valid UTF-8 (a lone surrogate once decoded with
    # surrogateescape, exactly what os.walk hands back for such names on POSIX).
    bad_name = b"track_\xf8.mp3".decode("utf-8", "surrogateescape")
    tone(os.path.join(audio_root, bad_name), 0.0, seconds=0.2)

    # A folder name that is not valid UTF-8, containing a valid file inside it.
    bad_dir = b"alb\xf8m".decode("utf-8", "surrogateescape")
    os.mkdir(os.path.join(audio_root, bad_dir))
    tone(os.path.join(audio_root, bad_dir, "inner.mp3"), 0.0, seconds=0.2)

    db = Database(str(tmp_path / "config" / "db.sqlite"))
    scanner = Scanner(cfg, db)

    # Must not raise, unlike before the fix (UnicodeEncodeError from sqlite).
    result = scanner.scan_all()

    assert result["audio"]["available"] is True
    assert result["audio"]["skipped"] == 2

    rows = db.query("SELECT relpath FROM files WHERE media = 'audio'")
    assert [r["relpath"] for r in rows] == ["good.mp3"]

    # The error is surfaced to the log so the user knows to rename the file(s).
    messages = [l["msg"] for l in db.log_list()]
    assert any("not valid UTF-8" in m for m in messages)

    # The other media type must still be scanned (previously the whole
    # scan_all() dict comprehension aborted, leaving every media type empty).
    assert result["video"]["available"] is True
