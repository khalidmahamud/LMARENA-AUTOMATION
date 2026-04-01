from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from playwright.async_api import BrowserContext, Playwright, Worker, async_playwright

from src.core.tiling import TileLayout, compute_tile_positions
from src.models.config import AppConfig, DisplayConfig
from src.proxy.pool import ProxyPool

logger = logging.getLogger(__name__)


class _RunGroup:
    """Internal state for a single run's browser contexts."""

    __slots__ = (
        "contexts", "tiles", "profile_base", "context_dirs",
        "proxies", "proxy_on_challenge", "proxy_assign_counter",
        "context_proxies", "incognito_mode", "zoom_pct",
    )

    def __init__(self) -> None:
        self.contexts: List[BrowserContext] = []
        self.tiles: List[TileLayout] = []
        self.profile_base: Optional[Path] = None
        self.context_dirs: Dict[int, Path] = {}
        self.proxies: List[dict] = []
        self.proxy_on_challenge: bool = False
        self.proxy_assign_counter: int = 0
        self.context_proxies: Dict[int, Optional[str]] = {}
        self.incognito_mode: bool = False
        self.zoom_pct: int = 100


class BrowserManager:
    """Manages N headed Playwright browser contexts with fresh run-local state.

    Supports multiple concurrent runs via ``run_id``-keyed context groups.
    Each window still uses ``launch_persistent_context()`` so Chromium opens
    as a separate top-level window, but the backing ``user_data_dir`` lives in
    a temporary per-run directory that is deleted when the run ends.
    """

    def __init__(self, config: AppConfig, proxy_pool: Optional[ProxyPool] = None) -> None:
        self._config = config
        self._proxy_pool = proxy_pool
        self._playwright: Optional[Playwright] = None
        self._tmp_root = Path(".tmp_browser_profiles")
        self._zoom_extension_dir = (Path(__file__).with_name("zoom_extension")).resolve()
        # Per-run state
        self._groups: Dict[str, _RunGroup] = {}
        # Backward-compat: default group key for callers that don't pass run_id
        self._default_group_key = "__default__"

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
        zoom_pct: int = 100,
        run_id: Optional[str] = None,
    ) -> List[BrowserContext]:
        """Launch *count* isolated persistent browser contexts for a run.

        Windows are automatically tiled to perfectly fill the available
        screen area.  Pass *display_override* to use UI-provided monitor
        settings instead of the YAML defaults.

        If *run_id* is provided, contexts are stored in a separate group
        so multiple runs can coexist.
        """
        if self._playwright is None:
            raise RuntimeError("Call start() before create_contexts()")

        gkey = run_id or self._default_group_key
        # Close any previous contexts for this group
        await self.close_contexts(run_id=gkey)

        group = _RunGroup()
        self._groups[gkey] = group

        disp = display_override or self._config.display
        group.incognito_mode = (
            self._config.browser.incognito if incognito is None else incognito
        )

        group.tiles = compute_tile_positions(
            count=count,
            monitor_count=disp.monitor_count,
            monitor_width=disp.monitor_width,
            monitor_height=disp.monitor_height,
            taskbar_height=disp.taskbar_height,
            margin=disp.margin,
            border_offset=disp.border_offset,
        )

        group.proxies = proxies or []
        group.proxy_on_challenge = proxy_on_challenge
        group.proxy_assign_counter = 0
        group.context_proxies.clear()
        group.zoom_pct = max(25, min(200, int(zoom_pct)))

        # Seed the pool with manually provided proxies
        if group.proxies and self._proxy_pool:
            self._proxy_pool.add_proxies(group.proxies, source="manual")

        self._tmp_root.mkdir(parents=True, exist_ok=True)
        group.profile_base = Path(
            tempfile.mkdtemp(prefix="arena_run_", dir=str(self._tmp_root))
        )
        group.context_dirs.clear()

        for i in range(count):
            tile = group.tiles[i]
            profile_dir = Path(
                tempfile.mkdtemp(
                    prefix=f"context_{i}_",
                    dir=str(group.profile_base),
                )
            )
            # If proxy_on_challenge, launch without proxy — proxy assigned on recreate
            if group.proxy_on_challenge:
                proxy = None
            else:
                proxy = group.proxies[i % len(group.proxies)] if group.proxies else None
            ctx = await self._launch_context(
                profile_dir, tile, proxy=proxy,
                incognito_mode=group.incognito_mode,
                zoom_pct=group.zoom_pct,
            )

            group.contexts.append(ctx)
            group.context_dirs[i] = profile_dir
            group.context_proxies[i] = proxy.get("server") if proxy else None
            logger.info(
                "Context %d (run=%s) launched at (%d, %d) size %dx%d",
                i, gkey[:8], tile.x, tile.y, tile.width, tile.height,
            )

        return list(group.contexts)

    async def close_contexts(self, run_id: Optional[str] = None) -> None:
        """Close browser contexts for a specific run (or default group).

        If *run_id* is ``None``, closes the default group for backward compat.
        """
        gkey = run_id or self._default_group_key
        group = self._groups.pop(gkey, None)
        if group is None:
            return

        for ctx in group.contexts:
            try:
                await ctx.close()
            except Exception:
                pass
        group.contexts.clear()
        if group.profile_base and group.profile_base.exists():
            shutil.rmtree(group.profile_base, ignore_errors=True)
        group.context_dirs.clear()
        logger.info("Browser contexts closed for run %s", gkey[:8])

    async def close_all(self) -> None:
        """Gracefully close every context across all runs and stop Playwright."""
        for gkey in list(self._groups):
            await self.close_contexts(run_id=gkey)

        if self._tmp_root.exists():
            shutil.rmtree(self._tmp_root, ignore_errors=True)

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Playwright engine stopped")

    async def recreate_context(self, index: int, run_id: Optional[str] = None) -> BrowserContext:
        """Close context *index* and launch a fresh window in the same tile."""
        if self._playwright is None:
            raise RuntimeError("Playwright not running")

        gkey = run_id or self._default_group_key
        group = self._groups.get(gkey)
        if group is None:
            raise RuntimeError(f"No run group found for run_id={gkey}")
        if index < 0 or index >= len(group.contexts):
            raise IndexError(f"Context index {index} out of range")

        # Close the existing context
        try:
            await group.contexts[index].close()
        except Exception:
            pass

        if group.profile_base is None:
            self._tmp_root.mkdir(parents=True, exist_ok=True)
            group.profile_base = Path(
                tempfile.mkdtemp(prefix="arena_run_", dir=str(self._tmp_root))
            )

        old_profile_dir = group.context_dirs.get(index)
        if old_profile_dir and old_profile_dir.exists():
            shutil.rmtree(old_profile_dir, ignore_errors=True)

        profile_dir = Path(
            tempfile.mkdtemp(
                prefix=f"context_{index}_",
                dir=str(group.profile_base),
            )
        )

        # Launch new context at the same tile position
        tile = group.tiles[index]
        if group.proxy_on_challenge and self._proxy_pool and self._proxy_pool.healthy_count > 0:
            # Mark old proxy as problematic
            old_proxy = group.context_proxies.get(index)
            if old_proxy:
                self._proxy_pool.mark_unhealthy(old_proxy)
            # Get next healthy proxy from pool
            proxy = self._proxy_pool.get_next_healthy()
            if proxy:
                logger.info("Pool mode: assigning healthy proxy %s to context %d", proxy.get("server", "???"), index)
            else:
                logger.warning("No healthy proxies in pool for context %d", index)
        elif group.proxy_on_challenge and group.proxies:
            # Fallback to round-robin if no pool
            proxy = group.proxies[group.proxy_assign_counter % len(group.proxies)]
            group.proxy_assign_counter += 1
            logger.info("Challenge mode: assigning proxy %s to context %d", proxy.get("server", "???"), index)
        else:
            proxy = group.proxies[index % len(group.proxies)] if group.proxies else None
        ctx = await self._launch_context(
            profile_dir, tile, proxy=proxy,
            incognito_mode=group.incognito_mode,
            zoom_pct=group.zoom_pct,
        )

        group.contexts[index] = ctx
        group.context_dirs[index] = profile_dir
        group.context_proxies[index] = proxy.get("server") if proxy else None
        logger.info("Context %d recreated at (%d, %d)", index, tile.x, tile.y)
        return ctx

    @property
    def contexts(self) -> List[BrowserContext]:
        """Backward compat: return default group's contexts."""
        group = self._groups.get(self._default_group_key)
        return list(group.contexts) if group else []

    def get_context_proxy(self, index: int, run_id: Optional[str] = None) -> Optional[str]:
        """Return the proxy server string for context *index*, or None."""
        gkey = run_id or self._default_group_key
        group = self._groups.get(gkey)
        return group.context_proxies.get(index) if group else None

    def report_proxy_success(self, index: int, run_id: Optional[str] = None) -> None:
        """Mark the proxy used by context *index* as healthy in the pool."""
        gkey = run_id or self._default_group_key
        group = self._groups.get(gkey)
        if group:
            server = group.context_proxies.get(index)
            if server and self._proxy_pool:
                self._proxy_pool.mark_healthy(server)

    async def _get_zoom_service_worker(self, ctx: BrowserContext) -> Optional[Worker]:
        workers = ctx.service_workers
        if workers:
            return workers[0]
        try:
            return await ctx.wait_for_event("serviceworker", timeout=10000)
        except Exception as exc:
            logger.warning("Zoom extension service worker unavailable: %s", exc)
            return None

    async def _configure_browser_zoom_extension(
        self, ctx: BrowserContext, zoom_pct: int, incognito_mode: bool
    ) -> None:
        worker = await self._get_zoom_service_worker(ctx)
        if worker is None:
            return
        try:
            result = await worker.evaluate(
                """async zoomPct => {
                    if (typeof globalThis.configureManagedZoom !== "function") {
                        return { ok: false, error: "configureManagedZoom missing" };
                    }
                    return await globalThis.configureManagedZoom(zoomPct);
                }""",
                zoom_pct,
            )
            logger.info("Browser zoom extension configured: %s", result)
            if incognito_mode and result and not result.get("incognitoAllowed", True):
                logger.warning(
                    "Browser zoom extension is not allowed in incognito windows; "
                    "launching with an isolated temporary profile instead of --incognito"
                )
        except Exception as exc:
            logger.warning("Failed to configure browser zoom extension: %s", exc)

    def _launch_args(self, tile: TileLayout, incognito_mode: bool, zoom_pct: int) -> List[str]:
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            f"--disable-extensions-except={self._zoom_extension_dir}",
            f"--load-extension={self._zoom_extension_dir}",
            f"--window-position={tile.x},{tile.y}",
            f"--window-size={tile.width},{tile.height}",
        ]
        if incognito_mode and zoom_pct == 100:
            args.insert(0, "--incognito")
        elif incognito_mode and zoom_pct != 100:
            logger.info(
                "Skipping --incognito so the browser zoom extension can control Chromium zoom. "
                "Windows still use isolated temporary profiles."
            )
        return args

    async def _launch_context(
        self,
        profile_dir: Path,
        tile: TileLayout,
        proxy: Optional[dict] = None,
        incognito_mode: bool = False,
        zoom_pct: int = 100,
    ) -> BrowserContext:
        launch_kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=self._config.browser.headless,
            no_viewport=True,
            args=self._launch_args(tile, incognito_mode, zoom_pct),
            ignore_default_args=["--enable-automation"],
        )
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info("Launching context with proxy: %s", proxy.get("server", "???"))

        ctx = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        await self._configure_browser_zoom_extension(ctx, zoom_pct, incognito_mode)

        # Apply stealth to existing and future pages
        from src.browser.stealth import apply_stealth

        await apply_stealth(ctx)
        return ctx
