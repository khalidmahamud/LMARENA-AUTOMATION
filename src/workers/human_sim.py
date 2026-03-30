from __future__ import annotations

import asyncio
import logging
import random

from playwright.async_api import ElementHandle, Page

from src.models.config import TypingConfig

logger = logging.getLogger(__name__)


class HumanSimulator:
    """Human-like typing, clicking, and delay helpers.

    All delays are randomised within configured bounds to avoid
    detectable patterns.
    """

    def __init__(self, config: TypingConfig) -> None:
        self._config = config

    async def _find_visible_element(
        self,
        page: Page,
        selector: str,
    ) -> ElementHandle:
        """Return the last visible, non-dialog match for *selector*."""
        locator = page.locator(selector)
        count = await locator.count()
        if count == 0:
            raise RuntimeError(f"Element not found: {selector}")

        fallback: ElementHandle | None = None
        for index in range(count - 1, -1, -1):
            handle = await locator.nth(index).element_handle()
            if handle is None:
                continue
            if fallback is None:
                fallback = handle

            is_visible = await handle.evaluate(
                """el => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        !el.closest('[role="dialog"]') &&
                        style.display !== "none" &&
                        style.visibility !== "hidden" &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                }"""
            )
            if is_visible:
                return handle

        if fallback is None:
            raise RuntimeError(f"Element not found: {selector}")
        return fallback

    async def type_text(
        self,
        page: Page,
        selector: str,
        text: str,
    ) -> ElementHandle:
        """Click the element then type *text*.

        Uses real keyboard input for native editors so reactive chat composers
        observe the same input events a user would trigger.
        """
        element = await self._find_visible_element(page, selector)

        tag = await element.evaluate("el => el.tagName.toLowerCase()")
        is_contenteditable = await element.evaluate(
            "el => el.getAttribute('contenteditable') === 'true'"
        )

        if tag in ("input", "textarea"):
            # Native form elements: use real typing to trigger composer state.
            await element.click()
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Backspace")
            await page.keyboard.type(
                text,
                delay=random.randint(
                    self._config.min_delay_ms,
                    self._config.max_delay_ms,
                ),
            )
        elif is_contenteditable:
            # TipTap / ProseMirror: replace via keyboard events.
            await element.click()
            await asyncio.sleep(random.uniform(0.3, 0.6))
            await page.keyboard.press("Control+a")
            await asyncio.sleep(0.1)
            await page.keyboard.press("Backspace")
            await page.keyboard.type(
                text,
                delay=random.randint(
                    self._config.min_delay_ms,
                    self._config.max_delay_ms,
                ),
            )
        else:
            # Fallback: click and type.
            await element.click()
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await page.keyboard.type(
                text,
                delay=random.randint(
                    self._config.min_delay_ms,
                    self._config.max_delay_ms,
                ),
            )

        logger.debug("Typed %d characters into %s", len(text), selector)
        return element

    async def click_element(self, page: Page, element: ElementHandle) -> None:
        """Move mouse to an element handle, pause briefly, then click."""
        box = await element.bounding_box()
        if box:
            x = box["x"] + random.uniform(box["width"] * 0.2, box["width"] * 0.8)
            y = box["y"] + random.uniform(box["height"] * 0.2, box["height"] * 0.8)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.05, 0.2))
            await page.mouse.click(x, y)
        else:
            await element.click()

    async def click(self, page: Page, selector: str) -> None:
        """Move mouse to element, pause briefly, then click."""
        element = await self._find_visible_element(page, selector)
        await self.click_element(page, element)

        logger.debug("Clicked %s", selector)

    @staticmethod
    async def random_delay(base_seconds: float, jitter_pct: float) -> None:
        """Sleep for *base_seconds* +/- *jitter_pct* (0.0-1.0)."""
        jitter = base_seconds * jitter_pct
        actual = base_seconds + random.uniform(-jitter, jitter)
        actual = max(0.1, actual)  # never negative / near-zero
        await asyncio.sleep(actual)
