from __future__ import annotations

import math
from typing import List, Tuple

from src.models.config import WindowSize


def compute_tile_positions(
    count: int,
    window_size: WindowSize,
    screen_width: int = 1920,
    screen_height: int = 1080,
    margin: int = 0,
) -> List[Tuple[int, int]]:
    """Compute (x, y) positions for tiling *count* windows in a grid.

    Grid fills left-to-right, top-to-bottom. Returns one (x, y) tuple
    per window.
    """
    if count <= 0:
        return []

    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)

    positions: List[Tuple[int, int]] = []
    for idx in range(count):
        row = idx // cols
        col = idx % cols
        x = margin + col * (window_size.width + margin)
        y = margin + row * (window_size.height + margin)
        positions.append((x, y))

    return positions
