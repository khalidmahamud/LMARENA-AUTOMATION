from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from playwright.async_api import BrowserContext, Playwright, async_playwright

from src.core.tiling import compute_tile_positions
from src.models.config import AppConfig

logger = logging.getLogger(__name__)


class BrowserManager:
    """Manages N headed Playwright browser contexts with persistent profiles.

    Each context uses ``launch_persistent_context()`` with a separate
    ``user_data_dir`` so cookies (``cf_clearance``, ``arena-auth-prod-v1``)
    survive across runs.  Trade-off: one browser process per context, but
    persistence is essential for Cloudflare bypass.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._playwright: Optional[Playwright] = None
        self._contexts: List[BrowserContext] = []

    async def start(self) -> None:
        """Initialise the Playwright engine (call once at server startup)."""
        self._playwright = await async_playwright().start()
        logger.info("Playwright engine started")

    async def create_contexts(self, count: int) -> List[BrowserContext]:
        """Launch *count* isolated persistent browser contexts.

        Windows are automatically tiled on screen and stealth patches are
        applied to every page opened inside them.
        """
        if self._playwright is None:
            raise RuntimeError("Call start() before create_contexts()")

        profile_base = Path(self._config.browser.profile_dir)
        profile_base.mkdir(parents=True, exist_ok=True)

        positions = compute_tile_positions(
            count=count,
            window_size=self._config.browser.window_size,
        )

        ws = self._config.browser.window_size

        for i in range(count):
            profile_dir = profile_base / f"context_{i}"
            profile_dir.mkdir(exist_ok=True)

            ctx = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=self._config.browser.headless,
                viewport={"width": ws.width, "height": ws.height},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    f"--window-position={positions[i][0]},{positions[i][1]}",
                    f"--window-size={ws.width},{ws.height}",
                ],
                ignore_default_args=["--enable-automation"],
            )

            # Apply stealth to existing and future pages
            from src.browser.stealth import apply_stealth

            await apply_stealth(ctx)

            self._contexts.append(ctx)
            logger.info(
                "Context %d launched at position (%d, %d)",
                i,
                positions[i][0],
                positions[i][1],
            )

        return list(self._contexts)

    async def close_all(self) -> None:
        """Gracefully close every context and stop Playwright."""
        for ctx in self._contexts:
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("All browser contexts closed")

    @property
    def contexts(self) -> List[BrowserContext]:
        return list(self._contexts)
