from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

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
        self._context_dirs: Dict[int, Path] = {}
        self._incognito_mode = self._config.browser.incognito
        self._proxies: List[dict] = []
        self._proxy_on_challenge: bool = False
        self._proxy_assign_counter: int = 0
        self._context_proxies: Dict[int, Optional[str]] = {}

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
        incognito: Optional[bool] = None,
        proxies: Optional[List[dict]] = None,
        proxy_on_challenge: bool = False,
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
        self._incognito_mode = (
            self._config.browser.incognito if incognito is None else incognito
        )

        self._tiles = compute_tile_positions(
            count=count,
            monitor_count=disp.monitor_count,
            monitor_width=disp.monitor_width,
            monitor_height=disp.monitor_height,
            taskbar_height=disp.taskbar_height,
            margin=disp.margin,
            border_offset=disp.border_offset,
        )

        self._proxies = proxies or []
        self._proxy_on_challenge = proxy_on_challenge
        self._proxy_assign_counter = 0
        self._context_proxies.clear()

        self._tmp_root.mkdir(parents=True, exist_ok=True)
        self._run_profile_base = Path(
            tempfile.mkdtemp(prefix="arena_run_", dir=str(self._tmp_root))
        )
        self._context_dirs.clear()

        for i in range(count):
            tile = self._tiles[i]
            profile_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"context_{i}_",
                    dir=str(self._run_profile_base),
                )
            )
            # If proxy_on_challenge, launch without proxy — proxy assigned on recreate
            if self._proxy_on_challenge:
                proxy = None
            else:
                proxy = self._proxies[i % len(self._proxies)] if self._proxies else None
            ctx = await self._launch_context(profile_dir, tile, proxy=proxy)

            self._contexts.append(ctx)
            self._context_dirs[i] = profile_dir
            self._context_proxies[i] = proxy.get("server") if proxy else None
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
        self._context_dirs.clear()
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

        old_profile_dir = self._context_dirs.get(index)
        if old_profile_dir and old_profile_dir.exists():
            shutil.rmtree(old_profile_dir, ignore_errors=True)

        profile_dir = Path(
            tempfile.mkdtemp(
                prefix=f"context_{index}_",
                dir=str(self._run_profile_base),
            )
        )

        # Launch new context at the same tile position
        tile = self._tiles[index]
        if self._proxy_on_challenge and self._proxies:
            # Assign next proxy from the pool (round-robin)
            proxy = self._proxies[self._proxy_assign_counter % len(self._proxies)]
            self._proxy_assign_counter += 1
            logger.info("Challenge mode: assigning proxy %s to context %d", proxy.get("server", "???"), index)
        else:
            proxy = self._proxies[index % len(self._proxies)] if self._proxies else None
        ctx = await self._launch_context(profile_dir, tile, proxy=proxy)

        self._contexts[index] = ctx
        self._context_dirs[index] = profile_dir
        self._context_proxies[index] = proxy.get("server") if proxy else None
        logger.info("Context %d recreated at (%d, %d)", index, tile.x, tile.y)
        return ctx

    @property
    def contexts(self) -> List[BrowserContext]:
        return list(self._contexts)

    def get_context_proxy(self, index: int) -> Optional[str]:
        """Return the proxy server string for context *index*, or None."""
        return self._context_proxies.get(index)

    def _launch_args(self, tile: TileLayout) -> List[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            f"--window-position={tile.x},{tile.y}",
            f"--window-size={tile.width},{tile.height}",
        ]
        if self._incognito_mode:
            args.insert(0, "--incognito")
        return args

    async def _launch_context(
        self,
        profile_dir: Path,
        tile: TileLayout,
        proxy: Optional[dict] = None,
    ) -> BrowserContext:
        launch_kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=self._config.browser.headless,
            no_viewport=True,
            args=self._launch_args(tile),
            ignore_default_args=["--enable-automation"],
        )
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info("Launching context with proxy: %s", proxy.get("server", "???"))

        ctx = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

        # Apply stealth to existing and future pages
        from src.browser.stealth import apply_stealth

        await apply_stealth(ctx)
        return ctx
