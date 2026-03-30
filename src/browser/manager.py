from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

from playwright.async_api import BrowserContext, Playwright, async_playwright

from src.core.tiling import TileLayout, compute_tile_positions
from src.models.config import AppConfig, DisplayConfig

logger = logging.getLogger(__name__)


class BrowserManager:
    """Manages N headed Playwright browser contexts with fresh run-local state.

    Each window still uses ``launch_persistent_context()`` so Chromium opens
    as a separate top-level window, but the backing ``user_data_dir`` lives in
    a temporary per-run directory that is deleted when the run ends.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._playwright: Optional[Playwright] = None
        self._contexts: List[BrowserContext] = []
        self._tiles: List[TileLayout] = []
        self._run_profile_base: Optional[Path] = None
        self._tmp_root = Path(".tmp_browser_profiles")

    async def start(self) -> None:
        """Initialise the Playwright engine (call once at server startup)."""
        if self._tmp_root.exists():
            shutil.rmtree(self._tmp_root, ignore_errors=True)
        self._playwright = await async_playwright().start()
        logger.info("Playwright engine started")

    async def create_contexts(
        self,
        count: int,
        display_override: Optional[DisplayConfig] = None,
    ) -> List[BrowserContext]:
        """Launch *count* isolated persistent browser contexts.

        Windows are automatically tiled to perfectly fill the available
        screen area.  Pass *display_override* to use UI-provided monitor
        settings instead of the YAML defaults.
        """
        if self._playwright is None:
            raise RuntimeError("Call start() before create_contexts()")
        await self.close_contexts()

        disp = display_override or self._config.display

        self._tiles = compute_tile_positions(
            count=count,
            monitor_count=disp.monitor_count,
            monitor_width=disp.monitor_width,
            monitor_height=disp.monitor_height,
            taskbar_height=disp.taskbar_height,
            margin=disp.margin,
        )

        self._tmp_root.mkdir(parents=True, exist_ok=True)
        self._run_profile_base = Path(
            tempfile.mkdtemp(prefix="arena_run_", dir=str(self._tmp_root))
        )

        for i in range(count):
            tile = self._tiles[i]
            profile_dir = self._run_profile_base / f"context_{i}"
            profile_dir.mkdir(parents=True, exist_ok=True)

            ctx = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=self._config.browser.headless,
                no_viewport=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    f"--window-position={tile.x},{tile.y}",
                    f"--window-size={tile.width},{tile.height}",
                ],
                ignore_default_args=["--enable-automation"],
            )

            # Grant clipboard access so copy-button extraction works
            await ctx.grant_permissions(["clipboard-read", "clipboard-write"])

            # Apply stealth to existing and future pages
            from src.browser.stealth import apply_stealth

            await apply_stealth(ctx)

            self._contexts.append(ctx)
            logger.info(
                "Context %d launched at (%d, %d) size %dx%d",
                i,
                tile.x,
                tile.y,
                tile.width,
                tile.height,
            )

        return list(self._contexts)

    async def close_contexts(self) -> None:
        """Close all browser contexts (windows) but keep Playwright alive."""
        for ctx in self._contexts:
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._run_profile_base and self._run_profile_base.exists():
            shutil.rmtree(self._run_profile_base, ignore_errors=True)
        self._run_profile_base = None
        if self._tmp_root.exists():
            shutil.rmtree(self._tmp_root, ignore_errors=True)
        logger.info("All browser contexts closed")

    async def close_all(self) -> None:
        """Gracefully close every context and stop Playwright."""
        await self.close_contexts()

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright engine stopped")

    async def recreate_context(self, index: int) -> BrowserContext:
        """Close context *index* and launch a fresh window in the same tile."""
        if self._playwright is None:
            raise RuntimeError("Playwright not running")
        if index < 0 or index >= len(self._contexts):
            raise IndexError(f"Context index {index} out of range")

        # Close the existing context
        try:
            await self._contexts[index].close()
        except Exception:
            pass

        if self._run_profile_base is None:
            self._tmp_root.mkdir(parents=True, exist_ok=True)
            self._run_profile_base = Path(
                tempfile.mkdtemp(prefix="arena_run_", dir=str(self._tmp_root))
            )

        profile_dir = self._run_profile_base / f"context_{index}"
        if profile_dir.exists():
            shutil.rmtree(profile_dir, ignore_errors=True)
        profile_dir.mkdir(parents=True, exist_ok=True)

        # Launch new context at the same tile position
        tile = self._tiles[index]
        ctx = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self._config.browser.headless,
            no_viewport=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
                f"--window-position={tile.x},{tile.y}",
                f"--window-size={tile.width},{tile.height}",
            ],
            ignore_default_args=["--enable-automation"],
        )

        await ctx.grant_permissions(["clipboard-read", "clipboard-write"])

        from src.browser.stealth import apply_stealth

        await apply_stealth(ctx)

        self._contexts[index] = ctx
        logger.info("Context %d recreated at (%d, %d)", index, tile.x, tile.y)
        return ctx

    @property
    def contexts(self) -> List[BrowserContext]:
        return list(self._contexts)
