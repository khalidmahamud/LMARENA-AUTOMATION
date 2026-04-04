from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TileLayout:
    """Position and size for a single tiled window."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class MonitorWorkArea:
    """Usable work area of a monitor in virtual-screen coordinates."""

    x: int
    y: int
    width: int
    height: int


def compute_tile_positions(
    count: int,
    start_monitor: int = 1,
    monitor_count: int = 1,
    monitor_width: int = 1920,
    monitor_height: int = 1080,
    taskbar_height: int = 40,
    margin: int = 0,
    border_offset: int = 7,
    monitor_work_areas: Optional[List[MonitorWorkArea]] = None,
) -> List[TileLayout]:
    """Compute position and size for *count* windows tiled across monitors.

    Windows are distributed across monitors first, then tiled within each
    monitor using a sub-grid. This avoids windows straddling monitor
    boundaries. *start_monitor* is a 1-based monitor index used by the
    synthetic fallback when actual monitor work areas are unavailable.

    *border_offset* compensates for the invisible window shadow on
    Windows 10/11 (~7 px on each side).  Set to 0 on Linux/macOS.
    """
    if count <= 0:
        return []

    if monitor_work_areas:
        monitors = [
            mon for mon in monitor_work_areas
            if mon.width > 0 and mon.height > 0
        ]
    else:
        work_height = max(1, monitor_height - taskbar_height)
        monitors = [
            MonitorWorkArea(
                x=(start_monitor - 1 + idx) * monitor_width,
                y=0,
                width=monitor_width,
                height=work_height,
            )
            for idx in range(monitor_count)
        ]

    if not monitors:
        return []

    # ── Distribute windows across monitors ──
    # Fill monitors evenly: first monitors may get one extra window
    active_monitor_count = len(monitors)
    base_per_monitor = count // active_monitor_count
    extra = count % active_monitor_count  # first `extra` monitors get base+1

    tiles: List[TileLayout] = []

    for m, monitor in enumerate(monitors):
        n = base_per_monitor + (1 if m < extra else 0)
        if n == 0:
            continue

        # ── Sub-grid for this monitor ──
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

        # Window size to fill this monitor's work area
        win_w = (monitor.width - (cols + 1) * margin) // cols
        win_h = (monitor.height - (rows + 1) * margin) // rows

        # Clamp to reasonable minimums
        win_w = max(win_w, 400)
        win_h = max(win_h, 300)

        for i in range(n):
            row = i // cols
            col = i % cols

            x = monitor.x + margin + col * (win_w + margin)
            y = monitor.y + margin + row * (win_h + margin)

            # Only overlap on internal edges. Expanding outer edges past the
            # monitor work area can make Windows clamp or restack the window.
            expand_left = border_offset if col > 0 else 0
            expand_right = border_offset if col < cols - 1 else 0
            expand_bottom = border_offset if row < rows - 1 else 0

            tiles.append(TileLayout(
                x=x - expand_left,
                y=y,
                width=win_w + expand_left + expand_right,
                height=win_h + expand_bottom,
            ))

    return tiles
