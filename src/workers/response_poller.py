from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from playwright.async_api import Page

from src.browser.selectors import SelectorRegistry
from src.browser.challenges import ChallengeType, detect_challenge, detect_login_dialog
from src.core.exceptions import LoginDialogError, PollingTimeoutError, RateLimitError
from src.models.config import TimingConfig

logger = logging.getLogger(__name__)


class ResponsePoller:
    """Polls Arena DOM until both side-by-side response panels are stable.

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
        cancel_event: Optional[asyncio.Event] = None,
        pause_event: Optional[asyncio.Event] = None,
        baseline_responses: Optional[Tuple[str, str]] = None,
    ) -> Tuple[Tuple[str, str], Tuple[Optional[str], Optional[str]], Tuple[str, str]]:
        """Poll until both responses are stable.

        Returns ``((response_a, response_b), (model_a_name, model_b_name), (html_a, html_b))``.
        Raises ``PollingTimeoutError`` if responses don't stabilise in time.
        """
        deadline = time.monotonic() + self._config.response_timeout_seconds
        interval = self._config.poll_interval_seconds
        required = self._config.stable_polls_required

        prev_text_a = ""
        prev_text_b = ""
        stable_count = 0
        baseline_a = (
            self._normalize_text(baseline_responses[0])
            if baseline_responses
            else ""
        )
        baseline_b = (
            self._normalize_text(baseline_responses[1])
            if baseline_responses
            else ""
        )

        slide_sel = selectors.get("response_slide")
        streaming_indicator_sel = selectors.get("streaming_indicator")
        stop_generation_sel = selectors.get("stop_generation_button")

        # Inject CSS once to hide "Thinking..." disclosure panels
        await self._hide_thinking_boxes(page)

        while time.monotonic() < deadline:
            await self._wait_if_paused(cancel_event, pause_event)

            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Run cancelled")

            # Check for login dialog during polling — raise to trigger window recreation
            try:
                if await detect_login_dialog(page):
                    logger.warning("Worker %d: login dialog detected during polling", worker_id)
                    raise LoginDialogError(worker_id)
            except LoginDialogError:
                raise
            except Exception:
                if cancel_event and cancel_event.is_set():
                    raise asyncio.CancelledError("Run cancelled")

            # Check for rate limit banner during polling
            try:
                challenge = await detect_challenge(page)
                if challenge == ChallengeType.RATE_LIMIT:
                    logger.warning(
                        "Worker %d: rate limit detected during polling",
                        worker_id,
                    )
                    raise RateLimitError(worker_id)
            except RateLimitError:
                raise
            except Exception:
                pass

            slides = await self._extract_slide_payloads(
                page,
                slide_sel,
                streaming_indicator_sel,
            )

            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Run cancelled")

            cur_a = (
                self._normalize_text(slides[0]["response_text"])
                if len(slides) > 0
                else ""
            )
            cur_b = (
                self._normalize_text(slides[1]["response_text"])
                if len(slides) > 1
                else ""
            )
            cur_html_a = (
                slides[0]["response_html"]
                if len(slides) > 0
                else ""
            )
            cur_html_b = (
                slides[1]["response_html"]
                if len(slides) > 1
                else ""
            )
            copy_ready = (
                len(slides) >= 2
                and slides[0]["has_copy_button"]
                and slides[1]["has_copy_button"]
            )
            slide_streaming_active = any(
                slide["has_streaming_indicator"] for slide in slides[:2]
            )
            stop_generation_active = await self._has_visible_element(
                page,
                stop_generation_sel,
            )
            streaming_active = (
                slide_streaming_active or stop_generation_active
            )

            if (
                cur_a
                and cur_b
                and (
                    baseline_responses is None
                    or (cur_a != baseline_a and cur_b != baseline_b)
                )
                and cur_a == prev_text_a
                and cur_b == prev_text_b
                and (copy_ready or not streaming_active)
            ):
                stable_count += 1
            else:
                stable_count = 0

            prev_text_a = cur_a
            prev_text_b = cur_b

            if stable_count >= required:
                logger.info(
                    "Worker %d: both responses stable after %d checks",
                    worker_id,
                    required,
                )
                model_names = (
                    slides[0]["model_name"] if len(slides) > 0 else None,
                    slides[1]["model_name"] if len(slides) > 1 else None,
                )
                return (cur_a, cur_b), model_names, (cur_html_a, cur_html_b)

            # Cancellation-aware sleep
            await self._sleep_with_controls(
                interval,
                cancel_event=cancel_event,
                pause_event=pause_event,
            )

        raise PollingTimeoutError(
            worker_id=worker_id,
            timeout_seconds=self._config.response_timeout_seconds,
        )

    @staticmethod
    async def _hide_thinking_boxes(page: Page) -> None:
        """Inject a CSS rule to collapse 'Thinking...' disclosure panels."""
        try:
            await page.evaluate(
                """() => {
                    if (document.getElementById('_arena_hide_thinking')) return;
                    const style = document.createElement('style');
                    style.id = '_arena_hide_thinking';
                    style.textContent = `
                        /* Hide reasoning/thinking accordions inside model slides */
                        [role='group'][aria-roledescription='slide'] div.not-prose[data-state] {
                            display: none !important;
                        }
                    `;
                    document.head.appendChild(style);
                }"""
            )
        except Exception:
            pass

    @staticmethod
    def _normalize_text(text: str) -> str:
        return " ".join(
            text.replace("\u200b", "").replace("\ufeff", "").split()
        ).strip()

    @staticmethod
    async def _has_visible_element(
        page: Page,
        selector: str,
    ) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """(selector) => {
                        const isVisible = (el) => {
                            if (!el) return false;
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return (
                                style.display !== "none" &&
                                style.visibility !== "hidden" &&
                                rect.width > 0 &&
                                rect.height > 0
                            );
                        };

                        return Array.from(document.querySelectorAll(selector))
                            .some(isVisible);
                    }""",
                    selector,
                )
            )
        except Exception as exc:
            logger.debug(
                "Visible-element check failed for %s: %s",
                selector,
                exc,
            )
            return False

    @staticmethod
    async def _wait_if_paused(
        cancel_event: Optional[asyncio.Event],
        pause_event: Optional[asyncio.Event],
    ) -> None:
        if pause_event is None:
            return

        while not pause_event.is_set():
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Run cancelled")
            try:
                await asyncio.wait_for(pause_event.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    async def _sleep_with_controls(
        self,
        duration: float,
        cancel_event: Optional[asyncio.Event] = None,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        deadline = time.monotonic() + duration
        while True:
            await self._wait_if_paused(cancel_event, pause_event)
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("Run cancelled")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return

            if cancel_event:
                try:
                    await asyncio.wait_for(
                        cancel_event.wait(),
                        timeout=min(remaining, 0.25),
                    )
                    raise asyncio.CancelledError("Run cancelled")
                except asyncio.TimeoutError:
                    continue

            await asyncio.sleep(min(remaining, 0.25))

    @staticmethod
    async def _extract_slide_payloads(
        page: Page,
        slide_selector: str,
        streaming_indicator_selector: str,
    ) -> List[Dict[str, object]]:
        """Return visible slide payloads for the two model response cards."""
        try:
            payloads = await page.evaluate(
                """({ slideSelector, streamingSelector }) => {
                    const isVisible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return (
                            style.display !== "none" &&
                            style.visibility !== "hidden" &&
                            rect.width > 0 &&
                            rect.height > 0
                        );
                    };

                    const hasCopyIcon = (button) => {
                        const paths = Array.from(
                            button.querySelectorAll("svg path")
                        ).map((path) => path.getAttribute("d") || "");
                        return (
                            paths.some((d) => d.includes("M19.4 20H9.6")) &&
                            paths.some((d) => d.includes("M15 9V4.6"))
                        );
                    };

                    return Array.from(document.querySelectorAll(slideSelector))
                        .filter(isVisible)
                        .map((slide) => {
                            const proseNodes = Array.from(
                                slide.querySelectorAll(".prose")
                            ).filter(isVisible);
                            const responseNode = proseNodes.length
                                ? proseNodes[proseNodes.length - 1]
                                : null;
                            const modelNode = slide.querySelector("span.truncate");
                            const copyButton = Array.from(
                                slide.querySelectorAll("button[type='button']")
                            ).find((btn) => isVisible(btn) && hasCopyIcon(btn));
                            const streamingNode = slide.querySelector(
                                streamingSelector
                            );

                            // Strip code-block language labels (e.g. "HTML")
                            // that appear at the start of innerText before an HTML tag
                            let cleanText = "";
                            let rawHtml = "";
                            if (responseNode) {
                                rawHtml = responseNode.innerHTML.trim();
                                cleanText = responseNode.innerText.trim()
                                    .replace(/^(HTML|CSS|JavaScript|JS|TypeScript|TS|Python|JSON|XML|Bash|Shell|SQL|Go|Rust|Java|Ruby|PHP)\\s*(?=<)/i, "");
                            }

                            return {
                                model_name: modelNode
                                    ? modelNode.textContent.trim()
                                    : null,
                                response_text: cleanText,
                                response_html: rawHtml,
                                has_copy_button: Boolean(copyButton),
                                has_streaming_indicator: Boolean(
                                    streamingNode && isVisible(streamingNode)
                                ),
                            };
                        })
                        .slice(0, 2);
                }""",
                {
                    "slideSelector": slide_selector,
                    "streamingSelector": streaming_indicator_selector,
                },
            )
            if isinstance(payloads, list):
                return payloads
        except Exception as exc:
            logger.debug("Slide extraction failed for %s: %s", slide_selector, exc)
        return []
