from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from playwright.async_api import BrowserContext, Page

from src.browser.challenges import ChallengeType, detect_challenge
from src.browser.selectors import SelectorRegistry
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import (
    ChallengeDetectedError,
    NavigationError,
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

    def __init__(
        self,
        worker_id: int,
        context: BrowserContext,
        config: AppConfig,
        event_bus: EventBus,
    ) -> None:
        self._id = worker_id
        self._context = context
        self._config = config
        self._event_bus = event_bus
        self._page: Optional[Page] = None
        self._result: Optional[WindowResult] = None
        self._started_at: Optional[datetime] = None
        self._cancelled = False

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

    async def navigate_to_arena(self) -> None:
        """Navigate to the Arena direct chat page, handling challenges."""
        await self.state_machine.transition(WorkerState.LAUNCHING)

        self._page = (
            self._context.pages[0]
            if self._context.pages
            else await self._context.new_page()
        )

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

        # Dismiss Terms of Use dialog if present
        await self._dismiss_tos_dialog()

        # Check for challenges
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
            await self._log(
                "warning",
                f"Challenge ({challenge.value}) detected — "
                "please solve it manually in the browser window",
            )
            await self._wait_for_challenge_resolution(timeout=120)

        await self.state_machine.transition(WorkerState.READY)
        await self._log("info", "Ready")

    async def _wait_for_challenge_resolution(self, timeout: float) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self._cancelled:
                return
            challenge = await detect_challenge(self._page)
            if challenge == ChallengeType.NONE:
                await self._event_bus.publish(
                    Event(
                        type=EventType.CHALLENGE_RESOLVED,
                        worker_id=self._id,
                    )
                )
                return
            await asyncio.sleep(2)
        raise ChallengeDetectedError(self._id, "timeout")

    # ── Submission ──

    async def submit_prompt(
        self,
        prompt: str,
        model: Optional[str] = None,
    ) -> None:
        """Select model (optional), paste prompt, and submit."""
        assert self._page is not None
        self._started_at = datetime.now(timezone.utc)

        # Optional model selection
        if model:
            await self.state_machine.transition(WorkerState.SELECTING_MODEL)
            await self._select_model(model)

        # Paste prompt
        await self.state_machine.transition(WorkerState.PASTING)
        textarea_sel = self._selectors.get("prompt_textarea")
        await self._human.type_text(self._page, textarea_sel, prompt)
        await self._log("info", "Prompt pasted")

        # Submit
        await self.state_machine.transition(WorkerState.SUBMITTING)
        submit_sel = self._selectors.get("submit_button")
        await self._human.click(self._page, submit_sel)
        await self._log("info", "Submitted")

        # Transition to polling
        await self.state_machine.transition(WorkerState.POLLING)

    async def _select_model(self, model_name: str) -> None:
        """Click model dropdown, search, and select."""
        try:
            dropdown_sel = self._selectors.get("model_dropdown")
            await self._human.click(self._page, dropdown_sel)
            await asyncio.sleep(0.5)

            search_sel = self._selectors.get("model_search_input")
            await self._human.type_text(self._page, search_sel, model_name)
            await asyncio.sleep(0.5)

            option_sel = self._selectors.get("model_option")
            await self._human.click(self._page, option_sel)
            await self._log("info", f"Selected model '{model_name}'")
        except Exception as exc:
            await self._log(
                "warning",
                f"Model selection failed: {exc}. Using Arena default.",
            )

    # ── Polling ──

    async def poll_for_completion(self) -> WindowResult:
        """Poll DOM until both responses are stable. Returns result."""
        assert self._page is not None

        try:
            response, model_name = await self._poller.poll(
                page=self._page,
                selectors=self._selectors,
                worker_id=self._id,
            )

            completed_at = datetime.now(timezone.utc)
            elapsed = (
                (completed_at - self._started_at).total_seconds()
                if self._started_at
                else None
            )

            self._result = WindowResult(
                worker_id=self._id,
                prompt="",  # set by orchestrator
                model_name=model_name,
                response=response,
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
        if not self.state_machine.is_terminal:
            await self.state_machine.transition(WorkerState.CANCELLED)
