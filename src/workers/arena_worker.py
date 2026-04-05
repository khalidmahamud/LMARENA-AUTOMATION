from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Coroutine, Optional

from playwright.async_api import BrowserContext, ElementHandle, Page, Worker

from src.browser.challenges import ChallengeType, detect_challenge
from src.browser.selectors import SelectorRegistry
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import (
    ChallengeDetectedError,
    GenerationFailedBannerError,
    LoginDialogError,
    ModelSelectionError,
    NavigationError,
    PollingTimeoutError,
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

# Reuse the same cap everywhere challenge recovery reopens a fresh
# browser context so repeated security popups do not get a one-shot path.
CHALLENGE_RETRY_LIMIT = 5


class ArenaWorker:
    """Full lifecycle manager for a single Arena browser window.

    Owns one ``BrowserContext``, one ``WorkerStateMachine``, and publishes
    all status changes through the shared ``EventBus``.
    """

    # Callback type: async (worker_index) -> new BrowserContext
    ContextRecreator = Callable[[int], Coroutine[None, None, BrowserContext]]
    # Callback type: (worker_index) -> proxy server string or None
    ProxyGetter = Callable[[int], Optional[str]]
    # Callback type: (worker_index) -> None
    ProxySuccessReporter = Callable[[int], None]
    # Callback type: (worker_index, reason) -> None
    ProxyFailureReporter = Callable[[int, str], None]

    def __init__(
        self,
        worker_id: int,
        context: BrowserContext,
        config: AppConfig,
        event_bus: EventBus,
        context_recreator: Optional[ContextRecreator] = None,
        proxy_getter: Optional[ProxyGetter] = None,
        proxy_success_reporter: Optional[ProxySuccessReporter] = None,
        proxy_failure_reporter: Optional[ProxyFailureReporter] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self._id = worker_id
        self._context = context
        self._config = config
        self._event_bus = event_bus
        self._run_id = run_id
        self._context_recreator = context_recreator
        self._proxy_getter = proxy_getter
        self._proxy_success_reporter = proxy_success_reporter
        self._proxy_failure_reporter = proxy_failure_reporter
        self._page: Optional[Page] = None
        self._result: Optional[WindowResult] = None
        self._started_at: Optional[datetime] = None
        self._cancelled = False
        self._cancel_event = asyncio.Event()
        self._last_prompt: Optional[str] = None
        self._last_model_a: Optional[str] = None
        self._last_model_b: Optional[str] = None
        self._zoom_service_worker: Optional[Worker] = None
        self._zoom_service_worker_checked = False

        self._selectors = SelectorRegistry.instance()
        self._human = HumanSimulator(config.typing)
        self._poller = ResponsePoller(config.timing)

        self.state_machine = WorkerStateMachine(
            worker_id=worker_id,
            on_transition=self._on_state_transition,
        )

    # ── Event publishing ──

    async def _publish(self, event: Event) -> None:
        """Publish an event, automatically stamping the run_id."""
        if event.run_id is None:
            event.run_id = self._run_id
        await self._event_bus.publish(event)

    async def _on_state_transition(
        self, old: WorkerState, new: WorkerState, wid: int
    ) -> None:
        proxy = self._proxy_getter(self._id) if self._proxy_getter else None
        await self._publish(
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
        await self._publish(
            Event(
                type=EventType.LOG,
                worker_id=self._id,
                data={"level": level, "text": text},
            )
        )

    async def _log_current_proxy(self, context: str = "Current proxy") -> None:
        """Emit the currently assigned proxy for this worker."""
        if not self._proxy_getter:
            return
        proxy = self._proxy_getter(self._id)
        if proxy:
            await self._log("info", f"{context}: {proxy}")
        else:
            await self._log("debug", f"{context}: direct/no proxy")

    @staticmethod
    def _is_navigation_timeout(exc: Exception) -> bool:
        text = str(exc).lower()
        return "page.goto" in text and "timeout" in text

    def _report_proxy_navigation_failure(self, reason: str) -> None:
        if not self._proxy_failure_reporter:
            return
        try:
            self._proxy_failure_reporter(self._id, reason)
        except Exception:
            logger.debug(
                "Worker %d proxy failure reporter failed",
                self._id,
                exc_info=True,
            )

    async def _goto_arena_with_retries(
        self,
        pause_event: Optional[asyncio.Event],
        max_attempts: int = 3,
    ) -> None:
        """Navigate to Arena with timeout-aware retries and context refresh."""
        assert self._page is not None
        timeout_schedule = (30_000, 45_000, 60_000)
        attempts = max(1, min(max_attempts, len(timeout_schedule)))
        last_exc: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            timeout_ms = timeout_schedule[attempt - 1]
            try:
                await self._ensure_active(pause_event)
                await self._log(
                    "info",
                    (
                        f"Navigating to Arena side-by-side page "
                        f"(attempt {attempt}/{attempts}, timeout {timeout_ms // 1000}s)"
                    ),
                )
                await self._page.goto(
                    self._config.arena_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                return
            except Exception as exc:
                last_exc = exc
                timeout_hit = self._is_navigation_timeout(exc)
                self._report_proxy_navigation_failure(
                    "goto_timeout" if timeout_hit else "goto_error"
                )

                if attempt >= attempts:
                    await self.state_machine.force_error(str(exc))
                    raise NavigationError(str(exc), self._id) from exc

                if self._context_recreator is not None:
                    await self._log(
                        "warning",
                        (
                            f"Arena navigation attempt {attempt}/{attempts} failed; "
                            "reopening window and retrying"
                        ),
                    )
                    self._context = await self._context_recreator(self._id)
                    self._reset_zoom_service_worker_cache()
                    self._page = (
                        self._context.pages[0]
                        if self._context.pages
                        else await self._context.new_page()
                    )
                    await self._log_current_proxy("Navigation retry proxy")
                else:
                    await self._log(
                        "warning",
                        (
                            f"Arena navigation attempt {attempt}/{attempts} failed; "
                            "retrying in current window"
                        ),
                    )
                await self._sleep_with_controls(min(1.5 * attempt, 4.0), pause_event)

        if last_exc is not None:
            await self.state_machine.force_error(str(last_exc))
            raise NavigationError(str(last_exc), self._id) from last_exc

    @staticmethod
    def _normalize_text(value: Optional[str]) -> str:
        return " ".join((value or "").replace("\u200b", "").split()).strip()

    @classmethod
    def _normalize_model_name(cls, value: Optional[str]) -> str:
        return cls._normalize_text(value).lower()

    @classmethod
    def _model_names_match(
        cls,
        current_name: Optional[str],
        requested_name: Optional[str],
    ) -> bool:
        current = cls._normalize_model_name(current_name)
        requested = cls._normalize_model_name(requested_name)
        return bool(current and requested and current == requested)

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

                    const dismissCookieConsent = () => {
                        const looksLikeCookieScope = (text) => {
                            const normalized = (text || "").toLowerCase();
                            return (
                                normalized.includes("this website uses cookies") ||
                                normalized.includes("manage cookies") ||
                                normalized.includes("accept cookies") ||
                                normalized.includes("accept all cookies")
                            );
                        };

                        const scopes = Array.from(
                            document.querySelectorAll(
                                '[role="dialog"], [aria-modal="true"], .modal, .popup, .overlay, body'
                            )
                        );

                        for (const scope of scopes) {
                            if (scope !== document.body && !isVisible(scope)) continue;
                            if (!looksLikeCookieScope(scope.innerText || "")) continue;

                            const acceptButton = Array.from(
                                scope.querySelectorAll('button, [role="button"]')
                            ).find((btn) => {
                                const text = (btn.innerText || btn.textContent || "")
                                    .trim()
                                    .toLowerCase();
                                return (
                                    isVisible(btn) &&
                                    (
                                        text === "accept cookies" ||
                                        text === "accept all cookies" ||
                                        text === "accept"
                                    )
                                );
                            });

                            if (acceptButton) {
                                acceptButton.click();
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
                    window.__lmArenaDismissCookieConsent =
                        dismissCookieConsent;
                    window.__lmArenaDismissVotingOnboarding =
                        dismissVotingOnboarding;
                    if (!window.__lmArenaPageGuardsInstalled) {
                        window.__lmArenaPageGuardsInstalled = true;
                        const observer = new MutationObserver(() => {
                            hidePromos();
                            dismissCookieConsent();
                            dismissVotingOnboarding();
                        });
                        observer.observe(document.documentElement, {
                            childList: true,
                            subtree: true,
                        });
                        window.setInterval(() => {
                            dismissCookieConsent();
                            dismissVotingOnboarding();
                        }, 500);
                    }

                    hidePromos();
                    dismissCookieConsent();
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

        early_zoom_task: Optional[asyncio.Task[bool]] = None
        target_zoom = max(25, min(200, int(getattr(self, "_zoom_pct", 100))))
        if target_zoom != 100:
            early_zoom_task = asyncio.create_task(
                self._trigger_zoom_when_side_by_side_ready(
                    pause_event=pause_event,
                    timeout_seconds=max(dialog_wait_seconds, 10.0),
                )
            )

        await self._install_arena_page_guards()
        await self._dismiss_known_dialogs(
            wait_seconds=max(dialog_wait_seconds, 10.0),
            pause_event=pause_event,
        )
        await self._ensure_active(pause_event)
        if early_zoom_task is not None:
            try:
                await early_zoom_task
            except Exception as exc:
                await self._log(
                    "debug",
                    f"Early browser zoom trigger skipped: {exc}",
                )

    # ── Navigation ──

    async def _trigger_zoom_when_side_by_side_ready(
        self,
        pause_event: Optional[asyncio.Event] = None,
        timeout_seconds: float = 10.0,
    ) -> bool:
        assert self._page is not None

        target_zoom = max(25, min(200, int(getattr(self, "_zoom_pct", 100))))
        if target_zoom == 100:
            return False

        selector = self._selectors.get("side_by_side_button")
        deadline = asyncio.get_running_loop().time() + timeout_seconds

        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_active(pause_event)
            try:
                button = await self._page.wait_for_selector(
                    selector,
                    state="visible",
                    timeout=500,
                )
            except Exception:
                button = None

            if button is not None:
                await self._log(
                    "debug",
                    (
                        "Side by Side button detected; triggering browser zoom "
                        f"refresh to {target_zoom}%"
                    ),
                )
                return await self._refresh_managed_browser_zoom(target_zoom)

            await self._sleep_with_controls(0.1, pause_event)

        return False

    async def _wait_for_zoom_settle_before_model_selection(
        self,
        pause_event: Optional[asyncio.Event] = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        """Wait for a non-default browser zoom to settle before opening model pickers."""
        assert self._page is not None

        target_zoom = max(25, min(200, int(getattr(self, "_zoom_pct", 100))))
        if target_zoom == 100:
            return

        await self._log(
            "debug",
            f"Waiting for browser zoom {target_zoom}% to settle before model selection",
        )
        try:
            await self._page.wait_for_load_state("load", timeout=10_000)
        except Exception:
            pass

        await self._refresh_managed_browser_zoom(target_zoom)

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_signature: Optional[tuple] = None
        stable_samples = 0
        zoom_match_samples = 0
        zoom_state_seen = False
        target_zoom_ratio = target_zoom / 100.0

        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_active(pause_event)
            zoom_state = await self._get_managed_zoom_state()
            if zoom_state and zoom_state.get("ok"):
                arena_tabs = zoom_state.get("arenaTabs") or []
                matching_tabs = [
                    tab for tab in arena_tabs
                    if isinstance(tab, dict)
                    and isinstance(tab.get("zoom"), (int, float))
                    and abs(float(tab["zoom"]) - target_zoom_ratio) <= 0.02
                ]
                zoom_state_seen = zoom_state_seen or bool(arena_tabs)
                if matching_tabs:
                    zoom_match_samples += 1
                else:
                    zoom_match_samples = 0

            snapshot = await self._page.evaluate(
                """() => ({
                    readyState: document.readyState,
                    innerWidth: window.innerWidth,
                    innerHeight: window.innerHeight,
                    dpr: window.devicePixelRatio || 1,
                })"""
            )
            signature = (
                snapshot.get("readyState"),
                snapshot.get("innerWidth"),
                snapshot.get("innerHeight"),
                round(float(snapshot.get("dpr") or 1), 3),
            )
            if signature == last_signature and snapshot.get("readyState") == "complete":
                stable_samples += 1
            else:
                stable_samples = 1
                last_signature = signature

            if zoom_match_samples >= 2 and stable_samples >= 2:
                return
            if not zoom_state_seen and stable_samples >= 4:
                return

            await self._sleep_with_controls(0.25, pause_event)

        await self._log(
            "debug",
            "Browser zoom did not fully settle before timeout; continuing with model selection",
        )

    async def _get_zoom_service_worker(self) -> Optional[Worker]:
        if self._zoom_service_worker is not None:
            return self._zoom_service_worker

        workers = self._context.service_workers
        if workers:
            self._zoom_service_worker = workers[0]
            self._zoom_service_worker_checked = True
            return self._zoom_service_worker
        if self._zoom_service_worker_checked:
            return None
        try:
            self._zoom_service_worker = await self._context.wait_for_event(
                "serviceworker",
                timeout=5_000,
            )
            self._zoom_service_worker_checked = True
            return self._zoom_service_worker
        except Exception as exc:
            self._zoom_service_worker_checked = True
            await self._log(
                "debug",
                f"Zoom extension service worker unavailable: {exc}",
            )
            return None

    def _reset_zoom_service_worker_cache(self) -> None:
        self._zoom_service_worker = None
        self._zoom_service_worker_checked = False

    async def _refresh_managed_browser_zoom(self, target_zoom: int) -> bool:
        worker = await self._get_zoom_service_worker()
        if worker is None:
            return False

        try:
            result = await worker.evaluate(
                """async zoomPct => {
                    if (typeof globalThis.configureManagedZoom !== "function") {
                        return { ok: false, error: "configureManagedZoom missing" };
                    }
                    return await globalThis.configureManagedZoom(zoomPct);
                }""",
                target_zoom,
            )
            return bool(isinstance(result, dict) and result.get("ok"))
        except Exception as exc:
            await self._log(
                "debug",
                f"Managed zoom refresh skipped: {exc}",
            )
            return False

    async def _get_managed_zoom_state(self) -> Optional[dict]:
        worker = await self._get_zoom_service_worker()
        if worker is None:
            return None

        try:
            result = await worker.evaluate(
                """async () => {
                    if (typeof globalThis.getManagedZoomState !== "function") {
                        return { ok: false, error: "getManagedZoomState missing" };
                    }
                    return await globalThis.getManagedZoomState();
                }"""
            )
            return result if isinstance(result, dict) else None
        except Exception as exc:
            await self._log(
                "debug",
                f"Managed zoom state check skipped: {exc}",
            )
            return None

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
        await self._log("info", "Preparing browser context for Arena")

        # Optionally clear cookies before navigation.
        if clear_cookies:
            await self._log("info", "Clearing cookies before Arena navigation")
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
        await self._goto_arena_with_retries(
            pause_event=pause_event,
            max_attempts=3,
        )

        pre_challenge = ChallengeType.NONE
        try:
            await self._stabilize_loaded_page(
                pause_event=pause_event,
                dialog_wait_seconds=5.0,
            )
        except ChallengeDetectedError as exc:
            if exc.challenge_type == "terms_blocked":
                pre_challenge = ChallengeType.SECURITY_MODAL
            else:
                raise
        if clear_cookies:
            await self._show_in_browser_toast("Cookies cleared successfully")

        # Check for challenges (including login dialog) — retry by reopening window
        await self._ensure_active(pause_event)
        challenge = (
            pre_challenge
            if pre_challenge != ChallengeType.NONE
            else await detect_challenge(self._page)
        )
        if challenge != ChallengeType.NONE:
            await self.state_machine.transition(
                WorkerState.WAITING_FOR_CHALLENGE
            )
            await self._publish(
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
        else:
            # No challenge — proxy worked, report success
            if self._proxy_success_reporter:
                self._proxy_success_reporter(self._id)

        await self.state_machine.transition(WorkerState.READY)
        await self._log("info", "Arena page is ready for prompt entry")

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
        button = await self._find_dialog_button_by_text(
            button_text="Accept Cookies",
            dialog_text_snippets=(
                "this website uses cookies",
                "accept cookies",
                "manage cookies",
                "personalize content and ads",
            ),
            selector_key="cookie_accept_button",
        )
        if button is not None:
            return button

        assert self._page is not None
        handle = await self._page.evaluate_handle(
            """() => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };

                const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const buttons = Array.from(
                    document.querySelectorAll('button, [role="button"]')
                ).filter(isVisible);

                for (const btn of buttons) {
                    const text = norm(btn.innerText || btn.textContent || '');
                    if (
                        text !== 'accept cookies' &&
                        text !== 'accept all cookies' &&
                        text !== 'accept'
                    ) {
                        continue;
                    }

                    const scope =
                        btn.closest('[role="dialog"], [aria-modal="true"], .modal, .popup, .overlay')
                        || btn.parentElement
                        || document.body;
                    const scopeText = norm(scope.innerText || '');
                    if (
                        scopeText.includes('cookies') ||
                        scopeText.includes('manage cookies') ||
                        scopeText.includes('personalize content and ads')
                    ) {
                        return btn;
                    }
                }

                return null;
            }"""
        )
        return handle.as_element()

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

    async def _find_terms_of_use_button(self) -> Optional[ElementHandle]:
        """Find the Terms-of-Use accept/agree button with stricter matching.

        The selector fallback in config can be broad. This helper validates
        the dialog text so we do not keep clicking unrelated submit buttons.
        """
        assert self._page is not None

        # 1) Prefer configured selector, but only if its dialog looks like ToS.
        try:
            selector = self._selectors.get("tos_agree_button")
            button = await self._find_visible_dialog_button(selector)
            if button is not None:
                is_tos = await button.evaluate(
                    """btn => {
                        const dialog = btn && btn.closest('[role="dialog"]');
                        const text = (dialog && dialog.innerText ? dialog.innerText : '').toLowerCase();
                        return (
                            text.includes('terms of use') ||
                            text.includes('terms') ||
                            text.includes('privacy policy') ||
                            text.includes('policy')
                        );
                    }"""
                )
                if is_tos:
                    return button
        except Exception:
            pass

        # 2) Robust fallback by dialog + button text heuristics.
        handle = await self._page.evaluate_handle(
            """() => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };

                const normalize = (s) => (s || '').trim().toLowerCase();
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'))
                    .filter(isVisible);

                for (const dialog of dialogs) {
                    const text = normalize(dialog.innerText);
                    const looksLikeTos =
                        text.includes('terms of use') ||
                        text.includes('terms') ||
                        text.includes('privacy policy') ||
                        text.includes('policy');
                    if (!looksLikeTos) continue;

                    const button = Array.from(dialog.querySelectorAll('button')).find((btn) => {
                        if (!isVisible(btn)) return false;
                        const t = normalize(btn.innerText);
                        return (
                            t === 'agree' ||
                            t === 'i agree' ||
                            t === 'accept' ||
                            t === 'continue'
                        );
                    });
                    if (button) return button;
                }
                return null;
            }"""
        )
        return handle.as_element()

    async def _is_terms_button_clickable(
        self,
        button: ElementHandle,
    ) -> bool:
        """Best-effort check whether the Terms Agree button is actionable."""
        try:
            return bool(
                await button.evaluate(
                    """btn => {
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

                        if (!isVisible(btn)) return false;
                        if (btn.disabled) return false;
                        if (btn.getAttribute("aria-disabled") === "true") return false;
                        if (window.getComputedStyle(btn).pointerEvents === "none") return false;

                        const rect = btn.getBoundingClientRect();
                        const cx = rect.left + rect.width / 2;
                        const cy = rect.top + rect.height / 2;
                        const topEl = document.elementFromPoint(cx, cy);
                        if (!topEl) return false;
                        return btn.contains(topEl) || topEl.contains(btn);
                    }"""
                )
            )
        except Exception:
            return False

    async def _click_dialog_button(
        self,
        button: ElementHandle,
        selector_key: str,
        pause_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Click a dialog button with resilient fallbacks.

        Returns ``True`` if any click strategy executed without raising.
        """
        assert self._page is not None
        errors: list[str] = []

        try:
            await button.click(force=True, timeout=3_000)
            return True
        except Exception as exc:
            errors.append(str(exc))

        try:
            await button.evaluate(
                """btn => {
                    if (!btn) return;
                    btn.dispatchEvent(new MouseEvent("click", {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                    }));
                    if (typeof btn.click === "function") {
                        btn.click();
                    }
                }"""
            )
            await self._sleep_with_controls(0.1, pause_event)
            return True
        except Exception as exc:
            errors.append(str(exc))

        if selector_key == "cookie_accept_button":
            try:
                await self._page.keyboard.press("Enter")
                await self._sleep_with_controls(0.1, pause_event)
                return True
            except Exception as exc:
                errors.append(str(exc))

        if errors:
            await self._log(
                "debug",
                f"Dialog click failed for {selector_key}: {errors[-1]}",
            )
        return False

    async def _accept_cookie_dialog_if_present(
        self,
        pause_event: Optional[asyncio.Event] = None,
    ) -> bool:
        """Aggressively accept the Arena cookie dialog when it is present."""
        assert self._page is not None

        try:
            dialog = self._page.locator("[role='dialog']").filter(
                has_text="This website uses cookies"
            )
            accept_button = dialog.get_by_role("button", name="Accept Cookies")
            if await accept_button.count() > 0:
                await self._log(
                    "info",
                    "Cookie consent dialog detected; clicking 'Accept Cookies'",
                )
                target = accept_button.last
                try:
                    await target.scroll_into_view_if_needed(timeout=2_000)
                except Exception:
                    pass
                await target.click(force=True, timeout=3_000)
                await self._sleep_with_controls(0.3, pause_event)
                return True
        except Exception as exc:
            await self._log(
                "debug",
                f"Locator-based cookie dialog click failed: {exc}",
            )

        try:
            accept_button = self._page.locator(
                "button:has-text('Accept Cookies'), "
                "button:has-text('Accept All Cookies'), "
                "[role='button']:has-text('Accept Cookies'), "
                "[role='button']:has-text('Accept All Cookies')"
            ).first
            if await accept_button.count() > 0:
                await self._log(
                    "info",
                    "Cookie consent button detected; clicking accept",
                )
                try:
                    await accept_button.scroll_into_view_if_needed(
                        timeout=2_000
                    )
                except Exception:
                    pass
                await accept_button.click(force=True, timeout=3_000)
                await self._sleep_with_controls(0.3, pause_event)
                return True
        except Exception as exc:
            await self._log(
                "debug",
                f"Global cookie button click failed: {exc}",
            )

        try:
            button = await self._find_cookie_accept_button()
            if button is not None:
                await self._log(
                    "info",
                    "Cookie consent dialog detected; clicking 'Accept Cookies'",
                )
                if await self._click_dialog_button(
                    button,
                    "cookie_accept_button",
                    pause_event,
                ):
                    await self._sleep_with_controls(0.2, pause_event)
                    return True
        except Exception as exc:
            await self._log(
                "debug",
                f"Element-handle cookie dialog click failed: {exc}",
            )

        try:
            clicked = bool(
                await self._page.evaluate(
                    """() => {
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
                        const norm = (s) => (s || "").trim().toLowerCase();

                        const scopes = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], .modal, .popup, .overlay, body'))
                            .filter((el) => el === document.body || isVisible(el));
                        for (const scope of scopes) {
                            const text = norm(scope.innerText);
                            if (
                                !text.includes("this website uses cookies") &&
                                !text.includes("manage cookies") &&
                                !text.includes("accept cookies")
                            ) continue;
                            const accept = Array.from(scope.querySelectorAll('button, [role="button"]')).find((btn) => {
                                const btnText = norm(btn.innerText || btn.textContent);
                                return (
                                    isVisible(btn) &&
                                    (
                                        btnText === "accept cookies" ||
                                        btnText === "accept all cookies" ||
                                        btnText === "accept"
                                    )
                                );
                            });
                            if (!accept) continue;
                            accept.dispatchEvent(new MouseEvent("click", {
                                bubbles: true,
                                cancelable: true,
                                view: window,
                            }));
                            if (typeof accept.click === "function") {
                                accept.click();
                            }
                            return true;
                        }
                        return false;
                    }"""
                )
            )
            if not clicked:
                return False
            await self._log(
                "info",
                "Cookie consent dialog detected; clicking 'Accept Cookies' via DOM fallback",
            )
            await self._sleep_with_controls(0.2, pause_event)
            return True
        except Exception as exc:
            await self._log(
                "debug",
                f"DOM fallback cookie dialog click failed: {exc}",
            )
            return False

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
            ("voting_onboarding_got_it_button", "Dismissed voting onboarding dialog"),
        ]
        click_counts: dict[str, int] = {}
        max_clicks_per_handler = {
            "cookie_accept_button": 4,
            "tos_agree_button": 2,
            "voting_onboarding_got_it_button": 2,
        }

        try:
            while True:
                await self._ensure_active(pause_event)
                dismissed_this_pass = False

                if await self._accept_cookie_dialog_if_present(pause_event):
                    click_counts["cookie_accept_button"] = (
                        click_counts.get("cookie_accept_button", 0) + 1
                    )
                    still_present = (
                        await self._find_cookie_accept_button()
                    ) is not None
                    if not still_present:
                        await self._log(
                            "info",
                            "Cookie consent dialog closed after clicking 'Accept Cookies'",
                        )
                        dismissed_any = True
                        dismissed_this_pass = True
                        continue
                    await self._log(
                        "debug",
                        (
                            "Cookie consent dialog is still visible after "
                            f"click attempt {click_counts['cookie_accept_button']}; "
                            "retrying"
                        ),
                    )

                for selector_key, log_text in handlers:
                    if selector_key == "cookie_accept_button":
                        button = await self._find_cookie_accept_button()
                    elif selector_key == "tos_agree_button":
                        button = await self._find_terms_of_use_button()
                    elif selector_key == "voting_onboarding_got_it_button":
                        button = await self._find_voting_onboarding_button()
                    else:
                        selector = self._selectors.get(selector_key)
                        button = await self._find_visible_dialog_button(selector)
                    if button is None:
                        continue

                    if selector_key == "tos_agree_button":
                        clickable = await self._is_terms_button_clickable(button)
                        if not clickable:
                            await self._log(
                                "warning",
                                "Terms dialog detected but Agree is not clickable; "
                                "treating as challenge and reopening with proxy",
                            )
                            raise ChallengeDetectedError(
                                self._id, "terms_blocked"
                            )

                    attempts = click_counts.get(selector_key, 0)
                    max_attempts = max_clicks_per_handler.get(selector_key, 2)
                    if attempts >= max_attempts:
                        continue

                    clicked = await self._click_dialog_button(
                        button=button,
                        selector_key=selector_key,
                        pause_event=pause_event,
                    )
                    if not clicked:
                        continue

                    click_counts[selector_key] = attempts + 1
                    await self._sleep_with_controls(0.5, pause_event)

                    if selector_key == "cookie_accept_button":
                        still_present = (
                            await self._find_cookie_accept_button()
                        ) is not None
                    elif selector_key == "tos_agree_button":
                        still_present = (
                            await self._find_terms_of_use_button()
                        ) is not None
                    elif selector_key == "voting_onboarding_got_it_button":
                        still_present = (
                            await self._find_voting_onboarding_button()
                        ) is not None
                    else:
                        still_present = False

                    if still_present:
                        if (
                            selector_key == "tos_agree_button"
                            and click_counts[selector_key]
                            >= max_attempts
                        ):
                            await self._log(
                                "warning",
                                "Terms dialog stayed after retries; "
                                "treating as challenge and reopening with proxy",
                            )
                            raise ChallengeDetectedError(
                                self._id, "terms_blocked"
                            )
                        if click_counts[selector_key] >= max_attempts:
                            await self._log(
                                "debug",
                                f"{selector_key} dialog remained after retries",
                            )
                        continue

                    # Log the first dismissal at info level; suppress repetitive spam.
                    if click_counts[selector_key] == 1:
                        await self._log("info", log_text)
                    else:
                        await self._log(
                            "debug",
                            f"{log_text} (retry {click_counts[selector_key]})",
                        )

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
        except ChallengeDetectedError:
            raise
        except Exception as exc:
            await self._log("debug", f"Dialog dismissal skipped: {exc}")
            return dismissed_any

    async def _handle_challenge(
        self,
        challenge: ChallengeType,
        max_retries: int = CHALLENGE_RETRY_LIMIT,
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
                    await self._publish(
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
            self._reset_zoom_service_worker_cache()
            if clear_cookies:
                await self._clear_cookies()
            self._page = (
                self._context.pages[0]
                if self._context.pages
                else await self._context.new_page()
            )
            await self._log_current_proxy("Challenge retry proxy")

            # Re-navigate
            await self.state_machine.transition(WorkerState.NAVIGATING)
            await self._log("info", "Fresh window opened; navigating back to Arena")
            await self._goto_arena_with_retries(
                pause_event=pause_event,
                max_attempts=2,
            )

            pre_challenge = ChallengeType.NONE
            try:
                await self._stabilize_loaded_page(
                    pause_event=pause_event,
                    dialog_wait_seconds=5.0,
                )
            except ChallengeDetectedError as exc:
                if exc.challenge_type == "terms_blocked":
                    pre_challenge = ChallengeType.SECURITY_MODAL
                else:
                    raise
            if clear_cookies:
                await self._show_in_browser_toast("Cookies cleared successfully")

            await self._ensure_active(pause_event)
            challenge = (
                pre_challenge
                if pre_challenge != ChallengeType.NONE
                else await detect_challenge(self._page)
            )
            if challenge == ChallengeType.NONE:
                if self._proxy_success_reporter:
                    self._proxy_success_reporter(self._id)
                await self._publish(
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
        retry_on_challenge: int = CHALLENGE_RETRY_LIMIT,
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

        try:
            await self._dismiss_known_dialogs(
                wait_seconds=1.0,
                pause_event=pause_event,
            )
        except ChallengeDetectedError as exc:
            if (
                exc.challenge_type != "terms_blocked"
                or retry_on_challenge <= 0
                or self._context_recreator is None
            ):
                raise
            await self._log(
                "warning",
                "Terms Agree is blocked; reopening window with proxy and retrying prepare",
            )
            await self.reset_with_fresh_context(
                zoom_pct=self._zoom_pct,
                clear_cookies=False,
                pause_event=pause_event,
            )
            return await self.prepare_prompt(
                prompt=prompt,
                model_a=model_a,
                model_b=model_b,
                mark_started=mark_started,
                retry_on_challenge=retry_on_challenge - 1,
                pause_event=pause_event,
                images=images,
            )

        if model_a or model_b:
            await self.state_machine.transition(WorkerState.SELECTING_MODEL)
            await self._wait_for_zoom_settle_before_model_selection(
                pause_event=pause_event,
            )
            try:
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
            except ModelSelectionError:
                if retry_on_challenge <= 0 or self._context_recreator is None:
                    raise
                await self._log(
                    "warning",
                    "Model selector did not become usable within 30s; reopening a fresh window and retrying prepare",
                )
                await self.reset_with_fresh_context(
                    zoom_pct=self._zoom_pct,
                    clear_cookies=False,
                    pause_event=pause_event,
                )
                return await self.prepare_prompt(
                    prompt=prompt,
                    model_a=model_a,
                    model_b=model_b,
                    mark_started=mark_started,
                    retry_on_challenge=retry_on_challenge - 1,
                    pause_event=pause_event,
                    images=images,
                )

        # Paste prompt
        await self.state_machine.transition(WorkerState.PASTING)
        await self._ensure_active(pause_event)
        await self._log("info", "Entering prompt text into the Arena composer")
        textarea_sel = self._selectors.get("prompt_textarea")
        has_images = bool(images)
        try:
            prompt_element = await self._human.type_text(
                self._page, textarea_sel, prompt, verify=not has_images,
            )
        except RuntimeError as exc:
            message = str(exc)
            missing_composer = message.startswith("Element not found:")
            if (
                not missing_composer
                or retry_on_challenge <= 0
                or self._context_recreator is None
            ):
                raise
            await self._log(
                "warning",
                "Prompt composer not found; reopening window with proxy and retrying prepare",
            )
            await self.reset_with_fresh_context(
                zoom_pct=self._zoom_pct,
                clear_cookies=False,
                pause_event=pause_event,
            )
            return await self.prepare_prompt(
                prompt=prompt,
                model_a=model_a,
                model_b=model_b,
                mark_started=mark_started,
                retry_on_challenge=retry_on_challenge - 1,
                pause_event=pause_event,
                images=images,
            )
        await self._log("info", "Prompt text entered into the Arena composer")

        # Paste images if provided
        if has_images:
            await self._log(
                "info",
                f"Pasting {len(images)} image(s) into the Arena composer",
            )
            await self._human.paste_images(
                self._page, prompt_element, images,
            )
            await self._log("info", f"Attached {len(images)} image(s) to the prompt")

        await self.state_machine.transition(WorkerState.PREPARED)
        await self._log("info", "Prompt composer is ready for submission")

    async def submit_prepared_prompt(
        self,
        retry_on_challenge: int = CHALLENGE_RETRY_LIMIT,
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
            await self._log(
                "info",
                f"Submitting prepared prompt (attempt {attempt}/3)",
            )
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

            try:
                last_snapshot = await self._wait_for_submission_acceptance(
                    expected_prompt=self._last_prompt or "",
                    pause_event=pause_event,
                    timeout_seconds=3.0,
                )
            except ChallengeDetectedError as exc:
                return await self._recover_submit_path_after_challenge(
                    exc=exc,
                    retry_on_challenge=retry_on_challenge,
                    pause_event=pause_event,
                    message=(
                        f"Challenge ({exc.challenge_type}) appeared right after submit - "
                        "reopening window with a fresh IP and retrying"
                    ),
                )
            if last_snapshot.get("accepted"):
                submit_accepted = True
                break

            try:
                dialog_dismissed = await self._dismiss_known_dialogs(
                    wait_seconds=1.5,
                    pause_event=pause_event,
                )
            except ChallengeDetectedError as exc:
                if (
                    exc.challenge_type != "terms_blocked"
                    or retry_on_challenge <= 0
                    or self._context_recreator is None
                ):
                    raise
                await self._log(
                    "warning",
                    "Terms Agree is blocked after submit; reopening window with proxy and retrying",
                )
                await self.reset_with_fresh_context(
                    zoom_pct=self._zoom_pct,
                    clear_cookies=False,
                    pause_event=pause_event,
                )
                return await self.submit_prompt(
                    prompt=self._last_prompt,
                    model_a=self._last_model_a,
                    model_b=self._last_model_b,
                    retry_on_challenge=retry_on_challenge - 1,
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
                    max_retries=CHALLENGE_RETRY_LIMIT,
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

            try:
                last_snapshot = await self._wait_for_submission_acceptance(
                    expected_prompt=self._last_prompt or "",
                    pause_event=pause_event,
                    timeout_seconds=2.0,
                )
            except ChallengeDetectedError as exc:
                return await self._recover_submit_path_after_challenge(
                    exc=exc,
                    retry_on_challenge=retry_on_challenge,
                    pause_event=pause_event,
                    message=(
                        f"Challenge ({exc.challenge_type}) appeared after dialog handling - "
                        "reopening window with a fresh IP and retrying"
                    ),
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
                # One extra grace window before re-clicking submit.
                try:
                    grace_snapshot = await self._wait_for_submission_acceptance(
                        expected_prompt=self._last_prompt or "",
                        pause_event=pause_event,
                        timeout_seconds=4.0,
                        poll_interval=0.4,
                    )
                except ChallengeDetectedError as exc:
                    return await self._recover_submit_path_after_challenge(
                        exc=exc,
                        retry_on_challenge=retry_on_challenge,
                        pause_event=pause_event,
                        message=(
                            f"Challenge ({exc.challenge_type}) appeared while waiting for the submit result - "
                            "reopening window with a fresh IP and retrying"
                        ),
                    )
                last_snapshot = grace_snapshot
                if grace_snapshot.get("accepted"):
                    submit_accepted = True
                    await self._log(
                        "info",
                        "Submission accepted after a delayed UI update",
                    )
                    break

                if not self._should_retry_submit(grace_snapshot):
                    # Ambiguous state: avoid a duplicate send and move to polling.
                    submit_accepted = True
                    await self._log(
                        "warning",
                        "Submission state is ambiguous; skipping re-submit to avoid duplicate prompt",
                    )
                    break

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
                    f", submit_in_viewport={last_snapshot.get('submit_in_viewport')}"
                    f", submit_enabled={last_snapshot.get('submit_enabled')}"
                    f", stop_visible={last_snapshot.get('stop_visible')}"
                )
            raise SubmissionError(
                f"Prompt submission was not accepted after 3 attempts.{details}",
                self._id,
            )

        await self._log(
            "info",
            "Prompt submission accepted; switching to response polling",
        )

        try:
            generation_snapshot = await self._wait_for_generation_start_after_submit(
                pause_event=pause_event,
                timeout_seconds=30.0,
            )
        except ChallengeDetectedError as exc:
            return await self._recover_submit_path_after_challenge(
                exc=exc,
                retry_on_challenge=retry_on_challenge,
                pause_event=pause_event,
                message=(
                    f"Challenge ({exc.challenge_type}) appeared before generation started - "
                    "reopening window with a fresh IP and retrying"
                ),
            )

        if not generation_snapshot.get("generation_started"):
            if retry_on_challenge <= 0 or self._context_recreator is None:
                raise SubmissionError(
                    "Generation did not start within 30 seconds after submit",
                    self._id,
                )
            await self._log(
                "warning",
                "Generation did not start within 30 seconds after submit; "
                "reopening window with a fresh IP and retrying",
            )
            await self.reset_with_fresh_context(
                zoom_pct=self._zoom_pct,
                clear_cookies=False,
                pause_event=pause_event,
            )
            return await self.submit_prompt(
                prompt=self._last_prompt,
                model_a=self._last_model_a,
                model_b=self._last_model_b,
                retry_on_challenge=retry_on_challenge - 1,
                pause_event=pause_event,
            )

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
                max_retries=CHALLENGE_RETRY_LIMIT,
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
        retry_on_challenge: int = CHALLENGE_RETRY_LIMIT,
        pause_event: Optional[asyncio.Event] = None,
        images: Optional[list] = None,
    ) -> None:
        """Select models (optional), paste prompt, and submit."""
        try:
            await self.prepare_prompt(
                prompt=prompt,
                model_a=model_a,
                model_b=model_b,
                mark_started=True,
                retry_on_challenge=retry_on_challenge,
                pause_event=pause_event,
                images=images,
            )
        except ChallengeDetectedError as exc:
            if (
                exc.challenge_type != "terms_blocked"
                or retry_on_challenge <= 0
                or self._context_recreator is None
            ):
                raise
            await self._log(
                "warning",
                "Terms Agree is blocked; reopening window with proxy and retrying prompt",
            )
            await self.reset_with_fresh_context(
                zoom_pct=self._zoom_pct,
                clear_cookies=False,
                pause_event=pause_event,
            )
            return await self.submit_prompt(
                prompt=prompt,
                model_a=model_a,
                model_b=model_b,
                retry_on_challenge=retry_on_challenge - 1,
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
        scroll it into view when needed, and fall back to submitting the form
        directly before trying Enter on the editor.
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
                await submit_button.scroll_into_view_if_needed(timeout=2_000)
                await self._sleep_with_controls(0.1, pause_event)
                await self._human.click_element(self._page, submit_button)
                return
            except Exception as exc:
                await self._log(
                    "debug",
                    f"Direct submit button click failed: {exc}",
                )
            try:
                await self._ensure_active(pause_event)
                await submit_button.scroll_into_view_if_needed(timeout=2_000)
                await submit_button.click(force=True, timeout=2_000)
                return
            except Exception as exc:
                await self._log(
                    "debug",
                    f"Forced submit button click failed: {exc}",
                )

        try:
            await self._ensure_active(pause_event)
            submitted_via_form = await prompt_element.evaluate(
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
                        return [...buttons].reverse().find(isEnabled)
                            ?? [...buttons].reverse()[0]
                            ?? null;
                    };

                    const form = promptEl.closest("form");
                    if (!form || typeof form.requestSubmit !== "function") {
                        return false;
                    }

                    const submitButton = findButton(form);
                    if (submitButton) {
                        submitButton.scrollIntoView({
                            block: "center",
                            inline: "nearest",
                            behavior: "auto",
                        });
                        form.requestSubmit(submitButton);
                        return true;
                    }

                    form.requestSubmit();
                    return true;
                }""",
                submit_sel,
            )
            if submitted_via_form:
                await self._log(
                    "info",
                    "Submit button click failed; submitted via form.requestSubmit()",
                )
                return
        except Exception as exc:
            await self._log(
                "debug",
                f"Form requestSubmit fallback failed: {exc}",
            )

        await self._log(
            "warning",
            "Submit button was not clickable; form submit failed; retrying with Enter on the prompt editor",
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

                const isInViewport = (el) => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    return (
                        rect.bottom > 0 &&
                        rect.right > 0 &&
                        rect.top < window.innerHeight &&
                        rect.left < window.innerWidth
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
                    submit_in_viewport: submitButton ? isInViewport(submitButton) : false,
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
            challenge = await detect_challenge(self._page)
            if challenge != ChallengeType.NONE:
                raise ChallengeDetectedError(self._id, challenge.value)
            latest = await self._get_submission_snapshot(expected_prompt)
            if latest.get("accepted"):
                return latest
            await self._sleep_with_controls(poll_interval, pause_event)
        return latest

    async def _get_generation_start_snapshot(self) -> dict:
        assert self._page is not None

        slide_sel = self._selectors.get("response_slide")
        stop_sel = self._selectors.get("stop_generation_button")
        streaming_sel = self._selectors.get("streaming_indicator")
        textarea_sel = self._selectors.get("prompt_textarea")

        snapshot = await self._page.evaluate(
            """({ slideSelector, stopSelector, streamingSelector, textareaSelector }) => {
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
                    (value || "").replace(/\\u200b/g, "").replace(/\\s+/g, " ").trim();

                const readPromptValue = (el) => {
                    if (!el) return "";
                    const tag = (el.tagName || "").toLowerCase();
                    if (tag === "textarea" || tag === "input") {
                        return el.value || "";
                    }
                    return el.innerText || el.textContent || "";
                };

                const pathname = window.location.pathname || "";
                const textarea = Array.from(document.querySelectorAll(textareaSelector))
                    .filter(isVisible)
                    .pop() || null;
                const promptValue = normalize(readPromptValue(textarea));
                const stopVisible = Array.from(document.querySelectorAll(stopSelector))
                    .some(isVisible);
                const streamingVisible = Array.from(document.querySelectorAll(streamingSelector))
                    .some(isVisible);
                const slides = Array.from(document.querySelectorAll(slideSelector))
                    .filter(isVisible);
                const responseTextVisible = slides.some((slide) =>
                    Array.from(slide.querySelectorAll(".prose"))
                        .filter(isVisible)
                        .some((node) => normalize(node.innerText || node.textContent).length > 0)
                );

                const onConversationPage =
                    pathname.startsWith("/c/") || pathname.startsWith("/chat/");
                const generationStarted =
                    stopVisible ||
                    streamingVisible ||
                    responseTextVisible ||
                    onConversationPage;

                return {
                    generation_started: generationStarted,
                    stop_visible: stopVisible,
                    streaming_visible: streamingVisible,
                    response_text_visible: responseTextVisible,
                    response_slide_count: slides.length,
                    prompt_cleared: !promptValue,
                    pathname,
                };
            }""",
            {
                "slideSelector": slide_sel,
                "stopSelector": stop_sel,
                "streamingSelector": streaming_sel,
                "textareaSelector": textarea_sel,
            },
        )
        return snapshot if isinstance(snapshot, dict) else {"generation_started": False}

    async def _wait_for_generation_start_after_submit(
        self,
        pause_event: Optional[asyncio.Event] = None,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.5,
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        latest: dict = {"generation_started": False}

        while asyncio.get_running_loop().time() < deadline:
            await self._ensure_active(pause_event)

            try:
                await self._dismiss_known_dialogs(
                    wait_seconds=0.0,
                    pause_event=pause_event,
                )
            except ChallengeDetectedError:
                raise

            challenge = await detect_challenge(self._page)
            if challenge != ChallengeType.NONE:
                raise ChallengeDetectedError(self._id, challenge.value)

            latest = await self._get_generation_start_snapshot()
            if latest.get("generation_started"):
                return latest

            await self._sleep_with_controls(poll_interval, pause_event)

        return latest

    async def _recover_submit_path_after_challenge(
        self,
        exc: ChallengeDetectedError,
        retry_on_challenge: int,
        pause_event: Optional[asyncio.Event],
        message: str,
    ) -> None:
        if retry_on_challenge <= 0 or self._context_recreator is None:
            raise exc
        if self.state_machine.state != WorkerState.WAITING_FOR_CHALLENGE:
            await self.state_machine.transition(
                WorkerState.WAITING_FOR_CHALLENGE
            )
        recovery_challenge = (
            ChallengeType(exc.challenge_type)
            if exc.challenge_type in {item.value for item in ChallengeType}
            else ChallengeType.SECURITY_MODAL
        )
        await self._log("warning", message)
        await self._handle_challenge(
            recovery_challenge,
            max_retries=CHALLENGE_RETRY_LIMIT,
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

    @staticmethod
    def _should_retry_submit(snapshot: Optional[dict]) -> bool:
        """Return True only when we have strong evidence submit did not fire."""
        if not snapshot:
            return False
        return bool(
            snapshot.get("textarea_visible")
            and snapshot.get("submit_visible")
            and snapshot.get("submit_enabled")
            and snapshot.get("prompt_matches_expected")
            and not snapshot.get("stop_visible")
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

    async def _get_current_model_labels(self) -> list[str]:
        """Return visible side-by-side model labels ordered as A, then B."""
        assert self._page is not None
        dropdown_sel = self._selectors.get("model_dropdown")
        labels = await self._page.evaluate(
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

                const readLabel = (btn) => {
                    const labelNode =
                        btn.querySelector("span.truncate.text-left") ||
                        btn.querySelector("span.truncate") ||
                        btn.querySelector("span.font-mono") ||
                        btn;
                    const text = (labelNode.innerText || labelNode.textContent || "")
                        .split("\\n")
                        .map((part) => part.trim())
                        .filter(Boolean)
                        .join(" ");
                    return text.trim();
                };

                return Array.from(
                    new Set([
                        ...document.querySelectorAll(selector),
                        ...document.querySelectorAll('button[aria-haspopup="dialog"]'),
                    ])
                )
                    .filter(isVisible)
                    .filter(buttonMatches)
                    .sort((a, b) => buttonScore(b) - buttonScore(a))
                    .slice(0, 2)
                    .map(readLabel)
                    .filter(Boolean);
            }""",
            dropdown_sel,
        )
        return [self._normalize_text(label) for label in labels]

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
        current_models = await self._get_current_model_labels()
        current_label = current_models[index] if len(current_models) > index else ""
        if self._model_names_match(current_label, model_name):
            await self._log(
                "info",
                f"Model {label} already selected as '{current_label}'; skipping re-selection",
            )
            return

        other_index = 1 - index
        other_label = current_models[other_index] if len(current_models) > other_index else ""
        if self._model_names_match(other_label, model_name):
            other_side = "A" if other_index == 0 else "B"
            await self._log(
                "warning",
                f"Model {label} selection skipped: '{model_name}' is already selected on side {other_side}",
            )
            return

        timeout_seconds = 30.0
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_issue = f"Model {label} selector was not usable"
        try:
            option_sel = self._selectors.get("model_option")
            attempt = 0
            while asyncio.get_running_loop().time() < deadline:
                attempt += 1
                await self._ensure_active(pause_event)
                dropdown = await self._find_model_dropdown_button(index)
                if dropdown is None:
                    last_issue = f"Could not find model {label} dropdown button"
                    await self._log(
                        "debug",
                        f"{last_issue} on attempt {attempt}; retrying",
                    )
                    await self._sleep_with_controls(0.5, pause_event)
                    continue

                await self._ensure_active(pause_event)
                await self._human.click_element(self._page, dropdown)
                search_input = await self._wait_for_model_search_input(
                    pause_event=pause_event
                )
                if search_input is None:
                    last_issue = (
                        f"Model {label} picker search input was not ready"
                    )
                    await self._log(
                        "debug",
                        f"{last_issue} on attempt {attempt}; retrying",
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

                last_issue = (
                    f"Model {label} option for '{model_name}' was not ready"
                )
                await self._log(
                    "debug",
                    f"{last_issue} on attempt {attempt}; retrying",
                )
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Escape")
                await self._sleep_with_controls(0.4, pause_event)

            await self._log(
                "warning",
                f"{last_issue} for 30s; forcing fresh window reload",
            )
            try:
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Escape")
                await self._sleep_with_controls(0.3, pause_event)
            except Exception:
                pass
            raise ModelSelectionError(self._id, model_name)
        except ModelSelectionError:
            raise
        except Exception as exc:
            await self._log(
                "warning",
                f"Model selection failed: {exc}",
            )
            # Always close the dialog to avoid stealing focus from prompt input
            try:
                await self._ensure_active(pause_event)
                await self._page.keyboard.press("Escape")
                await self._sleep_with_controls(0.3, pause_event)
            except Exception:
                pass
            raise ModelSelectionError(self._id, model_name) from exc

    # ── Polling ──

    async def poll_for_completion(
        self,
        baseline_responses: Optional[tuple[str, str]] = None,
        pause_event: Optional[asyncio.Event] = None,
        _recovery_retries: int = CHALLENGE_RETRY_LIMIT,
        _poll_timeout_retries: int = 1,
        _poll_timeout_override_seconds: Optional[float] = None,
    ) -> WindowResult:
        """Poll DOM until both responses are stable. Returns result."""
        assert self._page is not None

        async def _on_slide_stable(
            slide_index: int,
            text: str,
            html: str,
            model_name: Optional[str],
        ) -> None:
            slide = "a" if slide_index == 0 else "b"
            await self._publish(
                Event(
                    type=EventType.WORKER_PARTIAL_RESULT,
                    worker_id=self._id,
                    data={
                        "slide": slide,
                        "model_name": model_name,
                        "response": text,
                        "response_html": html,
                    },
                )
            )

        try:
            (resp_a, resp_b), (name_a, name_b), (html_a, html_b) = await self._poller.poll(
                page=self._page,
                selectors=self._selectors,
                worker_id=self._id,
                cancel_event=self._cancel_event,
                pause_event=pause_event,
                baseline_responses=baseline_responses,
                timeout_override_seconds=_poll_timeout_override_seconds,
                on_slide_stable=_on_slide_stable,
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
            await self._publish(
                Event(
                    type=EventType.WORKER_COMPLETE,
                    worker_id=self._id,
                    data={"result": self._result.model_dump(mode="json")},
                )
            )
            return self._result

        except GenerationFailedBannerError:
            if _recovery_retries <= 0 or self._context_recreator is None:
                raise

            return await self._recover_from_polling_restart(
                reason_text=(
                    "Generation failed banner detected — closing window, "
                    "reopening fresh, and retrying"
                ),
                recovery_type="generation_error",
                proxy_log_context="Generation-error retry proxy",
                baseline_responses=baseline_responses,
                pause_event=pause_event,
                recovery_retries=_recovery_retries,
                poll_timeout_retries=_poll_timeout_retries,
                poll_timeout_override_seconds=_poll_timeout_override_seconds,
            )

        except ChallengeDetectedError as exc:
            if _recovery_retries <= 0 or self._context_recreator is None:
                raise

            return await self._recover_from_polling_restart(
                reason_text=(
                    f"Challenge ({exc.challenge_type}) detected during polling - "
                    "closing window, reopening fresh, and retrying"
                ),
                recovery_type=exc.challenge_type,
                proxy_log_context="Challenge retry proxy",
                baseline_responses=baseline_responses,
                pause_event=pause_event,
                recovery_retries=_recovery_retries,
                poll_timeout_retries=_poll_timeout_retries,
                poll_timeout_override_seconds=_poll_timeout_override_seconds,
            )

        except RateLimitError:
            if _recovery_retries <= 0 or self._context_recreator is None:
                raise  # fall through to general handler below

            return await self._recover_from_polling_restart(
                reason_text=(
                    "Rate limit hit — closing window, reopening fresh, "
                    "and retrying"
                ),
                recovery_type="rate_limit",
                proxy_log_context="Rate-limit retry proxy",
                baseline_responses=baseline_responses,
                pause_event=pause_event,
                recovery_retries=_recovery_retries,
                poll_timeout_retries=_poll_timeout_retries,
                poll_timeout_override_seconds=_poll_timeout_override_seconds,
            )

        except LoginDialogError:
            if _recovery_retries <= 0 or self._context_recreator is None:
                raise

            return await self._recover_from_polling_restart(
                reason_text=(
                    "Login dialog detected — closing window, reopening "
                    "fresh, and retrying"
                ),
                recovery_type="login_wall",
                proxy_log_context="Login-wall retry proxy",
                baseline_responses=baseline_responses,
                pause_event=pause_event,
                recovery_retries=_recovery_retries,
                poll_timeout_retries=_poll_timeout_retries,
                poll_timeout_override_seconds=_poll_timeout_override_seconds,
            )

        except PollingTimeoutError as exc:
            if _poll_timeout_retries > 0:
                await self._log(
                    "warning",
                    (
                        "Polling timed out; waiting for late response and "
                        "retrying poll once"
                    ),
                )
                return await self.poll_for_completion(
                    baseline_responses=baseline_responses,
                    pause_event=pause_event,
                    _recovery_retries=_recovery_retries,
                    _poll_timeout_retries=_poll_timeout_retries - 1,
                    _poll_timeout_override_seconds=120.0,
                )

            self._result = WindowResult(
                worker_id=self._id,
                prompt="",
                success=False,
                error=str(exc),
                started_at=self._started_at,
                completed_at=datetime.now(timezone.utc),
            )
            await self.state_machine.force_error(str(exc))
            await self._publish(
                Event(
                    type=EventType.WORKER_ERROR,
                    worker_id=self._id,
                    data={"error": str(exc)},
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
            await self._publish(
                Event(
                    type=EventType.WORKER_ERROR,
                    worker_id=self._id,
                    data={"error": str(exc)},
                )
            )
            return self._result

    async def _recover_from_polling_restart(
        self,
        *,
        reason_text: str,
        recovery_type: str,
        proxy_log_context: str,
        baseline_responses: Optional[tuple[str, str]],
        pause_event: Optional[asyncio.Event],
        recovery_retries: int,
        poll_timeout_retries: int,
        poll_timeout_override_seconds: Optional[float],
    ) -> WindowResult:
        assert self._context_recreator is not None
        assert self._last_prompt is not None

        await self._log("warning", reason_text)
        await self._publish(
            Event(
                type=EventType.CHALLENGE_DETECTED,
                worker_id=self._id,
                data={"challenge_type": recovery_type},
            )
        )

        await self.state_machine.transition(WorkerState.WAITING_FOR_CHALLENGE)

        await self._log(
            "info",
            f"Requesting a fresh browser context after '{recovery_type}' while polling",
        )
        self._context = await self._context_recreator(self._id)
        self._reset_zoom_service_worker_cache()
        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )
        await self._log_current_proxy(proxy_log_context)

        await self.state_machine.transition(WorkerState.NAVIGATING)
        await self._log(
            "info",
            f"Fresh browser context created after '{recovery_type}'; navigating back to Arena",
        )
        await self._goto_arena_with_retries(
            pause_event=pause_event,
            max_attempts=2,
        )

        await self._stabilize_loaded_page(
            pause_event=pause_event,
            dialog_wait_seconds=5.0,
        )

        await self._publish(
            Event(type=EventType.CHALLENGE_RESOLVED, worker_id=self._id)
        )

        await self.state_machine.transition(WorkerState.READY)
        await self._log(
            "info",
            "Replaying the last prompt in the refreshed browser context",
        )
        await self.submit_prompt(
            prompt=self._last_prompt,
            model_a=self._last_model_a,
            model_b=self._last_model_b,
            pause_event=pause_event,
        )
        await self._log(
            "info",
            "Prompt replay submitted; resuming response polling",
        )

        return await self.poll_for_completion(
            baseline_responses=baseline_responses,
            pause_event=pause_event,
            _recovery_retries=recovery_retries - 1,
            _poll_timeout_retries=poll_timeout_retries,
            _poll_timeout_override_seconds=poll_timeout_override_seconds,
        )

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
        self._reset_zoom_service_worker_cache()
        await self._log_current_proxy("Recovery reset proxy")

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
