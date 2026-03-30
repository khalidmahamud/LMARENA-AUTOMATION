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

    @staticmethod
    def _normalize_text(text: str) -> str:
        return text.replace("\r\n", "\n").replace("\r", "\n")

    async def _read_element_text(self, element: ElementHandle) -> str:
        value = await element.evaluate(
            """el => {
                const tag = el.tagName.toLowerCase();
                if (tag === "input" || tag === "textarea") {
                    return el.value || "";
                }
                if (
                    el.isContentEditable ||
                    el.getAttribute("contenteditable") === "true"
                ) {
                    return el.innerText || el.textContent || "";
                }
                return el.value || el.innerText || el.textContent || "";
            }"""
        )
        return value if isinstance(value, str) else ""

    async def _paste_value(
        self,
        page: Page,
        element: ElementHandle,
        text: str,
    ) -> None:
        await element.click()
        await asyncio.sleep(random.uniform(0.15, 0.35))

        await element.evaluate(
            """(el, value) => {
                const tag = el.tagName.toLowerCase();
                const fireInput = (target) => {
                    target.dispatchEvent(
                        new InputEvent("input", {
                            bubbles: true,
                            cancelable: true,
                            inputType: "insertFromPaste",
                            data: value,
                        })
                    );
                    target.dispatchEvent(new Event("change", { bubbles: true }));
                };

                if (tag === "textarea") {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLTextAreaElement.prototype,
                        "value"
                    )?.set;
                    if (setter) setter.call(el, value);
                    else el.value = value;
                    fireInput(el);
                    return;
                }

                if (tag === "input") {
                    const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype,
                        "value"
                    )?.set;
                    if (setter) setter.call(el, value);
                    else el.value = value;
                    fireInput(el);
                    return;
                }

                if (
                    el.isContentEditable ||
                    el.getAttribute("contenteditable") === "true"
                ) {
                    el.focus();
                    el.textContent = value;
                    fireInput(el);
                    return;
                }

                el.textContent = value;
                fireInput(el);
            }""",
            text,
        )

        await asyncio.sleep(random.uniform(0.1, 0.2))

    async def type_text(
        self,
        page: Page,
        selector: str,
        text: str,
    ) -> ElementHandle:
        """Populate the target element with *text* immediately."""
        element = await self._find_visible_element(page, selector)
        await self._paste_value(page, element, text)

        current = self._normalize_text(await self._read_element_text(element))
        expected = self._normalize_text(text)
        if current != expected:
            raise RuntimeError(
                f"Pasted value did not match the requested text for {selector}"
            )

        logger.debug("Pasted %d characters into %s", len(text), selector)
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
