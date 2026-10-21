"""Schedule windows: a 7 x 24 grid per media type (Monday first, local time)."""
from __future__ import annotations

from datetime import datetime, timedelta


def in_window(grid: list, when: datetime) -> bool:
    return bool(grid[when.weekday()][when.hour])


def next_change(grid: list, now: datetime) -> datetime | None:
    """First full hour after now at which the window opens or closes."""
    current = in_window(grid, now)
    hour = now.replace(minute=0, second=0, microsecond=0)
    for i in range(1, 7 * 24 + 1):
        candidate = hour + timedelta(hours=i)
        if in_window(grid, candidate) != current:
            return candidate
    return None
