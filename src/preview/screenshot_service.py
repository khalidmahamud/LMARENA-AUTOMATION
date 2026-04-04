from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Optional, Set, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket
    from playwright.async_api import Page
    from src.browser.manager import BrowserManager
    from src.models.config import PreviewConfig

logger = logging.getLogger(__name__)


class ScreenshotService:
    """Captures screenshots from all active browser pages at a configurable
    interval and broadcasts them as base64-encoded JPEG to WebSocket
    subscribers."""

    def __init__(
        self,
        browser_manager: BrowserManager,
        config: PreviewConfig,
    ) -> None:
        self._browser_manager = browser_manager
        self._config = config
        self._subscribers: Set[WebSocket] = set()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # Subscriber management
    # ------------------------------------------------------------------

    def add_subscriber(self, ws: WebSocket) -> None:
        self._subscribers.add(ws)
        logger.info(
            "Subscriber added (total: %d)", len(self._subscribers)
        )

    def remove_subscriber(self, ws: WebSocket) -> None:
        self._subscribers.discard(ws)
        logger.info(
            "Subscriber removed (total: %d)", len(self._subscribers)
        )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background capture loop."""
        if self._running:
            logger.warning("ScreenshotService is already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._capture_loop())
        logger.info(
            "ScreenshotService started (interval=%.1fs, quality=%d)",
            self._config.interval_seconds,
            self._config.jpeg_quality,
        )

    async def stop(self) -> None:
        """Stop the background capture loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("ScreenshotService stopped")

    # ------------------------------------------------------------------
    # Internal capture loop
    # ------------------------------------------------------------------

    async def _capture_loop(self) -> None:
        """Periodically captures screenshots and broadcasts to subscribers."""
        while self._running:
            try:
                # If nobody is listening, just sleep and check again.
                if not self._subscribers:
                    await asyncio.sleep(self._config.interval_seconds)
                    continue

                screenshots = await self._capture_all_pages()

                if screenshots:
                    message = json.dumps(
                        {
                            "type": "preview_screenshots",
                            "screenshots": screenshots,
                        }
                    )
                    await self._broadcast(message)

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in screenshot capture loop")

            await asyncio.sleep(self._config.interval_seconds)

    async def _capture_all_pages(self) -> List[dict]:
        """Capture a JPEG screenshot from every active page."""
        pages: List[Tuple[str, int, Page]] = (
            self._browser_manager.get_all_pages()
        )

        screenshots: List[dict] = []
        now = time.time()

        for run_id, worker_index, page in pages:
            try:
                raw = await page.screenshot(
                    type="jpeg",
                    quality=self._config.jpeg_quality,
                    full_page=False,
                )
                encoded = base64.b64encode(raw).decode("ascii")
                screenshots.append(
                    {
                        "run_id": run_id,
                        "worker_index": worker_index,
                        "data": encoded,
                        "timestamp": now,
                    }
                )
            except Exception:
                logger.debug(
                    "Failed to capture screenshot for run=%s worker=%d",
                    run_id,
                    worker_index,
                    exc_info=True,
                )

        return screenshots

    async def _broadcast(self, message: str) -> None:
        """Send *message* to every subscriber, removing dead connections."""
        dead: List[WebSocket] = []

        for ws in self._subscribers:
            try:
                await ws.send_text(message)
            except Exception:
                logger.debug("Removing dead subscriber", exc_info=True)
                dead.append(ws)

        for ws in dead:
            self._subscribers.discard(ws)
