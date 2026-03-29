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
    ) -> Tuple[str, str, Optional[str], Optional[str]]:
        """Poll until both panels are done.

        Returns ``(response_a, response_b, model_a_name, model_b_name)``.
        Raises ``PollingTimeoutError`` if responses don't stabilise in time.
        """
        deadline = time.monotonic() + self._config.response_timeout_seconds
        interval = self._config.poll_interval_seconds
        required = self._config.stable_polls_required

        prev_left = ""
        prev_right = ""
        stable_left = 0
        stable_right = 0

        sel_left = selectors.get("response_panel.left")
        sel_right = selectors.get("response_panel.right")

        while time.monotonic() < deadline:
            cur_left = await self._extract_text(page, sel_left)
            cur_right = await self._extract_text(page, sel_right)

            # Track stability
            if cur_left and cur_left == prev_left:
                stable_left += 1
            else:
                stable_left = 0

            if cur_right and cur_right == prev_right:
                stable_right += 1
            else:
                stable_right = 0

            prev_left = cur_left
            prev_right = cur_right

            if stable_left >= required and stable_right >= required:
                logger.info(
                    "Worker %d: both panels stable after %d checks",
                    worker_id,
                    required,
                )
                model_a = await self._extract_model_name(page, selectors, "left")
                model_b = await self._extract_model_name(page, selectors, "right")
                return cur_left, cur_right, model_a, model_b

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
        page: Page, selectors: SelectorRegistry, side: str
    ) -> Optional[str]:
        """Try to read the model name label for a given side."""
        try:
            sel = selectors.get(f"model_name_label.{side}")
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        except (KeyError, Exception) as exc:
            logger.debug("Model name extraction failed for %s: %s", side, exc)
        return None
