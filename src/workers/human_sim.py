from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import Page

from src.models.config import TypingConfig

logger = logging.getLogger(__name__)


class HumanSimulator:
    """Human-like typing, clicking, and delay helpers.

    All delays are randomised within configured bounds to avoid
    detectable patterns.
    """

    def __init__(self, config: TypingConfig) -> None:
        self._config = config

    async def type_text(self, page: Page, selector: str, text: str) -> None:
        """Click the element then type *text* with per-keystroke random delay."""
        await page.click(selector)
        await asyncio.sleep(random.uniform(0.2, 0.5))

        for char in text:
            await page.keyboard.press(char if len(char) == 1 else char)
            delay_ms = random.randint(
                self._config.min_delay_ms, self._config.max_delay_ms
            )
            await asyncio.sleep(delay_ms / 1000.0)

        logger.debug("Typed %d characters into %s", len(text), selector)

    async def click(self, page: Page, selector: str) -> None:
        """Move mouse to element, pause briefly, then click."""
        element = await page.wait_for_selector(selector, timeout=10_000)
        if element is None:
            raise RuntimeError(f"Element not found: {selector}")

        box = await element.bounding_box()
        if box:
            # Move to a random point within the element
            x = box["x"] + random.uniform(box["width"] * 0.2, box["width"] * 0.8)
            y = box["y"] + random.uniform(box["height"] * 0.2, box["height"] * 0.8)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.05, 0.2))
            await page.mouse.click(x, y)
        else:
            await element.click()

        logger.debug("Clicked %s", selector)

    @staticmethod
    async def random_delay(base_seconds: float, jitter_pct: float) -> None:
        """Sleep for *base_seconds* ± *jitter_pct* (0.0–1.0)."""
        jitter = base_seconds * jitter_pct
        actual = base_seconds + random.uniform(-jitter, jitter)
        actual = max(0.1, actual)  # never negative / near-zero
        await asyncio.sleep(actual)
