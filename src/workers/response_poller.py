from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Tuple

from playwright.async_api import Page

from src.browser.selectors import SelectorRegistry
from src.core.exceptions import PollingTimeoutError, ResponseExtractionError
from src.models.config import TimingConfig

logger = logging.getLogger(__name__)


class ResponsePoller:
    """Polls Arena DOM until both response panels are stable.

    "Stable" means the text content has not changed for
    ``stable_polls_required`` consecutive checks at ``poll_interval_seconds``
    intervals.
    """

    def __init__(self, config: TimingConfig) -> None:
        self._config = config

    async def poll(
        self,
        page: Page,
        selectors: SelectorRegistry,
        worker_id: int = -1,
    ) -> Tuple[str, Optional[str]]:
        """Poll until the response is stable.

        Returns ``(response, model_name)``.
        Raises ``PollingTimeoutError`` if response doesn't stabilise in time.
        """
        deadline = time.monotonic() + self._config.response_timeout_seconds
        interval = self._config.poll_interval_seconds
        required = self._config.stable_polls_required

        prev_text = ""
        stable_count = 0

        sel_response = selectors.get("response_panel")

        while time.monotonic() < deadline:
            cur_text = await self._extract_text(page, sel_response)

            if cur_text and cur_text == prev_text:
                stable_count += 1
            else:
                stable_count = 0

            prev_text = cur_text

            if stable_count >= required:
                logger.info(
                    "Worker %d: response stable after %d checks",
                    worker_id,
                    required,
                )
                model_name = await self._extract_model_name(page, selectors)
                return cur_text, model_name

            await asyncio.sleep(interval)

        raise PollingTimeoutError(
            worker_id=worker_id,
            timeout_seconds=self._config.response_timeout_seconds,
        )

    @staticmethod
    async def _extract_text(page: Page, selector: str) -> str:
        """Read inner text from a selector. Returns empty string on failure."""
        try:
            el = await page.query_selector(selector)
            if el:
                return (await el.inner_text()).strip()
        except Exception as exc:
            logger.debug("Text extraction failed for %s: %s", selector, exc)
        return ""

    @staticmethod
    async def _extract_model_name(
        page: Page, selectors: SelectorRegistry
    ) -> Optional[str]:
        """Try to read the selected model name."""
        try:
            sel = selectors.get("model_name_label")
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        except (KeyError, Exception) as exc:
            logger.debug("Model name extraction failed: %s", exc)
        return None
