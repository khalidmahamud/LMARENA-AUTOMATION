from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

from playwright.async_api import BrowserContext, ElementHandle, Page

from src.browser.challenges import ChallengeType, detect_challenge
from src.browser.selectors import SelectorRegistry
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import (
    ChallengeDetectedError,
    NavigationError,
)
from src.core.state_machine import WorkerStateMachine
from src.models.config import AppConfig
from src.models.results import WindowResult
from src.models.worker import WorkerState
from src.workers.human_sim import HumanSimulator
from src.workers.response_poller import ResponsePoller

logger = logging.getLogger(__name__)


class ArenaWorker:
    """Full lifecycle manager for a single Arena browser window.

    Owns one ``BrowserContext``, one ``WorkerStateMachine``, and publishes
    all status changes through the shared ``EventBus``.
    """

    # Callback type: async (worker_index) -> new BrowserContext
    ContextRecreator = Callable[[int], Coroutine[None, None, BrowserContext]]

    def __init__(
        self,
        worker_id: int,
        context: BrowserContext,
        config: AppConfig,
        event_bus: EventBus,
        context_recreator: Optional[ContextRecreator] = None,
    ) -> None:
        self._id = worker_id
        self._context = context
        self._config = config
        self._event_bus = event_bus
        self._context_recreator = context_recreator
        self._page: Optional[Page] = None
        self._result: Optional[WindowResult] = None
        self._started_at: Optional[datetime] = None
        self._cancelled = False
        self._cancel_event = asyncio.Event()

        self._selectors = SelectorRegistry.instance()
        self._human = HumanSimulator(config.typing)
        self._poller = ResponsePoller(config.timing)

        self.state_machine = WorkerStateMachine(
            worker_id=worker_id,
            on_transition=self._on_state_transition,
        )

    # ── Event publishing ──

    async def _on_state_transition(
        self, old: WorkerState, new: WorkerState, wid: int
    ) -> None:
        await self._event_bus.publish(
            Event(
                type=EventType.WORKER_STATE_CHANGED,
                worker_id=wid,
                data={
                    "old_state": old.value,
                    "new_state": new.value,
                    "progress": self.state_machine.progress,
                },
            )
        )

    async def _log(self, level: str, text: str) -> None:
        await self._event_bus.publish(
            Event(
                type=EventType.LOG,
                worker_id=self._id,
                data={"level": level, "text": text},
            )
        )

    # ── Navigation ──

    async def navigate_to_arena(
        self, clear_cookies: bool = False, zoom_pct: int = 100
    ) -> None:
        """Navigate to the Arena side-by-side page, handling challenges."""
        self._zoom_pct = zoom_pct
        await self.state_machine.transition(WorkerState.LAUNCHING)

        # Optionally clear cookies before navigation.
        if clear_cookies:
            await self._clear_cookies()

        # Reuse the default page from persistent context (closing all pages
        # kills the browser process), or create one if none exist.
        if self._context.pages:
            self._page = self._context.pages[0]
            # Close any extra restored tabs
            for extra in self._context.pages[1:]:
                try:
                    await extra.close()
                except Exception:
                    pass
        else:
            self._page = await self._context.new_page()

        await self.state_machine.transition(WorkerState.NAVIGATING)
        try:
            await self._page.goto(
                self._config.arena_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as exc:
            await self.state_machine.force_error(str(exc))
            raise NavigationError(str(exc), self._id)

        # Apply page zoom
        if self._zoom_pct != 100:
            await self._page.evaluate(
                "z => document.body.style.zoom = z + '%'", self._zoom_pct
            )

        # Dismiss popups (TOS dialog, login dialog) if present
        await self._dismiss_tos_dialog()
        await self._dismiss_login_dialog()

        # Check for challenges — retry by reopening window if possible
        challenge = await detect_challenge(self._page)
        if challenge != ChallengeType.NONE:
            await self.state_machine.transition(
                WorkerState.WAITING_FOR_CHALLENGE
            )
            await self._event_bus.publish(
                Event(
                    type=EventType.CHALLENGE_DETECTED,
                    worker_id=self._id,
                    data={"challenge_type": challenge.value},
                )
            )
            await self._handle_challenge(challenge)

        await self.state_machine.transition(WorkerState.READY)
        await self._log("info", "Ready")

    async def _clear_cookies(self) -> None:
        """Remove all cookies from the current browser context."""
        try:
            all_cookies = await self._context.cookies()
            await self._context.clear_cookies()
            await self._log(
                "info",
                f"Cleared {len(all_cookies)} cookies",
            )
        except Exception as exc:
            await self._log("debug", f"Cookie clearing failed: {exc}")

    async def _dismiss_tos_dialog(self) -> bool:
        """Click 'Agree' on the Terms of Use dialog if it appears.

        Returns ``True`` if a dialog was actually dismissed.
        """
        try:
            dialog_sel = self._selectors.get("tos_dialog")
            dialog = await self._page.query_selector(dialog_sel)
            if dialog:
                btn_sel = self._selectors.get("tos_agree_button")
                btn = await self._page.query_selector(btn_sel)
                if btn:
                    await self._human.click(self._page, btn_sel)
                    await asyncio.sleep(1)
                    await self._log("info", "Dismissed Terms of Use dialog")
                    return True
        except Exception as exc:
            await self._log("debug", f"No TOS dialog or already dismissed: {exc}")
        return False

    async def _dismiss_login_dialog(self, wait: bool = False) -> None:
        """Close the login dialog by clicking the X button if it appears."""
        try:
            close_sel = self._selectors.get("login_dialog_close")
            if wait:
                close_btn = await self._page.wait_for_selector(
                    close_sel, timeout=5_000
                )
            else:
                close_btn = await self._page.query_selector(close_sel)
            if close_btn:
                await close_btn.click()
                await asyncio.sleep(1)
                await self._log("info", "Dismissed login dialog")
        except Exception:
            pass  # no dialog present — expected

    async def _handle_challenge(
        self,
        challenge: ChallengeType,
        max_retries: int = 3,
        clear_cookies: bool = False,
    ) -> None:
        """Handle a detected challenge by closing the window and reopening.

        If no ``context_recreator`` callback was provided, falls back to
        waiting for manual resolution (legacy behaviour).
        """
        if self.state_machine.state != WorkerState.WAITING_FOR_CHALLENGE:
            await self.state_machine.transition(WorkerState.WAITING_FOR_CHALLENGE)

        if self._context_recreator is None:
            # No recreator — wait for manual resolution
            await self._log(
                "warning",
                f"Challenge ({challenge.value}) detected — "
                "please solve it manually in the browser window",
            )
            deadline = asyncio.get_event_loop().time() + 120
            while asyncio.get_event_loop().time() < deadline:
                if self._cancelled:
                    return
                if await detect_challenge(self._page) == ChallengeType.NONE:
                    await self._event_bus.publish(
                        Event(type=EventType.CHALLENGE_RESOLVED, worker_id=self._id)
                    )
                    return
                await asyncio.sleep(2)
            raise ChallengeDetectedError(self._id, "timeout")

        # Retry by closing and reopening the window
        for attempt in range(1, max_retries + 1):
            if self._cancelled:
                return
            await self._log(
                "warning",
                f"Challenge ({challenge.value}) detected — "
                f"reopening window (attempt {attempt}/{max_retries})",
            )

            # Get a fresh context from the manager
            self._context = await self._context_recreator(self._id)
            if clear_cookies:
                await self._clear_cookies()
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

            # Re-navigate
            await self.state_machine.transition(WorkerState.NAVIGATING)
            await self._log("info", "Fresh window opened; navigating back to Arena")
            try:
                await self._page.goto(
                    self._config.arena_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception as exc:
                await self.state_machine.force_error(str(exc))
                raise NavigationError(str(exc), self._id)

            if self._zoom_pct != 100:
                await self._page.evaluate(
                    "z => document.body.style.zoom = z + '%'", self._zoom_pct
                )

            await self._dismiss_tos_dialog()
            await self._dismiss_login_dialog()

            challenge = await detect_challenge(self._page)
            if challenge == ChallengeType.NONE:
                await self._event_bus.publish(
                    Event(type=EventType.CHALLENGE_RESOLVED, worker_id=self._id)
                )
                return

            # Still challenged — loop back to WAITING_FOR_CHALLENGE for next retry
            if attempt < max_retries:
                await self.state_machine.transition(WorkerState.WAITING_FOR_CHALLENGE)

        raise ChallengeDetectedError(self._id, f"still challenged after {max_retries} retries")

    # ── Submission ──

    async def submit_prompt(
        self,
        prompt: str,
        model_a: Optional[str] = None,
        model_b: Optional[str] = None,
        retry_on_challenge: int = 1,
    ) -> None:
        """Select models (optional), paste prompt, and submit."""
        assert self._page is not None
        self._started_at = datetime.now(timezone.utc)

        # Optional model selection (side-by-side has two dropdowns)
        if model_a or model_b:
            await self.state_machine.transition(WorkerState.SELECTING_MODEL)
            if model_a:
                await self._select_model(model_a, index=0)
            if model_b:
                await self._select_model(model_b, index=1)

        # Paste prompt
        await self.state_machine.transition(WorkerState.PASTING)
        textarea_sel = self._selectors.get("prompt_textarea")
        prompt_element = await self._human.type_text(
            self._page, textarea_sel, prompt
        )
        await self._log("info", "Prompt pasted")

        # Submit (TOS dialog may appear after clicking, so retry if needed)
        await self.state_machine.transition(WorkerState.SUBMITTING)
        submit_sel = self._selectors.get("submit_button")
        await self._submit_prompt_form(prompt_element, submit_sel)

        # TOS or login dialog may pop up after submit — dismiss and re-submit
        # only if the first submit was consumed by the dialog
        await asyncio.sleep(2)
        tos_dismissed = await self._dismiss_tos_dialog()
        await self._dismiss_login_dialog(wait=True)
        challenge = await detect_challenge(self._page)
        if challenge != ChallengeType.NONE:
            if retry_on_challenge <= 0:
                raise ChallengeDetectedError(self._id, challenge.value)
            if self.state_machine.state != WorkerState.WAITING_FOR_CHALLENGE:
                await self.state_machine.transition(
                    WorkerState.WAITING_FOR_CHALLENGE
                )
            await self._log(
                "warning",
                f"Challenge ({challenge.value}) appeared after submit — "
                "closing this window, opening a new one, and retrying",
            )
            await self._handle_challenge(
                challenge,
                max_retries=1,
                clear_cookies=False,
            )
            if self.state_machine.state != WorkerState.READY:
                await self.state_machine.transition(WorkerState.READY)
            return await self.submit_prompt(
                prompt=prompt,
                model_a=model_a,
                model_b=model_b,
                retry_on_challenge=retry_on_challenge - 1,
            )
        if tos_dismissed:
            # Check if a response is already appearing (first submit went through)
            slide_sel = self._selectors.get("response_slide")
            has_response = await self._page.evaluate(
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
                        .filter(isVisible)
                        .some((slide) => Array.from(slide.querySelectorAll(".prose"))
                            .filter(isVisible)
                            .some((node) => (node.innerText || "").trim().length > 0)
                        );
                }""",
                slide_sel,
            )
            if not has_response:
                await self._log("info", "Re-submitting after TOS dialog")
                refreshed_prompt = await self._page.wait_for_selector(
                    textarea_sel,
                    state="visible",
                    timeout=5_000,
                )
                await self._submit_prompt_form(refreshed_prompt, submit_sel)
            else:
                await self._log("info", "TOS dismissed — submission already went through")

        await self._log("info", "Submitted")

        # Transition to polling
        await self.state_machine.transition(WorkerState.POLLING)

    async def _submit_prompt_form(
        self,
        prompt_element: ElementHandle,
        submit_sel: str,
    ) -> None:
        """Submit the composer tied to the active prompt element.

        Arena can keep other submit buttons mounted in dialogs, so clicking the
        first ``button[type='submit']`` in the DOM is unreliable. Prefer the
        submit button in the same form as the prompt, ignore dialog buttons,
        and fall back to Enter if the button is still not clickable.
        """
        assert self._page is not None

        await asyncio.sleep(0.3)
        submit_handle = await prompt_element.evaluate_handle(
            """(promptEl, selector) => {
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

                const isEnabled = (el) => {
                    if (!el) return false;
                    if (el.disabled) return false;
                    if (el.getAttribute("aria-disabled") === "true") return false;
                    return window.getComputedStyle(el).pointerEvents !== "none";
                };

                const findButton = (root) => {
                    const buttons = Array.from(root.querySelectorAll(selector))
                        .filter((btn) => !btn.closest('[role="dialog"]'))
                        .filter(isVisible);
                    const enabled = [...buttons].reverse().find(isEnabled);
                    return {
                        button: enabled ?? [...buttons].reverse()[0] ?? null,
                        enabled: Boolean(enabled),
                    };
                };

                const form = promptEl.closest("form");
                if (form) {
                    const formMatch = findButton(form);
                    if (formMatch.button) return formMatch.button;
                }

                const pageMatch = findButton(document);
                if (pageMatch.button) return pageMatch.button;
                return null;
            }""",
            submit_sel,
        )
        submit_button = submit_handle.as_element()
        if submit_button is not None:
            try:
                await self._human.click_element(self._page, submit_button)
                return
            except Exception as exc:
                await self._log(
                    "debug",
                    f"Direct submit button click failed: {exc}",
                )

        await self._log(
            "warning",
            "Submit button was not clickable; retrying with Enter on the prompt editor",
        )
        await prompt_element.click()
        await asyncio.sleep(0.1)
        await self._page.keyboard.press("Enter")

        if submit_button is None:
            await self._log(
                "warning",
                "No non-dialog submit button was found; Enter fallback used",
            )

    async def _find_model_dropdown_button(
        self, index: int
    ) -> Optional[ElementHandle]:
        """Return the visible model dropdown button for slot *index*."""
        assert self._page is not None
        dropdown_sel = self._selectors.get("model_dropdown")
        handle = await self._page.evaluate_handle(
            """({ selector, index }) => {
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

                const buttonMatches = (btn) => {
                    const text = (btn.textContent || "").trim();
                    return text && !text.includes("Direct") && !text.includes("Side");
                };

                const scoped = Array.from(document.querySelectorAll(selector))
                    .filter(isVisible)
                    .filter(buttonMatches);
                if (index < scoped.length) return scoped[index];

                const fallback = Array.from(
                    document.querySelectorAll('button[aria-haspopup="dialog"]')
                )
                    .filter(isVisible)
                    .filter(buttonMatches);
                return fallback[index] || null;
            }""",
            {
                "selector": dropdown_sel,
                "index": index,
            },
        )
        return handle.as_element()

    async def _wait_for_model_search_input(
        self, timeout_seconds: float = 8.0
    ) -> Optional[ElementHandle]:
        """Return the visible search input inside the active model dialog."""
        assert self._page is not None
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        search_sel = self._selectors.get("model_search_input")

        while asyncio.get_running_loop().time() < deadline:
            handle = await self._page.evaluate_handle(
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

                    const dialogs = Array.from(
                        document.querySelectorAll('[role="dialog"]')
                    ).filter(isVisible);
                    for (let i = dialogs.length - 1; i >= 0; i -= 1) {
                        const input = dialogs[i].querySelector(selector);
                        if (input && isVisible(input)) return input;
                    }

                    const inputs = Array.from(
                        document.querySelectorAll(selector)
                    ).filter(isVisible);
                    return inputs.length ? inputs[inputs.length - 1] : null;
                }""",
                search_sel,
            )
            input = handle.as_element()
            if input is not None:
                return input
            await asyncio.sleep(0.25)

        return None

    async def _select_model(self, model_name: str, index: int = 0) -> None:
        """Click the *index*-th model dropdown (0 = Model A, 1 = Model B),
        search for *model_name*, and select it.
        """
        label = "A" if index == 0 else "B"
        try:
            option_sel = self._selectors.get("model_option")
            for attempt in range(1, 4):
                dropdown = await self._find_model_dropdown_button(index)
                if dropdown is None:
                    await self._log(
                        "warning",
                        f"Could not find model {label} dropdown button",
                    )
                    return

                await self._human.click_element(self._page, dropdown)
                search_input = await self._wait_for_model_search_input()
                if search_input is None:
                    await self._log(
                        "debug",
                        f"Model {label} picker search input was not ready on "
                        f"attempt {attempt}; retrying",
                    )
                    await self._page.keyboard.press("Escape")
                    await asyncio.sleep(0.4)
                    continue

                await search_input.click()
                await asyncio.sleep(0.2)
                await self._page.keyboard.press("Control+A")
                await asyncio.sleep(0.1)
                await self._page.keyboard.press("Backspace")
                await asyncio.sleep(0.1)
                await self._page.keyboard.type(model_name, delay=50)
                await asyncio.sleep(1.0)

                option_handle = await self._page.evaluate_handle(
                    """({ selector, requestedName }) => {
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

                        const options = Array.from(document.querySelectorAll(selector))
                            .filter(isVisible);
                        const normalizedRequest = requestedName.toLowerCase();
                        const exact = options.find((opt) => {
                            const dataValue = (
                                opt.getAttribute("data-value") || ""
                            ).toLowerCase();
                            const text = (
                                opt.textContent || ""
                            ).trim().toLowerCase();
                            return (
                                dataValue.includes(normalizedRequest) ||
                                text.includes(normalizedRequest)
                            );
                        });
                        return exact || options[0] || null;
                    }""",
                    {
                        "selector": option_sel,
                        "requestedName": model_name,
                    },
                )
                option = option_handle.as_element()
                if option:
                    await option.click()
                    await asyncio.sleep(0.5)
                    await self._log(
                        "info", f"Selected model {label}: '{model_name}'"
                    )
                    return

                await self._log(
                    "debug",
                    f"Model {label} option for '{model_name}' was not ready on "
                    f"attempt {attempt}; retrying",
                )
                await self._page.keyboard.press("Escape")
                await asyncio.sleep(0.4)

            await self._log(
                "warning",
                f"No model {label} option found for '{model_name}'",
            )
        except Exception as exc:
            await self._log(
                "warning",
                f"Model selection failed: {exc}. Using Arena default.",
            )
            # Always close the dialog to avoid stealing focus from prompt input
            try:
                await self._page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass

    # ── Clipboard extraction ──

    async def _extract_via_clipboard(
        self, button_index: int, label: str
    ) -> Optional[str]:
        """Click the *button_index*-th copy button and read clipboard.

        Returns the clipboard text, or ``None`` if extraction fails
        (caller should fall back to DOM text).
        """
        assert self._page is not None
        try:
            slide_sel = self._selectors.get("response_slide")
            slide_handle = await self._page.evaluate_handle(
                """({ selector, index }) => {
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

                    const slides = Array.from(document.querySelectorAll(selector))
                        .filter(isVisible);
                    return slides[index] || null;
                }""",
                {
                    "selector": slide_sel,
                    "index": button_index,
                },
            )
            slide = slide_handle.as_element()
            if slide is None:
                await self._log(
                    "debug",
                    f"Response slide {label} not found for clipboard extraction",
                )
                return None

            copy_handle = await slide.evaluate_handle(
                """(slideEl) => {
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

                    return (
                        Array.from(slideEl.querySelectorAll("button[type='button']"))
                            .find((btn) => isVisible(btn) && hasCopyIcon(btn)) || null
                    );
                }"""
            )
            copy_button = copy_handle.as_element()
            if copy_button is None:
                await self._log("debug", f"Copy button {label} not found in slide")
                return None

            before_text = await self._page.evaluate(
                "() => navigator.clipboard.readText().catch(() => '')"
            )
            await self._human.click_element(self._page, copy_button)

            clipboard_text = ""
            for _ in range(10):
                await asyncio.sleep(0.2)
                clipboard_text = await self._page.evaluate(
                    "() => navigator.clipboard.readText().catch(() => '')"
                )
                if clipboard_text and clipboard_text.strip():
                    if clipboard_text != before_text or not before_text.strip():
                        break

            if clipboard_text and clipboard_text.strip():
                await self._log(
                    "info",
                    f"Copied {label} response via clipboard ({len(clipboard_text)} chars)",
                )
                return clipboard_text.strip()

            await self._log("debug", f"Clipboard was empty for {label}")
            return None
        except Exception as exc:
            await self._log("debug", f"Clipboard extraction failed for {label}: {exc}")
            return None

    # ── Polling ──

    async def poll_for_completion(self) -> WindowResult:
        """Poll DOM until both responses are stable. Returns result."""
        assert self._page is not None

        try:
            (resp_a, resp_b), (name_a, name_b) = await self._poller.poll(
                page=self._page,
                selectors=self._selectors,
                worker_id=self._id,
                cancel_event=self._cancel_event,
            )

            # Click copy buttons and read clipboard (falls back to DOM text)
            clipboard_a = await self._extract_via_clipboard(0, "Model A")
            clipboard_b = await self._extract_via_clipboard(1, "Model B")
            final_resp_a = clipboard_a or resp_a
            final_resp_b = clipboard_b or resp_b

            completed_at = datetime.now(timezone.utc)
            elapsed = (
                (completed_at - self._started_at).total_seconds()
                if self._started_at
                else None
            )

            self._result = WindowResult(
                worker_id=self._id,
                prompt="",  # set by orchestrator
                model_a_name=name_a,
                model_a_response=final_resp_a,
                model_b_name=name_b,
                model_b_response=final_resp_b,
                started_at=self._started_at,
                completed_at=completed_at,
                elapsed_seconds=elapsed,
                success=True,
            )

            await self.state_machine.transition(WorkerState.COMPLETE)
            await self._event_bus.publish(
                Event(
                    type=EventType.WORKER_COMPLETE,
                    worker_id=self._id,
                    data={"result": self._result.model_dump(mode="json")},
                )
            )
            return self._result

        except Exception as exc:
            self._result = WindowResult(
                worker_id=self._id,
                prompt="",
                success=False,
                error=str(exc),
                started_at=self._started_at,
                completed_at=datetime.now(timezone.utc),
            )
            await self.state_machine.force_error(str(exc))
            await self._event_bus.publish(
                Event(
                    type=EventType.WORKER_ERROR,
                    worker_id=self._id,
                    data={"error": str(exc)},
                )
            )
            return self._result

    # ── Lifecycle ──

    def get_result(self) -> Optional[WindowResult]:
        return self._result

    async def cancel(self) -> None:
        self._cancelled = True
        self._cancel_event.set()
        if not self.state_machine.is_terminal:
            try:
                await self.state_machine.transition(WorkerState.CANCELLED)
            except Exception:
                pass  # best-effort — flag and event are already set
