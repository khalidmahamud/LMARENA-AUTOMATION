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
    LoginDialogError,
    NavigationError,
    RateLimitError,
    SubmissionError,
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
    # Callback type: (worker_index) -> proxy server string or None
    ProxyGetter = Callable[[int], Optional[str]]

    def __init__(
        self,
        worker_id: int,
        context: BrowserContext,
        config: AppConfig,
        event_bus: EventBus,
        context_recreator: Optional[ContextRecreator] = None,
        proxy_getter: Optional[ProxyGetter] = None,
    ) -> None:
        self._id = worker_id
        self._context = context
        self._config = config
        self._event_bus = event_bus
        self._context_recreator = context_recreator
        self._proxy_getter = proxy_getter
        self._page: Optional[Page] = None
        self._result: Optional[WindowResult] = None
        self._started_at: Optional[datetime] = None
        self._cancelled = False
        self._cancel_event = asyncio.Event()
        self._last_prompt: Optional[str] = None
        self._last_model_a: Optional[str] = None
        self._last_model_b: Optional[str] = None

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
        proxy = self._proxy_getter(self._id) if self._proxy_getter else None
        await self._event_bus.publish(
            Event(
                type=EventType.WORKER_STATE_CHANGED,
                worker_id=wid,
                data={
                    "old_state": old.value,
                    "new_state": new.value,
                    "progress": self.state_machine.progress,
                    "proxy": proxy,
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

    @staticmethod
    def _normalize_text(value: Optional[str]) -> str:
        return " ".join((value or "").replace("\u200b", "").split()).strip()

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

    async def _ensure_active(
        self,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        await self._wait_if_paused(self._cancel_event, pause_event)
        if self._cancelled or self._cancel_event.is_set():
            raise asyncio.CancelledError("Run cancelled")

    async def _sleep_with_controls(
        self,
        duration: float,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + duration
        while True:
            await self._ensure_active(pause_event)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.25))

    async def _install_arena_page_guards(self) -> None:
        assert self._page is not None
        try:
            await self._page.evaluate(
                """() => {
                    const domains = [
                        "x.com/arena",
                        "linkedin.com/company/arenaai",
                        "youtube.com/@arenaaiofficial",
                    ];
                    const texts = [
                        "follow us for the latest in ai news and advancements",
                        "for the latest in ai news",
                    ];

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

                    const findFixedAncestor = (el) => {
                        let node = el;
                        while (node && node !== document.body) {
                            const style = window.getComputedStyle(node);
                            if (
                                (style.position === "fixed" ||
                                    style.position === "sticky") &&
                                isVisible(node)
                            ) {
                                return node;
                            }
                            node = node.parentElement;
                        }
                        return null;
                    };

                    const hideElement = (el) => {
                        if (!el || el.dataset.lmArenaHidden === "true") return;
                        el.dataset.lmArenaHidden = "true";
                        el.style.setProperty("display", "none", "important");
                    };

                    const ensureToastRoot = () => {
                        let root = document.getElementById(
                            "lm-arena-toast-root"
                        );
                        if (root) return root;

                        root = document.createElement("div");
                        root.id = "lm-arena-toast-root";
                        Object.assign(root.style, {
                            position: "fixed",
                            right: "20px",
                            bottom: "20px",
                            zIndex: "2147483647",
                            display: "flex",
                            flexDirection: "column",
                            gap: "10px",
                            alignItems: "flex-end",
                            pointerEvents: "none",
                        });
                        document.body.appendChild(root);
                        return root;
                    };

                    const showToast = (message, level = "success") => {
                        if (!message) return;
                        const root = ensureToastRoot();
                        const toast = document.createElement("div");
                        const colors = {
                            success: {
                                bg: "rgba(22, 101, 52, 0.96)",
                                border: "rgba(134, 239, 172, 0.85)",
                            },
                            info: {
                                bg: "rgba(30, 64, 175, 0.96)",
                                border: "rgba(147, 197, 253, 0.85)",
                            },
                            warning: {
                                bg: "rgba(133, 77, 14, 0.96)",
                                border: "rgba(253, 224, 71, 0.85)",
                            },
                            error: {
                                bg: "rgba(153, 27, 27, 0.96)",
                                border: "rgba(252, 165, 165, 0.85)",
                            },
                        };
                        const palette = colors[level] || colors.success;

                        toast.textContent = message;
                        Object.assign(toast.style, {
                            maxWidth: "320px",
                            padding: "12px 16px",
                            borderRadius: "12px",
                            border: `1px solid ${palette.border}`,
                            background: palette.bg,
                            color: "#ffffff",
                            fontFamily:
                                "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
                            fontSize: "14px",
                            lineHeight: "1.4",
                            boxShadow: "0 14px 30px rgba(0, 0, 0, 0.28)",
                            opacity: "0",
                            transform: "translateY(12px)",
                            transition:
                                "opacity 180ms ease, transform 180ms ease",
                        });

                        root.appendChild(toast);
                        requestAnimationFrame(() => {
                            toast.style.opacity = "1";
                            toast.style.transform = "translateY(0)";
                        });

                        window.setTimeout(() => {
                            toast.style.opacity = "0";
                            toast.style.transform = "translateY(12px)";
                            window.setTimeout(() => toast.remove(), 220);
                        }, 2600);
                    };

                    const dismissVotingOnboarding = () => {
                        const dialogs = Array.from(
                            document.querySelectorAll('[role="dialog"]')
                        );
                        for (const dialog of dialogs) {
                            if (!isVisible(dialog)) continue;
                            const text = (dialog.innerText || "").toLowerCase();
                            const isVotingDialog =
                                text.includes("how voting works") &&
                                (text.includes("it's a tie") ||
                                    text.includes("both are bad"));
                            if (!isVotingDialog) continue;

                            const gotItButton = Array.from(
                                dialog.querySelectorAll("button")
                            ).find((btn) => {
                                return (
                                    isVisible(btn) &&
                                    (btn.innerText || "").trim().toLowerCase() ===
                                        "got it"
                                );
                            });

                            if (gotItButton) {
                                gotItButton.click();
                                return true;
                            }
                        }
                        return false;
                    };

                    const hidePromos = () => {
                        const anchors = Array.from(
                            document.querySelectorAll("a[href]")
                        );
                        for (const anchor of anchors) {
                            const href = (
                                anchor.getAttribute("href") || ""
                            ).toLowerCase();
                            const text = (anchor.innerText || "").toLowerCase();
                            const isArenaPromo =
                                domains.some((domain) => href.includes(domain)) ||
                                texts.some((snippet) => text.includes(snippet));
                            if (!isArenaPromo) continue;
                            hideElement(
                                findFixedAncestor(anchor) ||
                                    anchor.closest("div") ||
                                    anchor
                            );
                        }
                    };

                    window.__lmArenaHidePromos = hidePromos;
                    window.__lmArenaShowToast = showToast;
                    window.__lmArenaDismissVotingOnboarding =
                        dismissVotingOnboarding;
                    if (!window.__lmArenaPageGuardsInstalled) {
                        window.__lmArenaPageGuardsInstalled = true;
                        const observer = new MutationObserver(() => {
                            hidePromos();
                            dismissVotingOnboarding();
                        });
                        observer.observe(document.documentElement, {
                            childList: true,
                            subtree: true,
                        });
                        window.setInterval(() => {
                            dismissVotingOnboarding();
                        }, 500);
                    }

                    hidePromos();
                    dismissVotingOnboarding();
                }"""
            )
        except Exception as exc:
            await self._log("debug", f"Page guard installation skipped: {exc}")

    async def _show_in_browser_toast(
        self,
        message: str,
        level: str = "success",
    ) -> None:
        assert self._page is not None
        try:
            await self._page.evaluate(
                """({ message, level }) => {
                    if (window.__lmArenaShowToast) {
                        window.__lmArenaShowToast(message, level);
                    }
                }""",
                {"message": message, "level": level},
            )
        except Exception as exc:
            await self._log("debug", f"In-browser toast skipped: {exc}")

    async def _stabilize_loaded_page(
        self,
        pause_event: Optional[asyncio.Event] = None,
        dialog_wait_seconds: float = 5.0,
    ) -> None:
        assert self._page is not None
        await self._ensure_active(pause_event)

        if self._zoom_pct != 100:
            await self._page.evaluate(
                "z => document.body.style.zoom = z + '%'", self._zoom_pct
            )

        await self._install_arena_page_guards()
        await self._dismiss_known_dialogs(
            wait_seconds=max(dialog_wait_seconds, 10.0),
            pause_event=pause_event,
        )
        await self._ensure_active(pause_event)

    # ── Navigation ──

    async def navigate_to_arena(
        self,
        clear_cookies: bool = False,
        zoom_pct: int = 100,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Navigate to the Arena side-by-side page, handling challenges."""
        self._zoom_pct = zoom_pct
        await self.state_machine.transition(WorkerState.LAUNCHING)
        await self._ensure_active(pause_event)

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
            await self._ensure_active(pause_event)
            await self._page.goto(
                self._config.arena_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
        except Exception as exc:
            await self.state_machine.force_error(str(exc))
            raise NavigationError(str(exc), self._id)

        await self._stabilize_loaded_page(
            pause_event=pause_event,
            dialog_wait_seconds=5.0,
        )
        if clear_cookies:
            await self._show_in_browser_toast("Cookies cleared successfully")

        # Check for challenges (including login dialog) — retry by reopening window
        await self._ensure_active(pause_event)
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
            await self._handle_challenge(
                challenge,
                pause_event=pause_event,
            )

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

    async def _find_visible_dialog_button(
        self, selector: str
    ) -> Optional[ElementHandle]:
        assert self._page is not None
        locator = self._page.locator(selector)
        count = await locator.count()
        for index in range(count - 1, -1, -1):
            handle = await locator.nth(index).element_handle()
            if handle and await handle.is_visible():
                return handle
        return None

    async def _find_dialog_button_by_text(
        self,
        button_text: str,
        dialog_text_snippets: tuple[str, ...],
        selector_key: Optional[str] = None,
    ) -> Optional[ElementHandle]:
        assert self._page is not None

        if selector_key is not None:
            try:
                selector = self._selectors.get(selector_key)
                button = await self._find_visible_dialog_button(selector)
                if button is not None:
                    return button
            except KeyError:
                pass

        handle = await self._page.evaluate_handle(
            """({ buttonText, dialogTexts }) => {
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

                const normalize = (value) =>
                    (value || "").trim().toLowerCase();

                const requestedButton = normalize(buttonText);
                const requestedDialogs = (dialogTexts || [])
                    .map(normalize)
                    .filter(Boolean);

                const dialogs = Array.from(
                    document.querySelectorAll('[role="dialog"]')
                ).filter(isVisible);

                for (const dialog of dialogs) {
                    const text = normalize(dialog.innerText);
                    if (
                        requestedDialogs.length &&
                        !requestedDialogs.some((snippet) => text.includes(snippet))
                    ) {
                        continue;
                    }

                    const button = Array.from(
                        dialog.querySelectorAll("button")
                    ).find((btn) => {
                        return (
                            isVisible(btn) &&
                            normalize(btn.innerText) === requestedButton
                        );
                    });
                    if (button) {
                        return button;
                    }
                }

                return null;
            }""",
            {
                "buttonText": button_text,
                "dialogTexts": list(dialog_text_snippets),
            },
        )
        return handle.as_element()

    async def _find_cookie_accept_button(self) -> Optional[ElementHandle]:
        return await self._find_dialog_button_by_text(
            button_text="Accept Cookies",
            dialog_text_snippets=(
                "this website uses cookies",
                "accept cookies",
            ),
            selector_key="cookie_accept_button",
        )

    async def _find_voting_onboarding_button(self) -> Optional[ElementHandle]:
        return await self._find_dialog_button_by_text(
            button_text="Got it",
            dialog_text_snippets=(
                "how voting works",
                "it's a tie",
                "both are bad",
            ),
            selector_key="voting_onboarding_got_it_button",
        )

    async def _dismiss_known_dialogs(
        self,
        wait_seconds: float = 0.0,
        poll_interval: float = 0.25,
        pause_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Dismiss known blocking dialogs like cookies, TOS, and onboarding.

        Returns ``True`` if at least one dialog button was clicked.
        """
        assert self._page is not None
        dismissed_any = False
        handlers = [
            ("cookie_accept_button", "Accepted cookie dialog"),
            ("tos_agree_button", "Dismissed Terms of Use dialog"),
            (
                "voting_onboarding_got_it_button",
                "Dismissed voting onboarding dialog",
            ),
        ]

        try:
            while True:
                await self._ensure_active(pause_event)
                dismissed_this_pass = False
                for selector_key, log_text in handlers:
                    if selector_key == "cookie_accept_button":
                        button = await self._find_cookie_accept_button()
                    elif selector_key == "voting_onboarding_got_it_button":
                        button = await self._find_voting_onboarding_button()
                    else:
                        selector = self._selectors.get(selector_key)
                        button = await self._find_visible_dialog_button(selector)
                    if button is None:
                        continue
                    await button.click(force=True)
                    await self._sleep_with_controls(0.5, pause_event)
                    await self._log("info", log_text)
                    dismissed_any = True
                    dismissed_this_pass = True
                    break

                if not dismissed_this_pass:
                    if wait_seconds <= 0:
                        return dismissed_any
                    wait_seconds = max(0.0, wait_seconds - poll_interval)
                    if wait_seconds <= 0:
                        return dismissed_any
                    await self._sleep_with_controls(poll_interval, pause_event)
                    continue
        except Exception as exc:
            await self._log("debug", f"Dialog dismissal skipped: {exc}")
            return dismissed_any

    async def _handle_challenge(
        self,
        challenge: ChallengeType,
        max_retries: int = 3,
        clear_cookies: bool = False,
        pause_event: Optional[asyncio.Event] = None,
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
                await self._ensure_active(pause_event)
                if self._cancelled:
                    return
                if await detect_challenge(self._page) == ChallengeType.NONE:
                    await self._event_bus.publish(
                        Event(type=EventType.CHALLENGE_RESOLVED, worker_id=self._id)
                    )
                    return
                await self._sleep_with_controls(2, pause_event)
            raise ChallengeDetectedError(self._id, "timeout")

        # Retry by closing and reopening the window
        for attempt in range(1, max_retries + 1):
            await self._ensure_active(pause_event)
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
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )

            # Re-navigate
            await self.state_machine.transition(WorkerState.NAVIGATING)
            await self._log("info", "Fresh window opened; navigating back to Arena")
            try:
                await self._ensure_active(pause_event)
                await self._page.goto(
                    self._config.arena_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception as exc:
                await self.state_machine.force_error(str(exc))
                raise NavigationError(str(exc), self._id)

            await self._stabilize_loaded_page(
                pause_event=pause_event,
                dialog_wait_seconds=5.0,
            )
            if clear_cookies:
                await self._show_in_browser_toast("Cookies cleared successfully")

            await self._ensure_active(pause_event)
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

    async def prepare_prompt(
        self,
        prompt: str,
        model_a: Optional[str] = None,
        model_b: Optional[str] = None,
        mark_started: bool = True,
        pause_event: Optional[asyncio.Event] = None,
        images: Optional[list] = None,
    ) -> None:
        """Select models (optional) and paste prompt without submitting."""
        assert self._page is not None
        await self._ensure_active(pause_event)
        await self._install_arena_page_guards()
        self._started_at = (
            datetime.now(timezone.utc) if mark_started else None
        )
        self._last_prompt = prompt
        self._last_model_a = model_a
        self._last_model_b = model_b

        await self._dismiss_known_dialogs(
            wait_seconds=1.0,
            pause_event=pause_event,
        )

        if model_a or model_b:
            await self.state_machine.transition(WorkerState.SELECTING_MODEL)
            if model_a:
                await self._select_model(
                    model_a,
                    index=0,
                    pause_event=pause_event,
                )
            if model_b:
                await self._select_model(
                    model_b,
                    index=1,
                    pause_event=pause_event,
                )

        # Paste prompt
        await self.state_machine.transition(WorkerState.PASTING)
        await self._ensure_active(pause_event)
        textarea_sel = self._selectors.get("prompt_textarea")
        has_images = bool(images)
        prompt_element = await self._human.type_text(
            self._page, textarea_sel, prompt, verify=not has_images,
        )
        await self._log("info", "Prompt pasted")

        # Paste images if provided
        if has_images:
            await self._human.paste_images(
                self._page, prompt_element, images,
            )
            await self._log("info", f"Pasted {len(images)} image(s)")

        await self.state_machine.transition(WorkerState.PREPARED)
        await self._log("info", "Prompt prepared")

    async def submit_prepared_prompt(
        self,
        retry_on_challenge: int = 1,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Submit a prompt that has already been prepared in the composer."""
        assert self._page is not None
        if self._last_prompt is None:
            raise RuntimeError(
                f"Worker {self._id}: no prepared prompt available for submit"
            )
        if self.state_machine.state != WorkerState.PREPARED:
            raise RuntimeError(
                f"Worker {self._id}: cannot submit prepared prompt from "
                f"state={self.state_machine.state.value}"
            )

        await self._ensure_active(pause_event)
        await self._install_arena_page_guards()
        if self._started_at is None:
            self._started_at = datetime.now(timezone.utc)

        await self.state_machine.transition(WorkerState.SUBMITTING)
        textarea_sel = self._selectors.get("prompt_textarea")
        submit_sel = self._selectors.get("submit_button")
        submit_accepted = False
        last_snapshot: Optional[dict] = None

        for attempt in range(1, 4):
            prompt_element = await self._page.wait_for_selector(
                textarea_sel,
                state="visible",
                timeout=5_000,
            )
            await self._submit_prompt_form(
                prompt_element,
                submit_sel,
                pause_event=pause_event,
            )

            last_snapshot = await self._wait_for_submission_acceptance(
                expected_prompt=self._last_prompt or "",
                pause_event=pause_event,
                timeout_seconds=3.0,
            )
            if last_snapshot.get("accepted"):
                submit_accepted = True
                break

            dialog_dismissed = await self._dismiss_known_dialogs(
                wait_seconds=1.5,
                pause_event=pause_event,
            )

            await self._ensure_active(pause_event)
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
                    f"Challenge ({challenge.value}) appeared after submit - "
                    "closing this window, opening a new one, and retrying",
                )
                await self._handle_challenge(
                    challenge,
                    max_retries=1,
                    clear_cookies=False,
                    pause_event=pause_event,
                )
                if self.state_machine.state != WorkerState.READY:
                    await self.state_machine.transition(WorkerState.READY)
                return await self.submit_prompt(
                    prompt=self._last_prompt,
                    model_a=self._last_model_a,
                    model_b=self._last_model_b,
                    retry_on_challenge=retry_on_challenge - 1,
                    pause_event=pause_event,
                )

            last_snapshot = await self._wait_for_submission_acceptance(
                expected_prompt=self._last_prompt or "",
                pause_event=pause_event,
                timeout_seconds=2.0,
            )
            if last_snapshot.get("accepted"):
                submit_accepted = True
                if dialog_dismissed:
                    await self._log(
                        "info",
                        "Dialog dismissed - submission accepted",
                    )
                break

            if attempt < 3:
                reason = (
                    "dialog blocked the first submit"
                    if dialog_dismissed
                    else "composer still contained the prompt"
                )
                await self._log(
                    "warning",
                    f"Submit attempt {attempt} was not accepted ({reason}); retrying",
                )

        if not submit_accepted:
            details = ""
            if last_snapshot:
                details = (
                    f" prompt_matches={last_snapshot.get('prompt_matches_expected')}"
                    f", textarea_visible={last_snapshot.get('textarea_visible')}"
                    f", submit_enabled={last_snapshot.get('submit_enabled')}"
                    f", stop_visible={last_snapshot.get('stop_visible')}"
                )
            raise SubmissionError(
                f"Prompt submission was not accepted after 3 attempts.{details}",
                self._id,
            )

        await self._log("info", "Submitted")

        # Transition to polling
        await self.state_machine.transition(WorkerState.POLLING)
        return

        # Cookie, TOS, onboarding, or login dialogs may pop up after submit.
        # Dismiss the passive ones here and let detect_challenge() catch login
        # barriers.
        await self._sleep_with_controls(3, pause_event)
        dialog_dismissed = await self._dismiss_known_dialogs(
            wait_seconds=2.0,
            pause_event=pause_event,
        )
        await self._ensure_active(pause_event)
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
                pause_event=pause_event,
            )
            if self.state_machine.state != WorkerState.READY:
                await self.state_machine.transition(WorkerState.READY)
            return await self.submit_prompt(
                prompt=self._last_prompt,
                model_a=self._last_model_a,
                model_b=self._last_model_b,
                retry_on_challenge=retry_on_challenge - 1,
                pause_event=pause_event,
            )
        if dialog_dismissed:
            # Check if a response is already appearing (first submit went through)
            slide_sel = self._selectors.get("response_slide")
            await self._ensure_active(pause_event)
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
                await self._log("info", "Re-submitting after dismissing dialog")
                await self._ensure_active(pause_event)
                refreshed_prompt = await self._page.wait_for_selector(
                    textarea_sel,
                    state="visible",
                    timeout=5_000,
                )
                await self._submit_prompt_form(
                    refreshed_prompt,
                    submit_sel,
                    pause_event=pause_event,
                )
            else:
                await self._log(
                    "info",
                    "Dialog dismissed — submission already went through",
                )

        await self._log("info", "Submitted")

        # Transition to polling
        await self.state_machine.transition(WorkerState.POLLING)

    async def submit_prompt(
        self,
        prompt: str,
        model_a: Optional[str] = None,
        model_b: Optional[str] = None,
        retry_on_challenge: int = 1,
        pause_event: Optional[asyncio.Event] = None,
        images: Optional[list] = None,
    ) -> None:
        """Select models (optional), paste prompt, and submit."""
        await self.prepare_prompt(
            prompt=prompt,
            model_a=model_a,
            model_b=model_b,
            mark_started=True,
            pause_event=pause_event,
            images=images,
        )
        await self.submit_prepared_prompt(
            retry_on_challenge=retry_on_challenge,
            pause_event=pause_event,
        )

    async def _submit_prompt_form(
        self,
        prompt_element: ElementHandle,
        submit_sel: str,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Submit the composer tied to the active prompt element.

        Arena can keep other submit buttons mounted in dialogs, so clicking the
        first ``button[type='submit']`` in the DOM is unreliable. Prefer the
        submit button in the same form as the prompt, ignore dialog buttons,
        and fall back to Enter if the button is still not clickable.
        """
        assert self._page is not None

        await self._sleep_with_controls(0.3, pause_event)
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
                await self._ensure_active(pause_event)
                await self._human.click_element(self._page, submit_button)
                return
            except Exception as exc:
                await self._log(
                    "debug",
                    f"Direct submit button click failed: {exc}",
                )
            try:
                await self._ensure_active(pause_event)
                await submit_button.click(force=True, timeout=2_000)
                return
            except Exception as exc:
                await self._log(
                    "debug",
                    f"Forced submit button click failed: {exc}",
                )

        await self._log(
            "warning",
            "Submit button was not clickable; retrying with Enter on the prompt editor",
        )
        await self._ensure_active(pause_event)
        await prompt_element.click()
        await self._sleep_with_controls(0.1, pause_event)
        await self._ensure_active(pause_event)
        await self._page.keyboard.press("Enter")

        if submit_button is None:
            await self._log(
                "warning",
                "No non-dialog submit button was found; Enter fallback used",
            )

    async def _get_submission_snapshot(
        self,
        expected_prompt: str,
    ) -> dict:
        assert self._page is not None

        textarea_sel = self._selectors.get("prompt_textarea")
        submit_sel = self._selectors.get("submit_button")
        stop_sel = self._selectors.get("stop_generation_button")
        slide_sel = self._selectors.get("response_slide")

        snapshot = await self._page.evaluate(
            """({ textareaSelector, submitSelector, stopSelector, slideSelector, expectedPrompt }) => {
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

                const normalize = (value) =>
                    (value || "").replace(/\\u200b/g, "").replace(/\\s+/g, " ").trim();

                const readPromptValue = (el) => {
                    if (!el) return "";
                    const tag = (el.tagName || "").toLowerCase();
                    if (tag === "textarea" || tag === "input") {
                        return el.value || "";
                    }
                    return el.innerText || el.textContent || "";
                };

                const textarea = Array.from(document.querySelectorAll(textareaSelector))
                    .filter(isVisible)
                    .pop() || null;
                const currentPrompt = normalize(readPromptValue(textarea));
                const expected = normalize(expectedPrompt);

                const submitButtons = Array.from(document.querySelectorAll(submitSelector))
                    .filter((btn) => !btn.closest('[role="dialog"]'))
                    .filter(isVisible);
                const submitButton = submitButtons.length
                    ? submitButtons[submitButtons.length - 1]
                    : null;
                const stopVisible = Array.from(document.querySelectorAll(stopSelector))
                    .some(isVisible);
                const visibleSlides = Array.from(document.querySelectorAll(slideSelector))
                    .filter(isVisible);

                const promptMatchesExpected = Boolean(expected) && currentPrompt === expected;
                const promptCleared = !currentPrompt;
                const accepted =
                    stopVisible ||
                    promptCleared ||
                    (Boolean(textarea) && !promptMatchesExpected);

                return {
                    accepted,
                    current_prompt: currentPrompt,
                    expected_prompt: expected,
                    prompt_matches_expected: promptMatchesExpected,
                    textarea_visible: Boolean(textarea),
                    prompt_cleared: promptCleared,
                    submit_visible: Boolean(submitButton),
                    submit_enabled: submitButton ? isEnabled(submitButton) : false,
                    stop_visible: stopVisible,
                    response_slide_count: visibleSlides.length,
                };
            }""",
            {
                "textareaSelector": textarea_sel,
                "submitSelector": submit_sel,
                "stopSelector": stop_sel,
                "slideSelector": slide_sel,
                "expectedPrompt": expected_prompt,
            },
        )
        return snapshot if isinstance(snapshot, dict) else {"accepted": False}

    async def _wait_for_submission_acceptance(
        self,
        expected_prompt: str,
        pause_event: Optional[asyncio.Event] = None,
        timeout_seconds: float = 3.0,
        poll_interval: float = 0.25,
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        latest: dict = {"accepted": False}
        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_active(pause_event)
            latest = await self._get_submission_snapshot(expected_prompt)
            if latest.get("accepted"):
                return latest
            await self._sleep_with_controls(poll_interval, pause_event)
        return latest

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
                    const text = (btn.innerText || btn.textContent || "")
                        .trim()
                        .toLowerCase();
                    if (!text) return false;
                    if (btn.closest('[role="dialog"]')) return false;
                    return ![
                        "direct",
                        "side",
                        "text",
                        "code",
                        "image",
                        "search",
                    ].some((value) => text === value);
                };

                const buttonScore = (btn) => {
                    let score = 0;
                    if (btn.querySelector("span.font-mono")) score += 5;
                    if (btn.querySelector("span.truncate")) score += 2;
                    const text = (btn.innerText || "").toLowerCase();
                    if (
                        text.includes("gpt") ||
                        text.includes("claude") ||
                        text.includes("gemini") ||
                        text.includes("llama") ||
                        text.includes("-")
                    ) {
                        score += 2;
                    }
                    return score;
                };

                const scoped = Array.from(
                    new Set([
                        ...document.querySelectorAll(selector),
                        ...document.querySelectorAll('button[aria-haspopup="dialog"]'),
                    ])
                )
                    .filter(isVisible)
                    .filter(buttonMatches);
                const ranked = scoped.sort(
                    (a, b) => buttonScore(b) - buttonScore(a)
                );
                return ranked[index] || null;
            }""",
            {
                "selector": dropdown_sel,
                "index": index,
            },
        )
        return handle.as_element()

    async def _wait_for_model_search_input(
        self,
        timeout_seconds: float = 8.0,
        pause_event: Optional[asyncio.Event] = None,
    ) -> Optional[ElementHandle]:
        """Return the visible search input inside the active model picker."""
        assert self._page is not None
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        search_sel = self._selectors.get("model_search_input")
        option_sel = self._selectors.get("model_option")

        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_active(pause_event)
            handle = await self._page.evaluate_handle(
                """({ selector, optionSelector }) => {
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

                    const visibleOptions = (root) =>
                        Array.from(root.querySelectorAll(optionSelector))
                            .filter(isVisible);

                    const inputScore = (input) => {
                        let score = 0;
                        let node = input;
                        while (node) {
                            if (
                                node.matches &&
                                node.matches('[role="dialog"]') &&
                                isVisible(node)
                            ) {
                                score += 3;
                            }
                            if (
                                node.querySelector &&
                                node.querySelector('[data-arena-buttons="true"]')
                            ) {
                                score += 4;
                            }
                            if (node.querySelector) {
                                const options = visibleOptions(node);
                                if (options.length) {
                                    score += Math.min(options.length, 5);
                                    break;
                                }
                            }
                            node = node.parentElement;
                        }

                        const rect = input.getBoundingClientRect();
                        if (rect.top > window.innerHeight * 0.25) {
                            score += 1;
                        }
                        return score;
                    };

                    const inputs = Array.from(
                        document.querySelectorAll(selector)
                    ).filter(isVisible);
                    inputs.sort((a, b) => inputScore(a) - inputScore(b));
                    return inputs.length ? inputs[inputs.length - 1] : null;
                }""",
                {
                    "selector": search_sel,
                    "optionSelector": option_sel,
                },
            )
            input = handle.as_element()
            if input is not None:
                return input
            await self._sleep_with_controls(0.25, pause_event)

        return None

    async def _select_model(
        self,
        model_name: str,
        index: int = 0,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Click the *index*-th model dropdown (0 = Model A, 1 = Model B),
        search for *model_name*, and select it.
        """
        label = "A" if index == 0 else "B"
        try:
            option_sel = self._selectors.get("model_option")
            for attempt in range(1, 4):
                await self._ensure_active(pause_event)
                dropdown = await self._find_model_dropdown_button(index)
                if dropdown is None:
                    await self._log(
                        "warning",
                        f"Could not find model {label} dropdown button",
                    )
                    return

                await self._ensure_active(pause_event)
                await self._human.click_element(self._page, dropdown)
                search_input = await self._wait_for_model_search_input(
                    pause_event=pause_event
                )
                if search_input is None:
                    await self._log(
                        "debug",
                        f"Model {label} picker search input was not ready on "
                        f"attempt {attempt}; retrying",
                    )
                    await self._ensure_active(pause_event)
                    await self._page.keyboard.press("Escape")
                    await self._sleep_with_controls(0.4, pause_event)
                    continue

                await self._ensure_active(pause_event)
                await search_input.click()
                await self._sleep_with_controls(0.2, pause_event)
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Control+A")
                await self._sleep_with_controls(0.1, pause_event)
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Backspace")
                await self._sleep_with_controls(0.1, pause_event)
                await self._ensure_active(pause_event)
                await self._page.keyboard.type(model_name, delay=50)
                await self._sleep_with_controls(1.0, pause_event)

                option_handle = await search_input.evaluate_handle(
                    """(input, { selector, requestedName }) => {
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
                            if (el.getAttribute("aria-disabled") === "true") {
                                return false;
                            }
                            return window.getComputedStyle(el).pointerEvents !== "none";
                        };

                        const normalize = (value) =>
                            (value || "").trim().toLowerCase();

                        const collectOptions = (root) =>
                            Array.from(root.querySelectorAll(selector))
                                .filter(isVisible)
                                .filter(isEnabled)
                                .filter((opt) => {
                                    const dataValue = normalize(
                                        opt.getAttribute("data-value")
                                    );
                                    const text = normalize(opt.textContent);
                                    return Boolean(dataValue || text);
                                });

                        let node = input;
                        let options = [];
                        while (node) {
                            if (node.querySelectorAll) {
                                options = collectOptions(node);
                                if (options.length) break;
                            }
                            node = node.parentElement;
                        }

                        if (!options.length) {
                            options = collectOptions(document);
                        }

                        const normalizedRequest = normalize(requestedName);
                        const exact = options.find((opt) => {
                            const dataValue = normalize(
                                opt.getAttribute("data-value")
                            );
                            const text = normalize(opt.textContent);
                            return dataValue === normalizedRequest ||
                                text === normalizedRequest;
                        });
                        const partial = options.find((opt) => {
                            const dataValue = normalize(
                                opt.getAttribute("data-value")
                            );
                            const text = normalize(opt.textContent);
                            return (
                                dataValue.includes(normalizedRequest) ||
                                text.includes(normalizedRequest)
                            );
                        });
                        return exact || partial || options[0] || null;
                    }""",
                    {
                        "selector": option_sel,
                        "requestedName": model_name,
                    },
                )
                option = option_handle.as_element()
                if option:
                    await self._ensure_active(pause_event)
                    await option.click()
                    await self._sleep_with_controls(0.5, pause_event)
                    await self._log(
                        "info", f"Selected model {label}: '{model_name}'"
                    )
                    return

                await self._log(
                    "debug",
                    f"Model {label} option for '{model_name}' was not ready on "
                    f"attempt {attempt}; retrying",
                )
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Escape")
                await self._sleep_with_controls(0.4, pause_event)

            await self._log(
                "warning",
                f"No model {label} option found for '{model_name}'",
            )
            try:
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Escape")
                await self._sleep_with_controls(0.3, pause_event)
            except Exception:
                pass
        except Exception as exc:
            await self._log(
                "warning",
                f"Model selection failed: {exc}. Using Arena default.",
            )
            # Always close the dialog to avoid stealing focus from prompt input
            try:
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Escape")
                await self._sleep_with_controls(0.3, pause_event)
            except Exception:
                pass

    # ── Polling ──

    async def poll_for_completion(
        self,
        baseline_responses: Optional[tuple[str, str]] = None,
        pause_event: Optional[asyncio.Event] = None,
        _rate_limit_retries: int = 2,
    ) -> WindowResult:
        """Poll DOM until both responses are stable. Returns result."""
        assert self._page is not None

        try:
            (resp_a, resp_b), (name_a, name_b), (html_a, html_b) = await self._poller.poll(
                page=self._page,
                selectors=self._selectors,
                worker_id=self._id,
                cancel_event=self._cancel_event,
                pause_event=pause_event,
                baseline_responses=baseline_responses,
            )

            # Stay DOM-only here so Chromium never shows a clipboard
            # permission prompt during result collection.
            final_resp_a = resp_a
            final_resp_b = resp_b

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
                model_a_response_html=html_a or None,
                model_b_response_html=html_b or None,
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

        except RateLimitError:
            if _rate_limit_retries <= 0 or self._context_recreator is None:
                raise  # fall through to general handler below

            await self._log(
                "warning",
                "Rate limit hit — closing window, reopening fresh, and retrying",
            )
            await self._event_bus.publish(
                Event(
                    type=EventType.CHALLENGE_DETECTED,
                    worker_id=self._id,
                    data={"challenge_type": "rate_limit"},
                )
            )

            # Transition to WAITING_FOR_CHALLENGE (allowed from POLLING)
            await self.state_machine.transition(
                WorkerState.WAITING_FOR_CHALLENGE
            )

            # Close and reopen window
            self._context = await self._context_recreator(self._id)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )

            # Re-navigate
            await self.state_machine.transition(WorkerState.NAVIGATING)
            await self._log("info", "Fresh window opened; navigating back to Arena")
            try:
                await self._ensure_active(pause_event)
                await self._page.goto(
                    self._config.arena_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception as nav_exc:
                await self.state_machine.force_error(str(nav_exc))
                raise NavigationError(str(nav_exc), self._id)

            await self._stabilize_loaded_page(
                pause_event=pause_event,
                dialog_wait_seconds=5.0,
            )

            await self._event_bus.publish(
                Event(type=EventType.CHALLENGE_RESOLVED, worker_id=self._id)
            )

            # Re-submit the same prompt
            await self.state_machine.transition(WorkerState.READY)
            await self.submit_prompt(
                prompt=self._last_prompt,
                model_a=self._last_model_a,
                model_b=self._last_model_b,
                pause_event=pause_event,
            )

            # Re-poll with decremented retry count
            return await self.poll_for_completion(
                baseline_responses=baseline_responses,
                pause_event=pause_event,
                _rate_limit_retries=_rate_limit_retries - 1,
            )

        except LoginDialogError:
            if _rate_limit_retries <= 0 or self._context_recreator is None:
                raise

            await self._log(
                "warning",
                "Login dialog detected — closing window, reopening fresh, and retrying",
            )
            await self._event_bus.publish(
                Event(
                    type=EventType.CHALLENGE_DETECTED,
                    worker_id=self._id,
                    data={"challenge_type": "login_wall"},
                )
            )

            await self.state_machine.transition(
                WorkerState.WAITING_FOR_CHALLENGE
            )

            self._context = await self._context_recreator(self._id)
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )

            await self.state_machine.transition(WorkerState.NAVIGATING)
            await self._log("info", "Fresh window opened; navigating back to Arena")
            try:
                await self._ensure_active(pause_event)
                await self._page.goto(
                    self._config.arena_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception as nav_exc:
                await self.state_machine.force_error(str(nav_exc))
                raise NavigationError(str(nav_exc), self._id)

            await self._stabilize_loaded_page(
                pause_event=pause_event,
                dialog_wait_seconds=5.0,
            )

            await self._event_bus.publish(
                Event(type=EventType.CHALLENGE_RESOLVED, worker_id=self._id)
            )

            await self.state_machine.transition(WorkerState.READY)
            await self.submit_prompt(
                prompt=self._last_prompt,
                model_a=self._last_model_a,
                model_b=self._last_model_b,
                pause_event=pause_event,
            )

            return await self.poll_for_completion(
                baseline_responses=baseline_responses,
                pause_event=pause_event,
                _rate_limit_retries=_rate_limit_retries - 1,
            )

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

    async def prepare_for_followup_prompt(self) -> tuple[str, str]:
        """Ready the current page for another prompt in the same chat."""
        baseline = (
            self._result.model_a_response if self._result else "",
            self._result.model_b_response if self._result else "",
        )

        if self.state_machine.state == WorkerState.COMPLETE:
            await self.state_machine.transition(WorkerState.READY)
        elif self.state_machine.state != WorkerState.READY:
            raise RuntimeError(
                f"Worker {self._id} is not ready for a follow-up prompt "
                f"(state={self.state_machine.state.value})"
            )

        self._result = None
        return baseline

    # ── Recovery ──

    async def reset_with_fresh_context(
        self,
        zoom_pct: int = 100,
        clear_cookies: bool = False,
        pause_event: Optional[asyncio.Event] = None,
    ) -> None:
        """Reset this worker with a completely fresh browser context.

        Sequence: reset state machine → recreate context → navigate to Arena.
        After this method completes the worker is in READY state and can
        accept a new ``submit_prompt()`` call.

        Raises if context recreation or navigation fails.
        """
        if self._context_recreator is None:
            raise RuntimeError(
                f"Worker {self._id}: no context_recreator available for reset"
            )

        # 1. Reset state machine back to IDLE
        if self.state_machine.is_terminal:
            await self.state_machine.reset()
        elif self.state_machine.state != WorkerState.IDLE:
            await self.state_machine.force_error("Resetting for retry")
            await self.state_machine.reset()

        # 2. Clear internal state
        self._result = None
        self._cancelled = False
        self._cancel_event.clear()

        # 3. Recreate the browser context (closes old window, opens fresh one)
        await self._ensure_active(pause_event)
        self._context = await self._context_recreator(self._id)

        # 4. Navigate to Arena (handles challenges, TOS, login dialogs)
        await self.navigate_to_arena(
            clear_cookies=clear_cookies,
            zoom_pct=zoom_pct,
            pause_event=pause_event,
        )

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
