from __future__ import annotations

import asyncio
import ctypes
import logging
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from playwright.async_api import BrowserContext, Playwright, Worker, async_playwright

from src.core.tiling import MonitorWorkArea, TileLayout, compute_tile_positions
from src.models.config import AppConfig, DisplayConfig
from src.proxy.pool import ProxyPool

logger = logging.getLogger(__name__)


@dataclass
class _LayoutReservation:
    """Reserved tile range for a concurrent run group."""

    base_offset: int
    total_windows: int


class _RunGroup:
    """Internal state for a single run's browser contexts."""

    __slots__ = (
        "contexts", "tiles", "profile_base", "context_dirs",
        "proxies", "proxy_on_challenge", "proxy_assign_counter",
        "context_proxies", "incognito_mode", "zoom_pct",
        "windows_per_proxy", "layout_group_id", "headless_mode",
        "minimized_mode",
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
        self.headless_mode: bool = False
        self.minimized_mode: bool = False
        self.zoom_pct: int = 100
        self.windows_per_proxy: int = 4
        self.layout_group_id: Optional[str] = None


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
        self._layout_reservations: Dict[str, _LayoutReservation] = {}
        self._layout_lock = asyncio.Lock()
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
        headless: Optional[bool] = None,
        minimized: Optional[bool] = None,
        incognito: Optional[bool] = None,
        proxies: Optional[List[dict]] = None,
        proxy_on_challenge: bool = False,
        windows_per_proxy: int = 4,
        zoom_pct: int = 100,
        run_id: Optional[str] = None,
        layout_group_id: Optional[str] = None,
        total_windows: Optional[int] = None,
        tile_offset: int = 0,
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
        if total_windows is not None and tile_offset + count > total_windows:
            raise ValueError(
                "tile_offset + count cannot exceed total_windows"
            )

        async with self._layout_lock:
            gkey = run_id or self._default_group_key
            await self._close_contexts_unlocked(gkey)

            group = _RunGroup()
            group.layout_group_id = layout_group_id

            disp = display_override or self._config.display
            monitor_work_areas = self._resolve_monitor_work_areas(disp)
            group.headless_mode = (
                self._config.browser.headless if headless is None else headless
            )
            group.minimized_mode = (
                False if group.headless_mode
                else (
                    self._config.browser.minimized
                    if minimized is None else minimized
                )
            )
            group.incognito_mode = (
                self._config.browser.incognito if incognito is None else incognito
            )

            retile_tasks: list[asyncio.Task] = []
            if total_windows is not None and layout_group_id:
                reservation = self._layout_reservations.get(layout_group_id)
                if reservation is None:
                    existing_context_entries = self._collect_existing_context_entries()
                    reservation = _LayoutReservation(
                        base_offset=len(existing_context_entries),
                        total_windows=total_windows,
                    )
                    self._layout_reservations[layout_group_id] = reservation
                    all_tiles = compute_tile_positions(
                        count=reservation.base_offset + reservation.total_windows,
                        monitor_work_areas=monitor_work_areas,
                        margin=disp.margin,
                        border_offset=disp.border_offset,
                    )
                    for tile_idx, (other_key, idx, ctx) in enumerate(existing_context_entries):
                        tile = all_tiles[tile_idx]
                        other_group = self._groups.get(other_key)
                        if not other_group:
                            continue
                        if len(other_group.tiles) <= idx:
                            other_group.tiles.extend(
                                [tile] * (idx + 1 - len(other_group.tiles))
                            )
                        else:
                            other_group.tiles[idx] = tile
                        retile_tasks.append(
                            asyncio.create_task(
                                self._retile_context(
                                    ctx,
                                    tile,
                                    minimized=other_group.minimized_mode,
                                )
                            )
                        )

                all_tiles = compute_tile_positions(
                    count=reservation.base_offset + reservation.total_windows,
                    monitor_work_areas=monitor_work_areas,
                    margin=disp.margin,
                    border_offset=disp.border_offset,
                )
                start = reservation.base_offset + tile_offset
                group.tiles = all_tiles[start : start + count]
            elif total_windows is not None:
                # Backward-compatible pre-computed tiling for a single run.
                all_tiles = compute_tile_positions(
                    count=total_windows,
                    monitor_work_areas=monitor_work_areas,
                    margin=disp.margin,
                    border_offset=disp.border_offset,
                )
                group.tiles = all_tiles[tile_offset : tile_offset + count]
            else:
                # Global tiling across all active runs to avoid overlap.
                existing_context_entries = self._collect_existing_context_entries()
                all_tiles = compute_tile_positions(
                    count=len(existing_context_entries) + count,
                    monitor_work_areas=monitor_work_areas,
                    margin=disp.margin,
                    border_offset=disp.border_offset,
                )
                tile_cursor = 0
                for other_key, idx, ctx in existing_context_entries:
                    tile = all_tiles[tile_cursor]
                    tile_cursor += 1
                    other_group = self._groups.get(other_key)
                    if not other_group:
                        continue
                    if len(other_group.tiles) <= idx:
                        other_group.tiles.extend(
                            [tile] * (idx + 1 - len(other_group.tiles))
                        )
                    else:
                        other_group.tiles[idx] = tile
                    retile_tasks.append(
                        asyncio.create_task(
                            self._retile_context(
                                ctx,
                                tile,
                                minimized=other_group.minimized_mode,
                            )
                        )
                    )

                group.tiles = all_tiles[tile_cursor : tile_cursor + count]

            self._groups[gkey] = group

            if retile_tasks:
                await asyncio.gather(*retile_tasks, return_exceptions=True)

            group.proxies = proxies or []
            group.proxy_on_challenge = proxy_on_challenge
            group.proxy_assign_counter = 0
            group.context_proxies.clear()
            group.zoom_pct = max(25, min(200, int(zoom_pct)))
            group.windows_per_proxy = max(1, int(windows_per_proxy))

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
                # If proxy_on_challenge, proactively distribute proxies across windows
                if group.proxy_on_challenge:
                    proxy = self._assign_proactive_proxy(group, i)
                else:
                    proxy = (
                        group.proxies[i % len(group.proxies)]
                        if group.proxies else None
                    )
                ctx = await self._launch_context(
                    profile_dir, tile, proxy=proxy,
                    headless_mode=group.headless_mode,
                    incognito_mode=group.incognito_mode,
                    zoom_pct=group.zoom_pct,
                )
                if not group.headless_mode:
                    await self._retile_context(
                        ctx, tile, minimized=group.minimized_mode
                    )

                group.contexts.append(ctx)
                group.context_dirs[i] = profile_dir
                group.context_proxies[i] = proxy.get("server") if proxy else None
                logger.info(
                    "Context %d (run=%s) launched at (%d, %d) size %dx%d",
                    i, gkey[:8], tile.x, tile.y, tile.width, tile.height,
                )

            # Log proxy distribution summary
            proxy_summary: Dict[str, int] = {}
            for idx in range(count):
                key = group.context_proxies.get(idx) or "bare-ip"
                proxy_summary[key] = proxy_summary.get(key, 0) + 1
            logger.info(
                "Proxy distribution for run %s: %s",
                gkey[:8],
                {k: f"{v} windows" for k, v in proxy_summary.items()},
            )

            return list(group.contexts)

    def _resolve_monitor_work_areas(
        self,
        disp: DisplayConfig,
    ) -> List[MonitorWorkArea]:
        detected = self._detect_windows_monitor_work_areas()
        if detected:
            start_idx = min(max(disp.start_monitor - 1, 0), len(detected) - 1)
            end_idx = min(start_idx + disp.monitor_count, len(detected))
            selected = detected[start_idx:end_idx]
            logger.info(
                "Using detected monitor work areas: %s",
                [(m.x, m.y, m.width, m.height) for m in selected],
            )
            return selected

        selected: List[MonitorWorkArea] = []
        work_height = max(1, disp.monitor_height - disp.taskbar_height)
        for idx in range(disp.monitor_count):
            selected.append(
                MonitorWorkArea(
                    x=(disp.start_monitor - 1 + idx) * disp.monitor_width,
                    y=0,
                    width=disp.monitor_width,
                    height=work_height,
                )
            )
        logger.info(
            "Using synthetic monitor work areas: %s",
            [(m.x, m.y, m.width, m.height) for m in selected],
        )
        return selected

    def _detect_windows_monitor_work_areas(self) -> List[MonitorWorkArea]:
        if sys.platform != "win32":
            return []

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong),
            ]

        monitors: List[tuple[int, int, int, int, int]] = []
        user32 = ctypes.windll.user32
        MONITORINFOF_PRIMARY = 1
        monitor_enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(RECT),
            ctypes.c_longlong,
        )

        def _callback(hmonitor, _hdc, _rect, _data) -> int:
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                return 1
            work = info.rcWork
            width = work.right - work.left
            height = work.bottom - work.top
            if width > 0 and height > 0:
                is_primary = 1 if (info.dwFlags & MONITORINFOF_PRIMARY) else 0
                monitors.append((is_primary, work.left, work.top, width, height))
            return 1

        try:
            ok = user32.EnumDisplayMonitors(
                0,
                0,
                monitor_enum_proc(_callback),
                0,
            )
            if not ok:
                return []
        except Exception as exc:
            logger.warning("Failed to detect monitor work areas: %s", exc)
            return []

        monitors.sort(key=lambda item: (0 if item[0] else 1, item[1], item[2]))
        return [
            MonitorWorkArea(x=x, y=y, width=width, height=height)
            for _primary, x, y, width, height in monitors
        ]

    def _collect_existing_context_entries(
        self,
    ) -> list[tuple[str, int, BrowserContext]]:
        entries: list[tuple[str, int, BrowserContext]] = []
        for other_key, other_group in self._groups.items():
            for idx, ctx in enumerate(other_group.contexts):
                entries.append((other_key, idx, ctx))
        return entries

    def _cleanup_layout_reservation(self, layout_group_id: Optional[str]) -> None:
        if not layout_group_id:
            return
        for group in self._groups.values():
            if group.layout_group_id == layout_group_id:
                return
        self._layout_reservations.pop(layout_group_id, None)

    async def _close_contexts_unlocked(self, gkey: str) -> None:
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
        self._cleanup_layout_reservation(group.layout_group_id)
        logger.info("Browser contexts closed for run %s", gkey[:8])

    def _assign_proactive_proxy(
        self, group: _RunGroup, index: int
    ) -> Optional[dict]:
        """Assign a proxy proactively using IP-slot grouping.

        Groups ``windows_per_proxy`` consecutive windows onto the same
        proxy IP.  First window in each slot fetches a fresh proxy from
        the pool (or manual list); subsequent windows in the slot reuse it.
        """
        wpp = group.windows_per_proxy
        first_in_slot = (index // wpp) * wpp

        # Reuse the proxy already assigned to the first window in this slot
        if index != first_in_slot and first_in_slot in group.context_proxies:
            existing_server = group.context_proxies[first_in_slot]
            if existing_server is not None:
                return self._find_proxy_dict(group, existing_server)
            return None  # slot is proxyless

        # First window in slot — get a fresh proxy
        if self._proxy_pool and self._proxy_pool.healthy_count > 0:
            proxy = self._proxy_pool.get_next_healthy()
            if proxy:
                logger.info(
                    "Proactive slot %d: assigning proxy %s to context %d",
                    index // wpp, proxy.get("server", "???"), index,
                )
                return proxy

        # Fallback: round-robin through manual list
        if group.proxies:
            proxy = group.proxies[group.proxy_assign_counter % len(group.proxies)]
            group.proxy_assign_counter += 1
            return proxy

        # No proxies available — bare IP
        return None

    def _find_proxy_dict(
        self, group: _RunGroup, server: str
    ) -> Optional[dict]:
        """Look up the full proxy dict (with credentials) for a server string."""
        if self._proxy_pool:
            entry = self._proxy_pool._entries.get(server)
            if entry:
                return entry.to_playwright_dict()
        for p in group.proxies:
            if p.get("server") == server:
                return p
        return None

    def _pick_pool_proxy(
        self,
        avoid_server: Optional[str] = None,
    ) -> Optional[dict]:
        """Pick a healthy pool proxy, avoiding *avoid_server* when possible."""
        if self._proxy_pool is None or self._proxy_pool.healthy_count <= 0:
            return None

        attempts = max(1, self._proxy_pool.healthy_count)
        fallback: Optional[dict] = None
        for _ in range(attempts):
            candidate = self._proxy_pool.get_next_healthy()
            if not candidate:
                break
            if fallback is None:
                fallback = candidate
            server = candidate.get("server")
            if avoid_server and server == avoid_server and attempts > 1:
                continue
            return candidate

        # If only the avoided server is available, force a direct fallback.
        if fallback and avoid_server and fallback.get("server") == avoid_server:
            return None
        return fallback

    def _pick_manual_proxy(
        self,
        group: _RunGroup,
        avoid_server: Optional[str] = None,
    ) -> Optional[dict]:
        """Round-robin through manual proxies, avoiding *avoid_server* if possible."""
        if not group.proxies:
            return None

        total = len(group.proxies)
        fallback: Optional[dict] = None
        for _ in range(total):
            candidate = group.proxies[group.proxy_assign_counter % total]
            group.proxy_assign_counter += 1
            if fallback is None:
                fallback = candidate
            server = candidate.get("server")
            if avoid_server and server == avoid_server and total > 1:
                continue
            return candidate

        if fallback and avoid_server and fallback.get("server") == avoid_server:
            return None
        return fallback

    async def _retile_context(
        self,
        ctx: BrowserContext,
        tile: TileLayout,
        minimized: bool = False,
    ) -> None:
        """Move and resize an already-open Chromium window."""
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            session = None
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                session = await ctx.new_cdp_session(page)
                window_info = await session.send("Browser.getWindowForTarget")
                window_id = window_info["windowId"]

                # Some Chromium builds ignore geometry updates until the window
                # has been restored to "normal" first.
                await session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {"windowState": "normal"},
                    },
                )
                await session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {
                            "left": tile.x,
                            "top": tile.y,
                            "width": tile.width,
                            "height": tile.height,
                        },
                    },
                )
                if minimized:
                    await session.send(
                        "Browser.setWindowBounds",
                        {
                            "windowId": window_id,
                            "bounds": {"windowState": "minimized"},
                        },
                    )
                return
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.2 * (attempt + 1))
            finally:
                if session is not None:
                    try:
                        await session.detach()
                    except Exception:
                        pass
        logger.warning("Window re-tiling failed after retries: %s", last_exc)

    async def close_contexts(self, run_id: Optional[str] = None) -> None:
        """Close browser contexts for a specific run (or default group).

        If *run_id* is ``None``, closes the default group for backward compat.
        """
        gkey = run_id or self._default_group_key
        async with self._layout_lock:
            await self._close_contexts_unlocked(gkey)

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

    async def close_open_windows(self) -> None:
        """Close every open browser window without stopping Playwright itself."""
        for gkey in list(self._groups):
            await self.close_contexts(run_id=gkey)

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
        old_proxy = group.context_proxies.get(index)
        if group.proxy_on_challenge:
            # Immediately penalize the previous proxy for this window.
            if old_proxy and self._proxy_pool:
                self._proxy_pool.mark_unhealthy(old_proxy)

            proxy = self._pick_pool_proxy(avoid_server=old_proxy)
            if proxy is None:
                proxy = self._pick_manual_proxy(group, avoid_server=old_proxy)

            if proxy:
                logger.info(
                    "Challenge mode: assigning proxy %s to context %d (prev=%s)",
                    proxy.get("server", "???"),
                    index,
                    old_proxy or "none",
                )
            else:
                logger.warning(
                    "Challenge mode: no alternate proxy for context %d (prev=%s); using direct/no proxy fallback",
                    index,
                    old_proxy or "none",
                )
        else:
            proxy = group.proxies[index % len(group.proxies)] if group.proxies else None
        ctx = await self._launch_context(
            profile_dir, tile, proxy=proxy,
            headless_mode=group.headless_mode,
            incognito_mode=group.incognito_mode,
            zoom_pct=group.zoom_pct,
        )
        if not group.headless_mode:
            await self._retile_context(
                ctx, tile, minimized=group.minimized_mode
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

    def get_all_pages(self) -> list:
        """Return (run_id, worker_index, Page) for every active context."""
        result = []
        for gkey, group in self._groups.items():
            for idx, ctx in enumerate(group.contexts):
                pages = ctx.pages
                if pages:
                    result.append((gkey, idx, pages[0]))
        return result

    async def focus_window(
        self,
        worker_index: int,
        run_id: Optional[str] = None,
    ) -> dict:
        """Bring a headed Chromium window to front and maximize it.

        Returns a small status dict so the API/UI can surface a helpful
        message instead of guessing why a window could not be shown.
        """
        gkey = run_id or self._default_group_key
        group = self._groups.get(gkey)
        if group is None:
            return {
                "ok": False,
                "reason": "run_not_found",
                "message": "Run is no longer active.",
            }

        if group.headless_mode:
            return {
                "ok": False,
                "reason": "headless",
                "message": "Original window is unavailable in headless mode.",
            }
        if worker_index < 0 or worker_index >= len(group.contexts):
            return {
                "ok": False,
                "reason": "worker_not_found",
                "message": "Window was not found.",
            }

        ctx = group.contexts[worker_index]
        pages = ctx.pages
        if not pages:
            return {
                "ok": False,
                "reason": "page_not_found",
                "message": "Window page is not available yet.",
            }

        page = pages[0]
        maximized = False
        try:
            await page.bring_to_front()
        except Exception as exc:
            logger.debug(
                "Failed to bring window to front (run=%s worker=%d): %s",
                gkey,
                worker_index,
                exc,
                exc_info=True,
            )

        try:
            session = await ctx.new_cdp_session(page)
            window_info = await session.send("Browser.getWindowForTarget")
            window_id = window_info.get("windowId")
            if window_id is not None:
                await session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {"windowState": "maximized"},
                    },
                )
                maximized = True
        except Exception as exc:
            logger.debug(
                "Failed to maximize window (run=%s worker=%d): %s",
                gkey,
                worker_index,
                exc,
                exc_info=True,
            )

        try:
            await page.evaluate("() => window.focus()")
        except Exception:
            pass

        return {
            "ok": True,
            "reason": "ok",
            "maximized": maximized,
            "message": "Window opened.",
        }

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

    def report_proxy_failure(
        self,
        index: int,
        run_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Penalize the current proxy for a worker context."""
        if self._proxy_pool is None:
            return
        gkey = run_id or self._default_group_key
        group = self._groups.get(gkey)
        if not group:
            return
        server = group.context_proxies.get(index)
        if not server:
            return

        # Navigation timeouts are usually hard proxy failures; apply extra penalty.
        strikes = 2 if reason == "goto_timeout" else 1
        for _ in range(strikes):
            self._proxy_pool.mark_unhealthy(server)
        logger.info(
            "Proxy failure reported for context %d (run=%s, server=%s, reason=%s, strikes=%d)",
            index,
            gkey[:8],
            server,
            reason or "unknown",
            strikes,
        )

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
        headless_mode: bool = False,
        incognito_mode: bool = False,
        zoom_pct: int = 100,
    ) -> BrowserContext:
        launch_kwargs = dict(
            user_data_dir=str(profile_dir),
            headless=headless_mode,
            ignore_default_args=["--enable-automation"],
        )
        if headless_mode:
            launch_kwargs["viewport"] = {"width": 1280, "height": 800}
            launch_kwargs["args"] = [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-first-run",
            ]
        else:
            launch_kwargs["no_viewport"] = True
            launch_kwargs["args"] = self._launch_args(tile, incognito_mode, zoom_pct)
        if proxy:
            launch_kwargs["proxy"] = proxy
            logger.info("Launching context with proxy: %s", proxy.get("server", "???"))

        ctx = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        await self._configure_browser_zoom_extension(ctx, zoom_pct, incognito_mode)

        # Apply stealth to existing and future pages
        from src.browser.stealth import apply_stealth

        await apply_stealth(ctx)
        return ctx
