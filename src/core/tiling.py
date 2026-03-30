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
) -> List[TileLayout]:
    """Compute position and size for *count* windows tiled across monitors.

    Windows are auto-sized to perfectly fill the available screen area.
    Multi-monitor assumes horizontal side-by-side arrangement.
    Grid fills left-to-right, top-to-bottom.
    """
    if count <= 0:
        return []

    total_width = monitor_count * monitor_width
    total_height = monitor_height - taskbar_height

    # Pick grid dimensions that best fill the available area
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)

    # Auto-compute window size to perfectly fill the screen
    win_width = (total_width - (cols + 1) * margin) // cols
    win_height = (total_height - (rows + 1) * margin) // rows

    # Clamp to reasonable minimums
    win_width = max(win_width, 400)
    win_height = max(win_height, 300)

    tiles: List[TileLayout] = []
    for idx in range(count):
        row = idx // cols
        col = idx % cols
        x = margin + col * (win_width + margin)
        y = margin + row * (win_height + margin)
        tiles.append(TileLayout(x=x, y=y, width=win_width, height=win_height))

    return tiles
