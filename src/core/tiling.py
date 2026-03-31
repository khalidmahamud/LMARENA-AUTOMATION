from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List


@dataclass
class TileLayout:
    """Position and size for a single tiled window."""

    x: int
    y: int
    width: int
    height: int


def compute_tile_positions(
    count: int,
    monitor_count: int = 1,
    monitor_width: int = 1920,
    monitor_height: int = 1080,
    taskbar_height: int = 40,
    margin: int = 0,
    border_offset: int = 7,
) -> List[TileLayout]:
    """Compute position and size for *count* windows tiled across monitors.

    Windows are distributed across monitors first, then tiled within each
    monitor using a sub-grid.  This avoids windows straddling monitor
    boundaries.

    *border_offset* compensates for the invisible window shadow on
    Windows 10/11 (~7 px on each side).  Set to 0 on Linux/macOS.
    """
    if count <= 0:
        return []

    work_height = monitor_height - taskbar_height

    # ── Distribute windows across monitors ──
    # Fill monitors evenly: first monitors may get one extra window
    base_per_monitor = count // monitor_count
    extra = count % monitor_count  # first `extra` monitors get base+1

    tiles: List[TileLayout] = []
    window_idx = 0

    for m in range(monitor_count):
        n = base_per_monitor + (1 if m < extra else 0)
        if n == 0:
            continue

        monitor_x = m * monitor_width

        # ── Sub-grid for this monitor ──
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)

        # Window size to fill this monitor's work area
        win_w = (monitor_width - (cols + 1) * margin) // cols
        win_h = (work_height - (rows + 1) * margin) // rows

        # Clamp to reasonable minimums
        win_w = max(win_w, 400)
        win_h = max(win_h, 300)

        for i in range(n):
            row = i // cols
            col = i % cols

            x = monitor_x + margin + col * (win_w + margin)
            y = margin + row * (win_h + margin)

            # Apply Windows border compensation:
            # Expand size and shift position so the *visible* content
            # fills the tile exactly (shadows overlap between windows,
            # same as Windows Snap behaviour).
            adj_x = x - border_offset
            adj_w = win_w + 2 * border_offset
            adj_h = win_h + border_offset  # bottom shadow only

            tiles.append(TileLayout(
                x=adj_x,
                y=y,
                width=adj_w,
                height=adj_h,
            ))
            window_idx += 1

    return tiles
