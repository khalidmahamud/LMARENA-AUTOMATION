from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

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
        on_slide_stable: Optional[
            Callable[[int, str, str, Optional[str]], Coroutine[Any, Any, None]]
        ] = None,
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
        stable_count_a = 0
        stable_count_b = 0
        emitted_a = False
        emitted_b = False
        retry_counts = [0, 0]
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

            errored_indices = [
                idx for idx, slide in enumerate(slides[:2])
                if slide.get("has_error")
            ]
            if errored_indices:
                for idx in errored_indices:
                    if retry_counts[idx] >= 2:
                        continue
                    if await self._click_retry_button(page, slide_sel, idx):
                        retry_counts[idx] += 1
                        logger.warning(
                            "Worker %d: response %d failed; clicked retry button (attempt %d)",
                            worker_id,
                            idx + 1,
                            retry_counts[idx],
                        )
                stable_count = 0
                stable_count_a = 0
                stable_count_b = 0
                emitted_a = False
                emitted_b = False
                prev_text_a = ""
                prev_text_b = ""
                await self._sleep_with_controls(
                    interval,
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                )
                continue

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
            stop_generation_active = await self._has_visible_element(
                page,
                stop_generation_sel,
            )

            # Per-slide streaming indicators
            streaming_a = (
                slides[0]["has_streaming_indicator"]
                if len(slides) > 0
                else False
            )
            streaming_b = (
                slides[1]["has_streaming_indicator"]
                if len(slides) > 1
                else False
            )
            streaming_active = (
                streaming_a or streaming_b or stop_generation_active
            )

            # Per-slide stability: track each model independently
            baseline_ok_a = (
                baseline_responses is None or cur_a != baseline_a
            )
            baseline_ok_b = (
                baseline_responses is None or cur_b != baseline_b
            )

            copy_ready_a = (
                len(slides) > 0 and slides[0]["has_copy_button"]
            )
            copy_ready_b = (
                len(slides) > 1 and slides[1]["has_copy_button"]
            )

            if (
                cur_a
                and baseline_ok_a
                and cur_a == prev_text_a
                and (copy_ready_a or not streaming_a)
            ):
                stable_count_a += 1
            else:
                stable_count_a = 0

            if (
                cur_b
                and baseline_ok_b
                and cur_b == prev_text_b
                and (copy_ready_b or not streaming_b)
            ):
                stable_count_b += 1
            else:
                stable_count_b = 0

            # Emit partial result as soon as each model stabilises
            if (
                on_slide_stable
                and stable_count_a >= required
                and not emitted_a
            ):
                emitted_a = True
                model_name_a = (
                    slides[0]["model_name"] if len(slides) > 0 else None
                )
                await on_slide_stable(0, cur_a, cur_html_a, model_name_a)

            if (
                on_slide_stable
                and stable_count_b >= required
                and not emitted_b
            ):
                emitted_b = True
                model_name_b = (
                    slides[1]["model_name"] if len(slides) > 1 else None
                )
                await on_slide_stable(1, cur_b, cur_html_b, model_name_b)

            # Overall stability: both models stable + global indicators clear
            if (
                stable_count_a >= required
                and stable_count_b >= required
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

                    const hasRetryIcon = (button) => {
                        const paths = Array.from(
                            button.querySelectorAll("svg path")
                        ).map((path) => path.getAttribute("d") || "");
                        return paths.some((d) =>
                            d.includes("M21.8883 13.5") ||
                            d.includes("M17 8H21.4")
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
                            const retryButton = Array.from(
                                slide.querySelectorAll("button[type='button']")
                            ).find((btn) => isVisible(btn) && hasRetryIcon(btn));
                            const streamingNode = slide.querySelector(
                                streamingSelector
                            );
                            const errorNode = Array.from(
                                slide.querySelectorAll("p, span, div")
                            ).find((node) => {
                                if (!isVisible(node)) return false;
                                const text = (node.textContent || "").trim();
                                return text.includes(
                                    "Something went wrong with this response, please try again."
                                );
                            });

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
                                has_error: Boolean(errorNode),
                                has_retry_button: Boolean(retryButton),
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

    @staticmethod
    async def _click_retry_button(
        page: Page,
        slide_selector: str,
        slide_index: int,
    ) -> bool:
        try:
            return bool(
                await page.evaluate(
                    """({ slideSelector, slideIndex }) => {
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

                        const hasRetryIcon = (button) => {
                            const paths = Array.from(
                                button.querySelectorAll("svg path")
                            ).map((path) => path.getAttribute("d") || "");
                            return paths.some((d) =>
                                d.includes("M21.8883 13.5") ||
                                d.includes("M17 8H21.4")
                            );
                        };

                        const slides = Array.from(
                            document.querySelectorAll(slideSelector)
                        ).filter(isVisible);
                        const slide = slides[slideIndex];
                        if (!slide) return false;

                        const retryButton = Array.from(
                            slide.querySelectorAll("button[type='button']")
                        ).find((btn) => isVisible(btn) && hasRetryIcon(btn));
                        if (!retryButton) return false;
                        retryButton.click();
                        return true;
                    }""",
                    {
                        "slideSelector": slide_selector,
                        "slideIndex": slide_index,
                    },
                )
            )
        except Exception as exc:
            logger.debug(
                "Retry button click failed for slide %d: %s",
                slide_index,
                exc,
            )
            return False
