from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from src.browser.manager import BrowserManager
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import AllWorkersFailedError, RunCancelledError
from src.models.config import AppConfig
from src.models.messages import StartRunRequest
from src.models.results import RunResult, WindowResult
from src.models.worker import WorkerState
from src.workers.arena_worker import ArenaWorker

logger = logging.getLogger(__name__)


class RunOrchestrator:
    """Central coordinator for one automation run.

    Lifecycle:
    1. Receive ``StartRunRequest``
    2. Launch N browser contexts via ``BrowserManager``
    3. Create N ``ArenaWorker`` instances
    4. Navigate all workers to Arena (parallel)
    5. Submit prompts sequentially with staggered gap
    6. Poll all workers for completion (parallel)
    7. Collect results and emit ``RUN_COMPLETE``
    """

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        browser_manager: BrowserManager,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._browser_manager = browser_manager
        self._workers: List[ArenaWorker] = []
        self._cancelled = False
        self._current_run: Optional[RunResult] = None

    async def execute_run(self, request: StartRunRequest) -> RunResult:
        """Execute a complete run. Returns ``RunResult`` when done."""
        run_id = str(uuid.uuid4())[:8]
        started_at = datetime.now(timezone.utc)
        count = request.window_count
        self._cancelled = False

        await self._event_bus.publish(
            Event(
                type=EventType.RUN_STARTED,
                data={
                    "run_id": run_id,
                    "window_count": count,
                    "prompt": request.prompt[:200],
                },
            )
        )

        # Phase 1: Launch browsers
        contexts = await self._browser_manager.create_contexts(count)

        # Phase 2: Create workers
        self._workers = [
            ArenaWorker(
                worker_id=i,
                context=contexts[i],
                config=self._config,
                event_bus=self._event_bus,
            )
            for i in range(count)
        ]

        # Phase 3: Navigate all to Arena (parallel)
        nav_results = await asyncio.gather(
            *(w.navigate_to_arena() for w in self._workers),
            return_exceptions=True,
        )
        for i, result in enumerate(nav_results):
            if isinstance(result, Exception):
                logger.error("Worker %d navigation failed: %s", i, result)

        # Phase 4: Sequential submission with staggered gap
        gap = request.submission_gap_seconds or self._config.timing.submission_gap_seconds
        submitted_workers: List[ArenaWorker] = []

        for i, worker in enumerate(self._workers):
            if self._cancelled:
                break
            if worker.state_machine.state != WorkerState.READY:
                logger.warning("Worker %d not ready, skipping", i)
                continue

            try:
                await worker.submit_prompt(
                    prompt=request.prompt,
                    model=request.model,
                )
                submitted_workers.append(worker)
            except Exception as exc:
                logger.error("Worker %d submission failed: %s", i, exc)

            await self._event_bus.publish(
                Event(
                    type=EventType.RUN_PROGRESS,
                    data={
                        "total_workers": count,
                        "submitted": len(submitted_workers),
                        "phase": "submitting",
                    },
                )
            )

            # Stagger next submission (skip after last)
            if i < count - 1 and not self._cancelled:
                jittered = self._apply_jitter(gap)
                await asyncio.sleep(jittered)

        # Phase 5: Parallel polling
        poll_tasks = [
            asyncio.create_task(w.poll_for_completion())
            for w in submitted_workers
        ]

        if poll_tasks:
            await asyncio.gather(*poll_tasks, return_exceptions=True)

        # Phase 6: Collect results
        window_results: List[WindowResult] = []
        for worker in self._workers:
            result = worker.get_result()
            if result:
                result.prompt = request.prompt
                window_results.append(result)

        completed_at = datetime.now(timezone.utc)
        successful = sum(1 for r in window_results if r.success)
        failed = sum(1 for r in window_results if not r.success)

        run_result = RunResult(
            run_id=run_id,
            prompt=request.prompt,
            started_at=started_at,
            completed_at=completed_at,
            total_elapsed_seconds=(completed_at - started_at).total_seconds(),
            window_results=window_results,
            total_windows=count,
            successful_windows=successful,
            failed_windows=failed,
        )

        await self._event_bus.publish(
            Event(
                type=EventType.RUN_COMPLETE,
                data={"run_result": run_result.model_dump(mode="json")},
            )
        )

        self._current_run = run_result

        if successful == 0 and count > 0:
            raise AllWorkersFailedError(count)

        return run_result

    async def cancel(self) -> None:
        """Cancel the current run gracefully."""
        self._cancelled = True
        for worker in self._workers:
            await worker.cancel()
        await self._event_bus.publish(Event(type=EventType.RUN_CANCELLED))
        logger.info("Run cancelled")

    def _apply_jitter(self, base: float) -> float:
        jitter_range = base * self._config.timing.jitter_pct
        return base + random.uniform(-jitter_range, jitter_range)

    @property
    def last_result(self) -> Optional[RunResult]:
        return self._current_run
