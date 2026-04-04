from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, List, Optional, Tuple

from src.browser.manager import BrowserManager
from src.checkpoint.manager import CheckpointManager, RunCheckpoint
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import AllWorkersFailedError, RunCancelledError
from src.export.excel_exporter import export_to_csv, export_to_excel, export_to_json
from src.models.config import AppConfig, DisplayConfig
from src.models.messages import PromptTurn, StartRunRequest
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
    5. For each batch of prompts:
       a. Submit prompts sequentially with staggered gap
       b. Poll all workers for completion (parallel)
       c. Collect results
       d. Reset workers and re-navigate for next batch
    6. Aggregate results and emit ``RUN_COMPLETE``
    """

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        browser_manager: BrowserManager,
        checkpoint_manager: Optional[CheckpointManager] = None,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._browser_manager = browser_manager
        self._checkpoint_manager = checkpoint_manager
        self._workers: List[ArenaWorker] = []
        self._cancelled = False
        self._paused = False
        self._resume_event = asyncio.Event()
        self._resume_event.set()
        self._current_run: Optional[RunResult] = None
        self._active_request: Optional[StartRunRequest] = None
        self._active_run_id: Optional[str] = None
        self._active_started_at: Optional[datetime] = None
        self._active_all_prompts: List[str] = []
        self._active_total_batches: int = 0
        self._active_batch_size: int = 0
        self._completed_results: List[WindowResult] = []
        self._live_results: dict[tuple[int, int], WindowResult] = {}
        self._current_batch_index: Optional[int] = None
        self._submit_group_locks: dict[str, asyncio.Lock] = {}
        self._submit_group_next_ready_at: dict[str, float] = {}

    async def execute_run(
        self,
        request: StartRunRequest,
        resume_checkpoint: Optional[RunCheckpoint] = None,
    ) -> RunResult:
        """Execute a complete run. Returns ``RunResult`` when done."""
        count = request.window_count
        self._cancelled = False
        self._paused = False
        self._resume_event.set()
        system_prompt = request.system_prompt.strip()
        combine_with_first = request.combine_with_first and bool(system_prompt)
        simultaneous_start = request.simultaneous_start
        image_dicts = (
            [img.model_dump() for img in request.images]
            if request.images
            else None
        )

        # Resolve multi-turn conversation
        turn_list: Optional[List[PromptTurn]] = None
        if request.turns and len(request.turns) > 0:
            turn_list = list(request.turns)

        if resume_checkpoint:
            # Restore state from checkpoint
            run_id = resume_checkpoint.run_id
            started_at = datetime.fromisoformat(
                resume_checkpoint.original_started_at
            )
            all_prompts = resume_checkpoint.all_prompts
            start_batch_idx = resume_checkpoint.next_batch_index
            restored_results = [
                WindowResult(**wr)
                for wr in resume_checkpoint.window_results
            ]
            if combine_with_first:
                system_prompt = ""
        else:
            run_id = request.run_id or str(uuid.uuid4())[:8]
            started_at = datetime.now(timezone.utc)
            start_batch_idx = 0
            restored_results = []

            # Determine all prompts and chunk into batches
            if turn_list:
                # Multi-turn: use first turn text for batching/progress
                all_prompts = [turn_list[0].text]
                if combine_with_first:
                    turn_list[0] = PromptTurn(
                        text=f"{system_prompt}\n\n{turn_list[0].text}",
                        images=turn_list[0].images,
                    )
                    all_prompts = [turn_list[0].text]
                    system_prompt = ""
            else:
                all_prompts = request.prompts if request.prompts else [request.prompt]
                if combine_with_first:
                    all_prompts = [f"{system_prompt}\n\n{p}" for p in all_prompts]
                    system_prompt = ""  # Skip Phase 1 entirely

            # Single prompt with multiple windows: replicate across all windows
            if len(all_prompts) == 1 and count > 1:
                all_prompts = all_prompts * count

        batches: List[List[str]] = [
            all_prompts[i : i + count]
            for i in range(0, len(all_prompts), count)
        ]
        total_batches = len(batches)
        turn_count = len(turn_list) if turn_list and len(turn_list) > 0 else 1
        expected_result_slots = len(all_prompts) * turn_count
        self._active_request = request
        self._active_run_id = run_id
        self._active_started_at = started_at
        self._active_all_prompts = list(all_prompts)
        self._active_total_batches = total_batches
        self._active_batch_size = count
        self._completed_results = list(restored_results)
        self._live_results = {}
        self._current_batch_index = None
        self._submit_group_locks = {}
        self._submit_group_next_ready_at = {}
        self._refresh_current_run()

        await self._publish(
            Event(
                type=EventType.RUN_STARTED,
                data={
                    "run_id": run_id,
                    "window_count": count,
                    "prompt": all_prompts[0][:200],
                    "total_prompts": len(all_prompts),
                    "total_result_slots": expected_result_slots,
                    "total_batches": total_batches,
                    "resumed_from_batch": start_batch_idx if resume_checkpoint else None,
                },
            )
        )

        # Build display config from request overrides (fall back to YAML defaults)
        base = self._config.display
        display_override = DisplayConfig(
            monitor_count=request.monitor_count or base.monitor_count,
            monitor_width=request.monitor_width or base.monitor_width,
            monitor_height=request.monitor_height or base.monitor_height,
            taskbar_height=(
                request.taskbar_height
                if request.taskbar_height is not None
                else base.taskbar_height
            ),
            margin=(
                request.margin if request.margin is not None else base.margin
            ),
            border_offset=(
                request.border_offset
                if request.border_offset is not None
                else base.border_offset
            ),
        )

        # Merge proxy list: UI overrides take precedence, fall back to YAML config
        proxy_list = None
        if request.proxies:
            proxy_list = request.proxies
        elif self._config.browser.proxies:
            proxy_list = [
                p.model_dump(exclude_none=True)
                for p in self._config.browser.proxies
            ]

        # Phase 1: Launch browsers
        contexts = await self._browser_manager.create_contexts(
            count,
            display_override=display_override,
            headless=request.headless,
            minimized=request.minimized,
            incognito=request.incognito,
            proxies=proxy_list,
            proxy_on_challenge=request.proxy_on_challenge,
            windows_per_proxy=request.windows_per_proxy,
            zoom_pct=request.zoom_pct,
            run_id=run_id,
            layout_group_id=request.layout_group_id,
            total_windows=request.total_windows,
            tile_offset=request.tile_offset or 0,
        )

        # Create run_id-scoped callbacks for workers
        async def _recreator(index: int) -> object:
            return await self._browser_manager.recreate_context(index, run_id=run_id)

        def _proxy_getter(index: int) -> object:
            return self._browser_manager.get_context_proxy(index, run_id=run_id)

        def _proxy_reporter(index: int) -> None:
            self._browser_manager.report_proxy_success(index, run_id=run_id)

        def _proxy_failure_reporter(index: int, reason: str) -> None:
            self._browser_manager.report_proxy_failure(
                index,
                run_id=run_id,
                reason=reason,
            )

        # Phase 2: Create workers
        self._workers = [
            ArenaWorker(
                worker_id=i,
                context=contexts[i],
                config=self._config,
                event_bus=self._event_bus,
                context_recreator=_recreator,
                proxy_getter=_proxy_getter,
                proxy_success_reporter=_proxy_reporter,
                proxy_failure_reporter=_proxy_failure_reporter,
                run_id=run_id,
            )
            for i in range(count)
        ]

        initial_navigation_tasks: Optional[List[asyncio.Task[None]]] = None
        if simultaneous_start:
            initial_navigation_tasks = [
                asyncio.create_task(
                    w.navigate_to_arena(
                        clear_cookies=request.clear_cookies,
                        zoom_pct=request.zoom_pct,
                        pause_event=self._resume_event,
                    )
                )
                for w in self._workers
            ]
            if start_batch_idx > 0:
                nav_results = await asyncio.gather(
                    *initial_navigation_tasks,
                    return_exceptions=True,
                )
                for i, result in enumerate(nav_results):
                    if isinstance(result, BaseException):
                        logger.error("Worker %d navigation failed: %s", i, result)
                initial_navigation_tasks = None
        else:
            # Phase 3: Navigate all to Arena (parallel)
            nav_results = await asyncio.gather(
                *(w.navigate_to_arena(
                    clear_cookies=request.clear_cookies,
                    zoom_pct=request.zoom_pct,
                    pause_event=self._resume_event,
                ) for w in self._workers),
                return_exceptions=True,
            )
            for i, result in enumerate(nav_results):
                if isinstance(result, BaseException):
                    logger.error("Worker %d navigation failed: %s", i, result)

        if request.clear_cookies:
            await self._publish(
                Event(
                    type=EventType.TOAST,
                    data={
                        "message": "Cookies cleared successfully",
                        "level": "success",
                    },
                )
            )

        await self._wait_if_paused()

        if self._cancelled:
            return await self._finish_cancelled(
                run_id, request, started_at, count, all_prompts, total_batches, []
            )

        # Phase 4-6: Batch loop
        gap = request.submission_gap_seconds or self._config.timing.submission_gap_seconds
        all_window_results: List[WindowResult] = list(restored_results)

        if combine_with_first:
            await self._publish(
                Event(
                    type=EventType.LOG,
                    data={
                        "level": "info",
                        "text": "Combined mode: system prompt merged into each prompt",
                    },
                )
            )

        for batch_idx, batch_prompts in enumerate(batches):
            if batch_idx < start_batch_idx:
                continue  # Skip already-completed batches

            self._current_batch_index = batch_idx
            self._live_results = {}
            self._refresh_current_run()
            batch_navigation_tasks = (
                initial_navigation_tasks if batch_idx == 0 else None
            )
            await self._wait_if_paused()
            if self._cancelled:
                break

            # Log batch start
            await self._publish(
                Event(
                    type=EventType.LOG,
                    data={
                        "level": "info",
                        "text": (
                            f"Batch {batch_idx + 1}/{total_batches} starting "
                            f"({len(batch_prompts)} prompt(s))"
                        ),
                    },
                )
            )

            # Re-navigate for subsequent batches
            if batch_idx > 0:
                await self._wait_if_paused()
                for worker in self._workers:
                    await worker.state_machine.reset()
                if simultaneous_start:
                    batch_navigation_tasks = [
                        asyncio.create_task(
                            w.navigate_to_arena(
                                zoom_pct=request.zoom_pct,
                                pause_event=self._resume_event,
                            )
                        )
                        for w in self._workers
                    ]
                else:
                    nav_results = await asyncio.gather(
                        *(w.navigate_to_arena(
                            zoom_pct=request.zoom_pct,
                            pause_event=self._resume_event,
                        )
                          for w in self._workers),
                        return_exceptions=True,
                    )
                    for i, result in enumerate(nav_results):
                        if isinstance(result, BaseException):
                            logger.error(
                                "Worker %d re-navigation failed (batch %d): %s",
                                i, batch_idx, result,
                            )

            if self._cancelled:
                break

            # Submit this batch's prompts (sequential with gap)
            workers_in_batch = min(len(batch_prompts), count)
            if simultaneous_start:
                batch_results = await self._run_simultaneous_batch(
                    batch_idx=batch_idx,
                    total_batches=total_batches,
                    batch_prompts=batch_prompts,
                    request=request,
                    system_prompt=system_prompt,
                    turn_list=turn_list,
                    image_dicts=image_dicts,
                    gap=gap,
                    expected_result_slots=expected_result_slots,
                    completed_before_batch=len(all_window_results),
                    navigation_tasks=batch_navigation_tasks,
                )
                all_window_results.extend(batch_results)
                self._completed_results = list(all_window_results)
                self._live_results = {}
                self._current_batch_index = None

                self._refresh_current_run()
                self._save_incremental(self._current_run)

                if self._checkpoint_manager:
                    self._save_checkpoint(
                        run_id, request, all_prompts, batch_idx,
                        count, total_batches, all_window_results, started_at,
                    )

                await self._publish(
                    Event(
                        type=EventType.RUN_PROGRESS,
                        data={
                            "total_workers": expected_result_slots,
                            "submitted": len(all_window_results),
                            "phase": "batch_complete",
                            "batch": batch_idx + 1,
                            "total_batches": total_batches,
                        },
                    )
                )

                await self._publish(
                    Event(
                        type=EventType.LOG,
                        data={
                            "level": "info",
                            "text": (
                                f"Batch {batch_idx + 1}/{total_batches} complete "
                                f"â€” {len(all_window_results)}/{expected_result_slots} "
                                f"{'responses' if turn_count > 1 else 'prompts'} done"
                            ),
                        },
                    )
                )
                continue

            batch_results: List[WindowResult] = []
            early_failures: List[Tuple[int, ArenaWorker, str]] = []
            submitted: List[
                Tuple[int, ArenaWorker, str, Optional[Tuple[str, str]]]
            ] = []
            ready_for_actual: dict[int, Tuple[str, str]] = {}

            if system_prompt:
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        data={
                            "level": "info",
                            "text": (
                                f"Batch {batch_idx + 1}/{total_batches}: "
                                "sending system prompt first"
                            ),
                        },
                    )
                )

                submitted_system: List[Tuple[int, ArenaWorker]] = []
                prepared_system: List[Tuple[int, ArenaWorker, str]] = []
                pending_system_prepare: List[
                    Tuple[int, ArenaWorker, str, asyncio.Task[None]]
                ] = []
                for i in range(workers_in_batch):
                    worker = self._workers[i]
                    prompt = batch_prompts[i]

                    await self._wait_if_paused()
                    if self._cancelled:
                        break
                    if worker.state_machine.state != WorkerState.READY:
                        logger.warning(
                            "Worker %d not ready for system prompt (batch %d)",
                            i,
                            batch_idx,
                        )
                        early_failures.append((i, worker, prompt))
                        continue

                    if simultaneous_start:
                        pending_system_prepare.append(
                            (
                                i,
                                worker,
                                prompt,
                                asyncio.create_task(
                                    worker.prepare_prompt(
                                        prompt=system_prompt,
                                        model_a=request.model_a,
                                        model_b=request.model_b,
                                        mark_started=False,
                                        pause_event=self._resume_event,
                                    )
                                ),
                            )
                        )
                    else:
                        try:
                            await worker.submit_prompt(
                                prompt=system_prompt,
                                model_a=request.model_a,
                                model_b=request.model_b,
                                pause_event=self._resume_event,
                            )
                            submitted_system.append((i, worker))
                        except Exception as exc:
                            logger.error(
                                "Worker %d system prompt submission failed (batch %d): %s",
                                i,
                                batch_idx,
                                exc,
                            )
                            early_failures.append((i, worker, prompt))

                        if i < workers_in_batch - 1 and not self._cancelled:
                            jittered = self._apply_jitter(gap)
                            await self._sleep_with_pause(jittered)

                if simultaneous_start and pending_system_prepare:
                    await self._publish(
                        Event(
                            type=EventType.LOG,
                            data={
                                "level": "info",
                                "text": (
                                    f"Batch {batch_idx + 1}/{total_batches}: "
                                    "preparing system prompt in all windows, "
                                    "then submitting one by one"
                                ),
                            },
                        )
                    )

                    system_results = await asyncio.gather(
                        *(task for _, _, _, task in pending_system_prepare),
                        return_exceptions=True,
                    )
                    for (
                        worker_idx,
                        worker,
                        prompt,
                        _,
                    ), result in zip(pending_system_prepare, system_results):
                        if isinstance(result, BaseException):
                            logger.error(
                                "Worker %d system prompt preparation failed (batch %d): %s",
                                worker_idx,
                                batch_idx,
                                result,
                            )
                            early_failures.append((worker_idx, worker, prompt))
                        else:
                            prepared_system.append((worker_idx, worker, prompt))

                    for prepared_idx, (
                        worker_idx,
                        worker,
                        prompt,
                    ) in enumerate(prepared_system):
                        await self._wait_if_paused()
                        if self._cancelled:
                            break

                        try:
                            await worker.submit_prepared_prompt(
                                pause_event=self._resume_event,
                            )
                            submitted_system.append((worker_idx, worker))
                        except Exception as exc:
                            logger.error(
                                "Worker %d system prompt submission failed (batch %d): %s",
                                worker_idx,
                                batch_idx,
                                exc,
                            )
                            early_failures.append((worker_idx, worker, prompt))

                        if (
                            prepared_idx < len(prepared_system) - 1
                            and not self._cancelled
                        ):
                            jittered = self._apply_jitter(gap)
                            await self._sleep_with_pause(jittered)

                if self._cancelled:
                    break

                system_poll_tasks = [
                    asyncio.create_task(
                        worker.poll_for_completion(
                            pause_event=self._resume_event,
                        )
                    )
                    for _, worker in submitted_system
                ]
                if system_poll_tasks:
                    await asyncio.gather(
                        *system_poll_tasks, return_exceptions=True
                    )

                await self._wait_if_paused()

                if self._cancelled:
                    break

                for worker_idx, worker in submitted_system:
                    await self._wait_if_paused()
                    system_result = worker.get_result()
                    actual_prompt = batch_prompts[worker_idx]
                    if not system_result or not system_result.success:
                        early_failures.append(
                            (worker_idx, worker, actual_prompt)
                        )
                        continue

                    try:
                        ready_for_actual[worker_idx] = (
                            await worker.prepare_for_followup_prompt()
                        )
                    except Exception as exc:
                        logger.error(
                            "Worker %d follow-up preparation failed (batch %d): %s",
                            worker_idx, batch_idx, exc,
                        )
                        early_failures.append(
                            (worker_idx, worker, actual_prompt)
                        )

                await self._publish(
                    Event(
                        type=EventType.LOG,
                        data={
                            "level": "info",
                            "text": (
                                f"Batch {batch_idx + 1}/{total_batches}: "
                                "system prompt phase complete"
                            ),
                        },
                    )
                )

            if simultaneous_start:
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        data={
                            "level": "info",
                            "text": (
                                f"Batch {batch_idx + 1}/{total_batches}: "
                                "preparing all windows in parallel, then "
                                "staggering submits with the configured gap"
                            ),
                        },
                    )
                )

                pending_prepare: List[
                    Tuple[
                        int,
                        ArenaWorker,
                        str,
                        Optional[Tuple[str, str]],
                        asyncio.Task[None],
                    ]
                ] = []
                for i in range(workers_in_batch):
                    worker = self._workers[i]
                    prompt = batch_prompts[i]
                    baseline_responses: Optional[Tuple[str, str]] = None

                    await self._wait_if_paused()
                    if self._cancelled:
                        break
                    if system_prompt:
                        baseline_responses = ready_for_actual.get(i)
                        if baseline_responses is None:
                            continue
                    if worker.state_machine.state != WorkerState.READY:
                        logger.warning(
                            "Worker %d not ready (batch %d), marking for recovery",
                            i, batch_idx,
                        )
                        early_failures.append((i, worker, prompt))
                        continue

                    pending_prepare.append(
                        (
                            i,
                            worker,
                            prompt,
                            baseline_responses,
                            asyncio.create_task(
                                worker.prepare_prompt(
                                    prompt=prompt,
                                    model_a=None if system_prompt else request.model_a,
                                    model_b=None if system_prompt else request.model_b,
                                    mark_started=False,
                                    pause_event=self._resume_event,
                                    images=image_dicts,
                                )
                            ),
                        )
                    )

                prepared_workers: List[
                    Tuple[int, ArenaWorker, str, Optional[Tuple[str, str]]]
                ] = []
                if pending_prepare:
                    submission_results = await asyncio.gather(
                        *(task for _, _, _, _, task in pending_prepare),
                        return_exceptions=True,
                    )
                    for (
                        worker_idx,
                        worker,
                        prompt,
                        baseline_responses,
                        _,
                    ), result in zip(pending_prepare, submission_results):
                        if isinstance(result, BaseException):
                            logger.error(
                                "Worker %d preparation failed (batch %d): %s",
                                worker_idx, batch_idx, result,
                            )
                            early_failures.append((worker_idx, worker, prompt))
                        else:
                            prepared_workers.append(
                                (worker_idx, worker, prompt, baseline_responses)
                            )

                for prepared_idx, (
                    worker_idx,
                    worker,
                    prompt,
                    baseline_responses,
                ) in enumerate(prepared_workers):
                    await self._wait_if_paused()
                    if self._cancelled:
                        break

                    try:
                        await worker.submit_prepared_prompt(
                            pause_event=self._resume_event,
                        )
                        submitted.append(
                            (worker_idx, worker, prompt, baseline_responses)
                        )
                    except Exception as exc:
                        logger.error(
                            "Worker %d submission failed (batch %d): %s",
                            worker_idx, batch_idx, exc,
                        )
                        early_failures.append((worker_idx, worker, prompt))

                    completed_prompts = (
                        len(all_window_results)
                        + len(batch_results)
                        + len(submitted)
                    )
                    await self._publish(
                        Event(
                            type=EventType.RUN_PROGRESS,
                            data={
                                "total_workers": expected_result_slots,
                                "submitted": completed_prompts,
                                "phase": "submitting",
                                "batch": batch_idx + 1,
                                "total_batches": total_batches,
                            },
                        )
                    )

                    if (
                        prepared_idx < len(prepared_workers) - 1
                        and not self._cancelled
                    ):
                        jittered = self._apply_jitter(gap)
                        await self._sleep_with_pause(jittered)
            else:
                for i in range(workers_in_batch):
                    worker = self._workers[i]
                    prompt = batch_prompts[i]
                    baseline_responses: Optional[Tuple[str, str]] = None

                    await self._wait_if_paused()
                    if self._cancelled:
                        break
                    if system_prompt:
                        baseline_responses = ready_for_actual.get(i)
                        if baseline_responses is None:
                            continue
                    if worker.state_machine.state != WorkerState.READY:
                        logger.warning(
                            "Worker %d not ready (batch %d), marking for recovery",
                            i, batch_idx,
                        )
                        early_failures.append((i, worker, prompt))
                        continue

                    try:
                        await worker.submit_prompt(
                            prompt=prompt,
                            model_a=None if system_prompt else request.model_a,
                            model_b=None if system_prompt else request.model_b,
                            pause_event=self._resume_event,
                            images=image_dicts,
                        )
                        submitted.append(
                            (i, worker, prompt, baseline_responses)
                        )
                    except Exception as exc:
                        logger.error(
                            "Worker %d submission failed (batch %d): %s",
                            i, batch_idx, exc,
                        )
                        early_failures.append((i, worker, prompt))

                    # Emit progress
                    completed_prompts = (
                        len(all_window_results)
                        + len(batch_results)
                        + len(submitted)
                    )
                    await self._publish(
                        Event(
                            type=EventType.RUN_PROGRESS,
                            data={
                                "total_workers": expected_result_slots,
                                "submitted": completed_prompts,
                                "phase": "submitting",
                                "batch": batch_idx + 1,
                                "total_batches": total_batches,
                            },
                        )
                    )

                    # Stagger next submission (skip after last in batch)
                    if i < workers_in_batch - 1 and not self._cancelled:
                        jittered = self._apply_jitter(gap)
                        await self._sleep_with_pause(jittered)

            if self._cancelled:
                break

            # Poll submitted workers
            await self._wait_if_paused()
            poll_tasks = [
                asyncio.create_task(
                    self._poll_worker_for_batch(
                        worker=worker,
                        prompt=prompt,
                        batch_idx=batch_idx,
                        retain=request.retain_output,
                        baseline_responses=baseline_responses,
                    )
                )
                for _, worker, prompt, baseline_responses in submitted
            ]
            if poll_tasks:
                await asyncio.gather(*poll_tasks, return_exceptions=True)

            await self._wait_if_paused()

            if self._cancelled:
                break

            # Collect batch results — separate successes from failures
            retain = request.retain_output
            poll_failures: List[Tuple[int, ArenaWorker, str]] = []
            for worker_idx, worker, prompt, _ in submitted:
                result = self._live_results.get((batch_idx, worker_idx))
                if result and result.success:
                    batch_results.append(result)
                else:
                    poll_failures.append((worker_idx, worker, prompt))

            # Attempt recovery for all failed workers (early + polling failures)
            all_failures = early_failures + poll_failures
            if all_failures and not self._cancelled:
                await self._wait_if_paused()
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        data={
                            "level": "warning",
                            "text": (
                                f"Batch {batch_idx + 1}: {len(all_failures)} "
                                "worker(s) failed — attempting recovery"
                            ),
                        },
                    )
                )
                recovery_tasks = [
                    self._attempt_recovery(
                        worker=worker,
                        worker_idx=widx,
                        prompt=prompt,
                        batch_idx=batch_idx,
                        request=request,
                        system_prompt=system_prompt,
                        image_dicts=image_dicts,
                    )
                    for widx, worker, prompt in all_failures
                ]
                recovery_results = await asyncio.gather(
                    *recovery_tasks, return_exceptions=True
                )
                for (widx, worker, prompt), recovery_out in zip(
                    all_failures, recovery_results
                ):
                    if isinstance(recovery_out, BaseException):
                        if isinstance(recovery_out, asyncio.CancelledError):
                            if self._cancelled:
                                error_text = (
                                    "Recovery cancelled (run cancelled)"
                                )
                            else:
                                error_text = (
                                    "Recovery cancelled unexpectedly"
                                )
                        else:
                            error_text = (
                                f"Recovery raised exception: {recovery_out}"
                            )
                        failed_result = self._failed_result(
                            worker_id=widx,
                            prompt=prompt,
                            batch_idx=batch_idx,
                            error=error_text,
                        )
                        await self._publish_failed_worker_result(
                            worker, failed_result
                        )
                        batch_results.append(failed_result)
                    else:
                        if recovery_out.success:
                            if retain == "model_a":
                                recovery_out.model_b_name = None
                                recovery_out.model_b_response = None
                                recovery_out.model_b_response_html = None
                            elif retain == "model_b":
                                recovery_out.model_a_name = None
                                recovery_out.model_a_response = None
                                recovery_out.model_a_response_html = None
                        batch_results.append(recovery_out)
            elif all_failures:
                # Cancelled — record failures without recovery
                for widx, _, prompt in all_failures:
                    batch_results.append(
                        self._failed_result(
                            worker_id=widx,
                            prompt=prompt,
                            batch_idx=batch_idx,
                            error="Worker failed and recovery skipped (run cancelled)",
                        )
                    )

            batch_results.sort(key=lambda item: item.worker_id)
            all_window_results.extend(batch_results)

            # ── Multi-turn: subsequent turns in the same conversation ──
            if turn_list and len(turn_list) > 1 and not self._cancelled:
                # Determine surviving workers from turn 0
                surviving: dict[int, ArenaWorker] = {}
                for r in batch_results:
                    if r.success and r.worker_id < len(self._workers):
                        surviving[r.worker_id] = self._workers[r.worker_id]

                for turn_idx in range(1, len(turn_list)):
                    if not surviving or self._cancelled:
                        break

                    turn = turn_list[turn_idx]
                    turn_image_dicts = (
                        [img.model_dump() for img in turn.images]
                        if turn.images
                        else None
                    )

                    await self._publish(
                        Event(
                            type=EventType.LOG,
                            data={
                                "level": "info",
                                "text": (
                                    f"Batch {batch_idx + 1}/{total_batches}: "
                                    f"Turn {turn_idx + 1}/{len(turn_list)}"
                                ),
                            },
                        )
                    )

                    # Prepare all surviving workers for follow-up
                    followup_baselines: dict[int, Tuple[str, str]] = {}
                    for wid in list(surviving.keys()):
                        worker = surviving[wid]
                        try:
                            followup_baselines[wid] = (
                                await worker.prepare_for_followup_prompt()
                            )
                        except Exception as exc:
                            logger.error(
                                "Worker %d follow-up prep failed (turn %d): %s",
                                wid, turn_idx, exc,
                            )
                            failed = self._failed_result(
                                worker_id=wid,
                                prompt=turn.text,
                                batch_idx=batch_idx,
                                error=f"Follow-up prep failed: {exc}",
                                turn_index=turn_idx,
                            )
                            await self._publish_failed_worker_result(
                                worker, failed
                            )
                            all_window_results.append(failed)
                            del surviving[wid]

                    if not surviving or self._cancelled:
                        break

                    # Submit turn to all surviving workers (sequential)
                    turn_submitted: List[
                        Tuple[int, ArenaWorker, Optional[Tuple[str, str]]]
                    ] = []
                    submit_idx = 0
                    for wid in list(surviving.keys()):
                        await self._wait_if_paused()
                        if self._cancelled:
                            break

                        worker = surviving[wid]
                        try:
                            await worker.submit_prompt(
                                prompt=turn.text,
                                model_a=None,
                                model_b=None,
                                pause_event=self._resume_event,
                                images=turn_image_dicts,
                            )
                            turn_submitted.append(
                                (wid, worker, followup_baselines.get(wid))
                            )
                        except Exception as exc:
                            logger.error(
                                "Worker %d turn %d submission failed: %s",
                                wid, turn_idx, exc,
                            )
                            failed = self._failed_result(
                                worker_id=wid,
                                prompt=turn.text,
                                batch_idx=batch_idx,
                                error=f"Turn {turn_idx} submission failed: {exc}",
                                turn_index=turn_idx,
                            )
                            await self._publish_failed_worker_result(
                                worker, failed
                            )
                            all_window_results.append(failed)
                            surviving.pop(wid, None)

                        submit_idx += 1
                        if (
                            submit_idx < len(surviving)
                            and not self._cancelled
                        ):
                            jittered = self._apply_jitter(gap)
                            await self._sleep_with_pause(jittered)

                    if not turn_submitted or self._cancelled:
                        # Record failures for remaining turns
                        for remaining_turn in range(
                            turn_idx if not turn_submitted else turn_idx + 1,
                            len(turn_list),
                        ):
                            for wid in surviving:
                                failed = self._failed_result(
                                    worker_id=wid,
                                    prompt=turn_list[remaining_turn].text,
                                    batch_idx=batch_idx,
                                    error="Skipped (earlier turn failed or cancelled)",
                                    turn_index=remaining_turn,
                                )
                                all_window_results.append(failed)
                        break

                    # Poll all submitted workers for this turn
                    turn_poll_tasks = [
                        asyncio.create_task(
                            self._poll_worker_for_batch(
                                worker=worker,
                                prompt=turn.text,
                                batch_idx=batch_idx,
                                retain=request.retain_output,
                                baseline_responses=baseline,
                                turn_index=turn_idx,
                            )
                        )
                        for wid, worker, baseline in turn_submitted
                    ]
                    if turn_poll_tasks:
                        await asyncio.gather(
                            *turn_poll_tasks, return_exceptions=True
                        )

                    await self._wait_if_paused()

                    # Collect turn results
                    turn_results: List[WindowResult] = []
                    for wid, worker, _ in turn_submitted:
                        result = self._live_results.get((batch_idx, wid))
                        if result:
                            turn_results.append(result)
                            if not result.success:
                                surviving.pop(wid, None)
                        else:
                            surviving.pop(wid, None)
                            failed = self._failed_result(
                                worker_id=wid,
                                prompt=turn.text,
                                batch_idx=batch_idx,
                                error=f"Turn {turn_idx} polling returned no result",
                                turn_index=turn_idx,
                            )
                            turn_results.append(failed)

                    all_window_results.extend(turn_results)

                    # Update progress
                    self._completed_results = list(all_window_results)
                    self._live_results = {}
                    self._refresh_current_run()

                    await self._publish(
                        Event(
                            type=EventType.RUN_PROGRESS,
                            data={
                                "total_workers": expected_result_slots,
                                "submitted": len(all_window_results),
                                "phase": "turn_complete",
                                "batch": batch_idx + 1,
                                "total_batches": total_batches,
                                "turn": turn_idx + 1,
                                "total_turns": len(turn_list),
                            },
                        )
                    )

            self._completed_results = list(all_window_results)
            self._live_results = {}
            self._current_batch_index = None

            # Save incremental results after each batch
            self._refresh_current_run()
            self._save_incremental(self._current_run)

            # Save checkpoint for resume capability
            if self._checkpoint_manager:
                self._save_checkpoint(
                    run_id, request, all_prompts, batch_idx,
                    count, total_batches, all_window_results, started_at,
                )

            # Emit batch complete progress
            await self._publish(
                Event(
                    type=EventType.RUN_PROGRESS,
                    data={
                        "total_workers": expected_result_slots,
                        "submitted": len(all_window_results),
                        "phase": "batch_complete",
                        "batch": batch_idx + 1,
                        "total_batches": total_batches,
                    },
                )
            )

            await self._publish(
                Event(
                    type=EventType.LOG,
                    data={
                        "level": "info",
                        "text": (
                            f"Batch {batch_idx + 1}/{total_batches} complete "
                            f"— {len(all_window_results)}/{expected_result_slots} "
                            f"{'responses' if turn_count > 1 else 'prompts'} done"
                        ),
                    },
                )
            )

        # Cancelled mid-batch — return partial results
        if self._cancelled:
            return await self._finish_cancelled(
                run_id, request, started_at, count,
                all_prompts, total_batches, all_window_results,
            )

        # Final result
        run_result = self._build_result(
            run_id, all_prompts, total_batches,
            started_at, all_window_results,
        )
        run_result.completed_at = datetime.now(timezone.utc)
        run_result.total_elapsed_seconds = (
            run_result.completed_at - started_at
        ).total_seconds()
        self._current_run = run_result
        self._completed_results = list(all_window_results)
        self._live_results = {}
        self._current_batch_index = None
        self._save_incremental(run_result)

        # Mark checkpoint as completed
        if self._checkpoint_manager:
            self._checkpoint_manager.mark_completed(run_id)

        await self._publish(
            Event(
                type=EventType.RUN_COMPLETE,
                data={"run_result": run_result.model_dump(mode="json")},
            )
        )

        # Close browser windows after successful completion
        await self._browser_manager.close_contexts(run_id=self._active_run_id)

        if run_result.successful_windows == 0 and len(all_prompts) > 0:
            raise AllWorkersFailedError(len(all_prompts))

        return run_result

    @staticmethod
    def _apply_retain_output(result: WindowResult, retain: str) -> None:
        if not result.success:
            return
        if retain == "model_a":
            result.model_b_name = None
            result.model_b_response = None
            result.model_b_response_html = None
        elif retain == "model_b":
            result.model_a_name = None
            result.model_a_response = None
            result.model_a_response_html = None

    def _submission_group_key(self, worker_idx: int) -> str:
        proxy = self._browser_manager.get_context_proxy(
            worker_idx,
            run_id=self._active_run_id,
        )
        return proxy or "bare-ip"

    async def _await_submit_slot(self, worker_idx: int, gap: float) -> None:
        group_key = self._submission_group_key(worker_idx)
        lock = self._submit_group_locks.setdefault(group_key, asyncio.Lock())
        async with lock:
            await self._wait_if_paused()
            if self._cancelled:
                return

            now = asyncio.get_running_loop().time()
            ready_at = self._submit_group_next_ready_at.get(group_key, now)
            wait_for = max(0.0, ready_at - now)
            if wait_for > 0:
                await self._sleep_with_pause(wait_for)
                if self._cancelled:
                    return

            self._submit_group_next_ready_at[group_key] = (
                asyncio.get_running_loop().time() + self._apply_jitter(gap)
            )

    async def _drain_navigation_tasks(
        self,
        navigation_tasks: Optional[List[asyncio.Task[None]]],
        used_count: int,
    ) -> None:
        if not navigation_tasks:
            return

        for task in navigation_tasks[used_count:]:
            if not task.done():
                task.cancel()

        await asyncio.gather(*navigation_tasks, return_exceptions=True)

    async def _await_worker_ready(
        self,
        worker_idx: int,
        worker: ArenaWorker,
        batch_idx: int,
        navigation_task: Optional[asyncio.Task[None]] = None,
    ) -> None:
        if navigation_task is not None:
            try:
                await navigation_task
            except Exception as exc:
                logger.error(
                    "Worker %d navigation failed (batch %d): %s",
                    worker_idx,
                    batch_idx,
                    exc,
                )
                raise

        if worker.state_machine.state != WorkerState.READY:
            raise RuntimeError(
                f"Worker {worker_idx} not ready for batch {batch_idx + 1} "
                f"(state={worker.state_machine.state.value})"
            )

    async def _prepare_and_submit_prompt(
        self,
        worker: ArenaWorker,
        worker_idx: int,
        prompt: str,
        gap: float,
        model_a: Optional[str] = None,
        model_b: Optional[str] = None,
        images: Optional[list[dict]] = None,
        on_submit: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        await worker.prepare_prompt(
            prompt=prompt,
            model_a=model_a,
            model_b=model_b,
            mark_started=False,
            pause_event=self._resume_event,
            images=images,
        )
        await self._await_submit_slot(worker_idx, gap)
        await worker.submit_prepared_prompt(
            pause_event=self._resume_event,
        )
        if on_submit is not None:
            await on_submit()

    async def _prepare_submit_and_poll_prompt(
        self,
        worker: ArenaWorker,
        worker_idx: int,
        prompt: str,
        batch_idx: int,
        gap: float,
        retain: str,
        baseline_responses: Optional[Tuple[str, str]] = None,
        turn_index: int = 0,
        model_a: Optional[str] = None,
        model_b: Optional[str] = None,
        images: Optional[list[dict]] = None,
        on_submit: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> WindowResult:
        await self._prepare_and_submit_prompt(
            worker=worker,
            worker_idx=worker_idx,
            prompt=prompt,
            gap=gap,
            model_a=model_a,
            model_b=model_b,
            images=images,
            on_submit=on_submit,
        )
        return await self._poll_worker_for_batch(
            worker=worker,
            prompt=prompt,
            batch_idx=batch_idx,
            retain=retain,
            baseline_responses=baseline_responses,
            turn_index=turn_index,
        )

    @staticmethod
    def _build_skipped_turn_results(
        worker_id: int,
        batch_idx: int,
        turns: List[PromptTurn],
        start_turn_index: int,
        error: str,
    ) -> List[WindowResult]:
        return [
            RunOrchestrator._failed_result(
                worker_id=worker_id,
                prompt=turns[turn_idx].text,
                batch_idx=batch_idx,
                error=error,
                turn_index=turn_idx,
            )
            for turn_idx in range(start_turn_index, len(turns))
        ]

    async def _run_simultaneous_worker_batch(
        self,
        worker_idx: int,
        prompt: str,
        batch_idx: int,
        request: StartRunRequest,
        system_prompt: str,
        turn_list: Optional[List[PromptTurn]],
        image_dicts: Optional[list[dict]],
        gap: float,
        on_submit: Callable[[], Awaitable[None]],
        navigation_task: Optional[asyncio.Task[None]] = None,
    ) -> List[WindowResult]:
        worker = self._workers[worker_idx]
        results: List[WindowResult] = []
        total_turns = len(turn_list) if turn_list else 1

        first_result: Optional[WindowResult] = None
        try:
            await self._wait_if_paused()
            await self._await_worker_ready(
                worker_idx=worker_idx,
                worker=worker,
                batch_idx=batch_idx,
                navigation_task=navigation_task,
            )

            baseline_responses: Optional[Tuple[str, str]] = None
            if system_prompt:
                await self._prepare_and_submit_prompt(
                    worker=worker,
                    worker_idx=worker_idx,
                    prompt=system_prompt,
                    gap=gap,
                    model_a=request.model_a,
                    model_b=request.model_b,
                )
                system_result = await worker.poll_for_completion(
                    pause_event=self._resume_event,
                )
                if not system_result.success:
                    raise RuntimeError(
                        system_result.error or "System prompt phase failed"
                    )
                baseline_responses = await worker.prepare_for_followup_prompt()

            first_result = await self._prepare_submit_and_poll_prompt(
                worker=worker,
                worker_idx=worker_idx,
                prompt=prompt,
                batch_idx=batch_idx,
                gap=gap,
                retain=request.retain_output,
                baseline_responses=baseline_responses,
                turn_index=0,
                model_a=None if system_prompt else request.model_a,
                model_b=None if system_prompt else request.model_b,
                images=image_dicts,
                on_submit=on_submit,
            )
            if not first_result.success:
                raise RuntimeError(
                    first_result.error or "Prompt polling failed"
                )
        except Exception as exc:
            logger.error(
                "Worker %d initial prompt pipeline failed (batch %d): %s",
                worker_idx,
                batch_idx,
                exc,
            )
            first_result = await self._attempt_recovery(
                worker=worker,
                worker_idx=worker_idx,
                prompt=prompt,
                batch_idx=batch_idx,
                request=request,
                system_prompt=system_prompt,
                image_dicts=image_dicts,
            )
            self._apply_retain_output(first_result, request.retain_output)

        results.append(first_result)
        if not first_result.success:
            if turn_list and total_turns > 1:
                results.extend(
                    self._build_skipped_turn_results(
                        worker_id=worker_idx,
                        batch_idx=batch_idx,
                        turns=turn_list,
                        start_turn_index=1,
                        error="Skipped (earlier turn failed or cancelled)",
                    )
                )
            return results

        if not turn_list or total_turns <= 1:
            return results

        for turn_idx in range(1, total_turns):
            turn = turn_list[turn_idx]
            turn_image_dicts = (
                [img.model_dump() for img in turn.images]
                if turn.images
                else None
            )

            try:
                baseline = await worker.prepare_for_followup_prompt()
                turn_result = await self._prepare_submit_and_poll_prompt(
                    worker=worker,
                    worker_idx=worker_idx,
                    prompt=turn.text,
                    batch_idx=batch_idx,
                    gap=gap,
                    retain=request.retain_output,
                    baseline_responses=baseline,
                    turn_index=turn_idx,
                    images=turn_image_dicts,
                    on_submit=on_submit,
                )
            except Exception as exc:
                logger.error(
                    "Worker %d turn %d failed (batch %d): %s",
                    worker_idx,
                    turn_idx,
                    batch_idx,
                    exc,
                )
                turn_result = self._failed_result(
                    worker_id=worker_idx,
                    prompt=turn.text,
                    batch_idx=batch_idx,
                    error=f"Turn {turn_idx} failed: {exc}",
                    turn_index=turn_idx,
                )
                await self._publish_failed_worker_result(worker, turn_result)

            results.append(turn_result)
            if not turn_result.success:
                results.extend(
                    self._build_skipped_turn_results(
                        worker_id=worker_idx,
                        batch_idx=batch_idx,
                        turns=turn_list,
                        start_turn_index=turn_idx + 1,
                        error="Skipped (earlier turn failed or cancelled)",
                    )
                )
                break

        return results

    async def _run_simultaneous_batch(
        self,
        batch_idx: int,
        total_batches: int,
        batch_prompts: List[str],
        request: StartRunRequest,
        system_prompt: str,
        turn_list: Optional[List[PromptTurn]],
        image_dicts: Optional[list[dict]],
        gap: float,
        expected_result_slots: int,
        completed_before_batch: int,
        navigation_tasks: Optional[List[asyncio.Task[None]]] = None,
    ) -> List[WindowResult]:
        workers_in_batch = min(len(batch_prompts), len(self._workers))
        submitted_count = 0
        progress_lock = asyncio.Lock()

        await self._publish(
            Event(
                type=EventType.LOG,
                data={
                    "level": "info",
                    "text": (
                        f"Batch {batch_idx + 1}/{total_batches}: windows will "
                        "prepare as soon as they are ready; same-IP submit clicks "
                        "will respect the configured gap"
                    ),
                },
            )
        )

        async def on_submit() -> None:
            nonlocal submitted_count
            async with progress_lock:
                submitted_count += 1
                await self._publish(
                    Event(
                        type=EventType.RUN_PROGRESS,
                        data={
                            "total_workers": expected_result_slots,
                            "submitted": completed_before_batch + submitted_count,
                            "phase": "submitting",
                            "batch": batch_idx + 1,
                            "total_batches": total_batches,
                        },
                    )
                )

        worker_tasks = [
            asyncio.create_task(
                self._run_simultaneous_worker_batch(
                    worker_idx=i,
                    prompt=batch_prompts[i],
                    batch_idx=batch_idx,
                    request=request,
                    system_prompt=system_prompt,
                    turn_list=turn_list,
                    image_dicts=image_dicts,
                    gap=gap,
                    on_submit=on_submit,
                    navigation_task=(
                        navigation_tasks[i]
                        if navigation_tasks and i < len(navigation_tasks)
                        else None
                    ),
                )
            )
            for i in range(workers_in_batch)
        ]

        worker_results = await asyncio.gather(
            *worker_tasks,
            return_exceptions=True,
        )
        await self._drain_navigation_tasks(navigation_tasks, workers_in_batch)

        batch_results: List[WindowResult] = []
        for i, result in enumerate(worker_results):
            if isinstance(result, BaseException):
                logger.error(
                    "Worker %d simultaneous batch pipeline crashed (batch %d): %s",
                    i,
                    batch_idx,
                    result,
                )
                failed = self._failed_result(
                    worker_id=i,
                    prompt=batch_prompts[i],
                    batch_idx=batch_idx,
                    error=f"Unexpected worker pipeline failure: {result}",
                )
                await self._publish_failed_worker_result(self._workers[i], failed)
                batch_results.append(failed)
                if turn_list and len(turn_list) > 1:
                    batch_results.extend(
                        self._build_skipped_turn_results(
                            worker_id=i,
                            batch_idx=batch_idx,
                            turns=turn_list,
                            start_turn_index=1,
                            error="Skipped (earlier turn failed or cancelled)",
                        )
                    )
            else:
                batch_results.extend(result)

        batch_results.sort(key=lambda item: (item.turn_index, item.worker_id))
        return batch_results

    async def _finish_cancelled(
        self,
        run_id: str,
        request: StartRunRequest,
        started_at: datetime,
        count: int,
        all_prompts: List[str],
        total_batches: int,
        partial_results: List[WindowResult],
    ) -> RunResult:
        """Clean up after cancellation: close browsers, notify UI, return."""
        await self._browser_manager.close_contexts(run_id=self._active_run_id)
        run_result = self._build_result(
            run_id, all_prompts, total_batches, started_at, partial_results,
        )
        self._current_run = run_result
        self._completed_results = list(partial_results)
        self._live_results = {}
        self._current_batch_index = None
        if partial_results:
            self._save_incremental(run_result)

        # Save checkpoint so the run can be resumed later
        if self._checkpoint_manager:
            completed_batches = len(partial_results) // count if count else 0
            if completed_batches < total_batches and completed_batches > 0:
                self._save_checkpoint(
                    run_id, request, all_prompts, completed_batches - 1,
                    count, total_batches, partial_results, started_at,
                )

        # RUN_CANCELLED already published by cancel()
        return run_result

    async def cancel(self) -> None:
        """Signal cancellation: stop workers, close browsers, and notify UI."""
        self._cancelled = True
        self._paused = False
        self._resume_event.set()
        for worker in self._workers:
            await worker.cancel()
        # Close browsers immediately — don't wait for execute_run to detect the flag
        await self._browser_manager.close_contexts(run_id=self._active_run_id)
        await self._publish(Event(type=EventType.RUN_CANCELLED))
        logger.info("Run cancelled")

    async def pause(self) -> None:
        if self._cancelled or self._paused:
            return

        self._paused = True
        self._resume_event.clear()
        self._refresh_current_run()
        self._save_pause_checkpoint()
        await self._publish(Event(type=EventType.RUN_PAUSED))
        await self._publish(
            Event(
                type=EventType.LOG,
                data={
                    "level": "warning",
                    "text": "Run paused",
                },
            )
        )

    async def resume(self) -> None:
        if self._cancelled or not self._paused:
            return

        self._paused = False
        self._resume_event.set()
        await self._publish(Event(type=EventType.RUN_RESUMED))
        await self._publish(
            Event(
                type=EventType.LOG,
                data={"level": "info", "text": "Run resumed"},
            )
        )

    async def _wait_if_paused(self) -> None:
        while not self._resume_event.is_set():
            if self._cancelled:
                return
            try:
                await asyncio.wait_for(self._resume_event.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass

    async def _sleep_with_pause(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            await self._wait_if_paused()
            if self._cancelled:
                return

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return

            await asyncio.sleep(min(remaining, 0.25))

    async def _publish(self, event: Event) -> None:
        """Publish an event, automatically stamping the current run_id."""
        if event.run_id is None:
            event.run_id = self._active_run_id
        await self._event_bus.publish(event)

    @staticmethod
    def _build_result(
        run_id: str,
        all_prompts: List[str],
        total_batches: int,
        started_at: datetime,
        window_results: List[WindowResult],
    ) -> RunResult:
        stamped_results = [
            wr.model_copy(update={"run_id": run_id})
            for wr in window_results
        ]
        successful = sum(1 for r in stamped_results if r.success)
        failed = sum(1 for r in stamped_results if not r.success)
        now = datetime.now(timezone.utc)
        return RunResult(
            run_id=run_id,
            prompt=all_prompts[0] if all_prompts else "",
            prompts=all_prompts,
            total_batches=total_batches,
            started_at=started_at,
            completed_at=now,
            total_elapsed_seconds=(now - started_at).total_seconds(),
            window_results=stamped_results,
            total_windows=len(stamped_results),
            successful_windows=successful,
            failed_windows=failed,
        )

    def _save_incremental(self, run_result: RunResult) -> None:
        """Save current results to Excel, CSV, and JSON (overwrites each call)."""
        out_dir = self._config.output_dir
        try:
            export_to_excel(run_result, out_dir)
            export_to_csv(run_result, out_dir)
            export_to_json(run_result, out_dir)
        except Exception as exc:
            logger.warning("Incremental save failed: %s", exc)

    async def _poll_worker_for_batch(
        self,
        worker: ArenaWorker,
        prompt: str,
        batch_idx: int,
        retain: str,
        baseline_responses: Optional[Tuple[str, str]] = None,
        turn_index: int = 0,
    ) -> WindowResult:
        result = await worker.poll_for_completion(
            baseline_responses=baseline_responses,
            pause_event=self._resume_event,
        )
        result.prompt = prompt
        result.batch_index = batch_idx
        result.turn_index = turn_index
        if retain == "model_a":
            result.model_b_name = None
            result.model_b_response = None
            result.model_b_response_html = None
        elif retain == "model_b":
            result.model_a_name = None
            result.model_a_response = None
            result.model_a_response_html = None

        self._live_results[(batch_idx, worker._id)] = result
        self._refresh_current_run()
        return result

    def _save_checkpoint(
        self,
        run_id: str,
        request: StartRunRequest,
        all_prompts: List[str],
        batch_idx: int,
        count: int,
        total_batches: int,
        all_window_results: List[WindowResult],
        started_at: datetime,
    ) -> None:
        """Save a checkpoint file so the run can be resumed."""
        try:
            checkpoint = RunCheckpoint(
                run_id=run_id,
                original_request=request.model_dump(mode="json"),
                all_prompts=all_prompts,
                completed_prompt_indices=list(
                    range(min((batch_idx + 1) * count, len(all_prompts)))
                ),
                next_batch_index=batch_idx + 1,
                total_batches=total_batches,
                window_results=[
                    wr.model_dump(mode="json") for wr in all_window_results
                ],
                original_started_at=started_at.isoformat(),
                last_checkpoint_at=datetime.now(timezone.utc).isoformat(),
                status="in_progress",
            )
            self._checkpoint_manager.save(checkpoint)
        except Exception as exc:
            logger.warning("Checkpoint save failed: %s", exc)

    def _save_pause_checkpoint(self) -> None:
        """Persist a resumable checkpoint when the run is paused."""
        if (
            not self._checkpoint_manager
            or not self._active_request
            or not self._active_run_id
            or not self._active_started_at
        ):
            return

        completed_batches = (
            len(self._completed_results) // self._active_batch_size
            if self._active_batch_size
            else 0
        )
        next_batch_index = (
            self._current_batch_index
            if self._current_batch_index is not None
            else completed_batches
        )

        try:
            checkpoint = RunCheckpoint(
                run_id=self._active_run_id,
                original_request=self._active_request.model_dump(mode="json"),
                all_prompts=self._active_all_prompts,
                completed_prompt_indices=list(range(len(self._completed_results))),
                next_batch_index=next_batch_index,
                total_batches=self._active_total_batches,
                window_results=[
                    wr.model_dump(mode="json") for wr in self._completed_results
                ],
                original_started_at=self._active_started_at.isoformat(),
                last_checkpoint_at=datetime.now(timezone.utc).isoformat(),
                status="in_progress",
            )
            self._checkpoint_manager.save(checkpoint)
        except Exception as exc:
            logger.warning("Pause checkpoint save failed: %s", exc)

    def _apply_jitter(self, base: float) -> float:
        jitter_range = base * self._config.timing.jitter_pct
        return base + random.uniform(-jitter_range, jitter_range)

    def _refresh_current_run(self) -> None:
        if not self._active_run_id or not self._active_started_at:
            return

        live_results = sorted(
            self._live_results.values(),
            key=lambda item: (item.batch_index, item.worker_id),
        )
        self._current_run = self._build_result(
            self._active_run_id,
            self._active_all_prompts,
            self._active_total_batches,
            self._active_started_at,
            [*self._completed_results, *live_results],
        )

    @staticmethod
    def _failed_result(
        worker_id: int,
        prompt: str,
        batch_idx: int,
        error: str,
        source: Optional[WindowResult] = None,
        turn_index: int = 0,
    ) -> WindowResult:
        return WindowResult(
            worker_id=worker_id,
            prompt=prompt,
            batch_index=batch_idx,
            turn_index=turn_index,
            model_a_name=source.model_a_name if source else None,
            model_b_name=source.model_b_name if source else None,
            started_at=source.started_at if source else None,
            completed_at=(
                source.completed_at
                if source and source.completed_at
                else datetime.now(timezone.utc)
            ),
            elapsed_seconds=source.elapsed_seconds if source else None,
            success=False,
            error=error,
        )

    async def _attempt_recovery(
        self,
        worker: ArenaWorker,
        worker_idx: int,
        prompt: str,
        batch_idx: int,
        request: StartRunRequest,
        system_prompt: str,
        image_dicts: Optional[list[dict]],
    ) -> WindowResult:
        """Attempt a single orchestrator-level retry for a failed worker.

        Closes the browser window, reopens fresh, re-navigates, and
        re-submits the same prompt.  Returns the result (success or failure).
        """
        if self._cancelled:
            result = self._failed_result(
                worker_id=worker_idx,
                prompt=prompt,
                batch_idx=batch_idx,
                error="Recovery skipped: run was cancelled",
            )
            await self._publish_failed_worker_result(worker, result)
            return result

        await self._publish(
            Event(
                type=EventType.LOG,
                worker_id=worker_idx,
                data={
                    "level": "warning",
                    "text": (
                        f"Worker {worker_idx}: starting recovery for batch "
                        f"{batch_idx + 1}; recreating the browser context and replaying the prompt"
                    ),
                },
            )
        )

        await self._wait_if_paused()
        recovery_gap = (
            request.submission_gap_seconds
            or self._config.timing.submission_gap_seconds
        )

        max_context_retries = 3
        for attempt in range(1, max_context_retries + 1):
            await self._wait_if_paused()
            if self._cancelled:
                result = self._failed_result(
                    worker_id=worker_idx,
                    prompt=prompt,
                    batch_idx=batch_idx,
                    error="Recovery skipped: run was cancelled",
                )
                await self._publish_failed_worker_result(worker, result)
                return result

            try:
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        worker_id=worker_idx,
                        data={
                            "level": "info",
                            "text": (
                                f"Worker {worker_idx}: recovery reset attempt "
                                f"{attempt}/{max_context_retries}"
                            ),
                        },
                    )
                )
                await worker.reset_with_fresh_context(
                    zoom_pct=request.zoom_pct,
                    clear_cookies=request.clear_cookies,
                    pause_event=self._resume_event,
                )
                if attempt > 1:
                    await self._publish(
                        Event(
                            type=EventType.LOG,
                            worker_id=worker_idx,
                            data={
                                "level": "info",
                                "text": (
                                    f"Worker {worker_idx}: recovery context "
                                    f"recreated on retry {attempt}/{max_context_retries}"
                                ),
                            },
                        )
                    )
                break
            except Exception as exc:
                if attempt >= max_context_retries:
                    result = self._failed_result(
                        worker_id=worker_idx,
                        prompt=prompt,
                        batch_idx=batch_idx,
                        error=(
                            "Recovery failed (context recreation): "
                            f"{exc} (after {max_context_retries} attempts)"
                        ),
                    )
                    await self._publish_failed_worker_result(worker, result)
                    return result

                wait_s = min(2.0 * attempt, 5.0)
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        worker_id=worker_idx,
                        data={
                            "level": "warning",
                            "text": (
                                f"Worker {worker_idx}: recovery context recreation "
                                f"failed (attempt {attempt}/{max_context_retries}): {exc} "
                                f"— retrying in {wait_s:.1f}s"
                            ),
                        },
                    )
                )
                await self._sleep_with_pause(wait_s)

        try:
            # System prompt phase (if applicable)
            recovery_baseline: Optional[Tuple[str, str]] = None
            if system_prompt:
                await self._wait_if_paused()
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        worker_id=worker_idx,
                        data={
                            "level": "info",
                            "text": (
                                f"Worker {worker_idx}: replaying the system prompt during recovery"
                            ),
                        },
                    )
                )
                await self._prepare_and_submit_prompt(
                    worker=worker,
                    worker_idx=worker_idx,
                    prompt=system_prompt,
                    gap=recovery_gap,
                    model_a=request.model_a,
                    model_b=request.model_b,
                )
                sys_result = await worker.poll_for_completion(
                    pause_event=self._resume_event,
                )
                if not sys_result.success:
                    result = self._failed_result(
                        worker_id=worker_idx,
                        prompt=prompt,
                        batch_idx=batch_idx,
                        error=(
                            f"Recovery failed (system prompt): "
                            f"{sys_result.error or 'unknown error'}"
                        ),
                        source=sys_result,
                    )
                    await self._publish_failed_worker_result(worker, result)
                    return result
                await self._wait_if_paused()
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        worker_id=worker_idx,
                        data={
                            "level": "info",
                            "text": (
                                f"Worker {worker_idx}: system prompt replay completed; preparing the follow-up composer"
                            ),
                        },
                    )
                )
                recovery_baseline = await worker.prepare_for_followup_prompt()

            # Submit actual prompt
            await self._wait_if_paused()
            await self._publish(
                Event(
                    type=EventType.LOG,
                    worker_id=worker_idx,
                    data={
                        "level": "info",
                        "text": (
                            f"Worker {worker_idx}: replaying the original prompt during recovery"
                        ),
                    },
                )
            )
            await self._prepare_and_submit_prompt(
                worker=worker,
                worker_idx=worker_idx,
                prompt=prompt,
                gap=recovery_gap,
                model_a=None if system_prompt else request.model_a,
                model_b=None if system_prompt else request.model_b,
                images=image_dicts,
            )

            # Poll for completion
            await self._publish(
                Event(
                    type=EventType.LOG,
                    worker_id=worker_idx,
                    data={
                        "level": "info",
                        "text": (
                            f"Worker {worker_idx}: recovery replay submitted; resuming polling"
                        ),
                    },
                )
            )
            result = await worker.poll_for_completion(
                baseline_responses=recovery_baseline,
                pause_event=self._resume_event,
            )
            result.prompt = prompt
            result.batch_index = batch_idx
            if result.success:
                await self._publish(
                    Event(
                        type=EventType.LOG,
                        worker_id=worker_idx,
                        data={
                            "level": "info",
                            "text": (
                                f"Worker {worker_idx}: recovery completed successfully"
                            ),
                        },
                    )
                )
            if not result.success:
                await self._publish_failed_worker_result(worker, result)
            return result

        except Exception as exc:
            result = self._failed_result(
                worker_id=worker_idx,
                prompt=prompt,
                batch_idx=batch_idx,
                error=f"Recovery failed (retry attempt): {exc}",
            )
            await self._publish_failed_worker_result(worker, result)
            return result

    async def _publish_failed_worker_result(
        self,
        worker: ArenaWorker,
        result: WindowResult,
    ) -> None:
        """Push a terminal failure state/result to the UI for orchestrator-side failures."""
        error_text = result.error or "Worker failed"
        try:
            if not worker.state_machine.is_terminal:
                await worker.state_machine.force_error(error_text)
        except Exception as exc:
            logger.warning(
                "Failed to transition worker %d to error state: %s",
                result.worker_id,
                exc,
            )

        await self._publish(
            Event(
                type=EventType.WORKER_ERROR,
                worker_id=result.worker_id,
                data={"error": error_text},
            )
        )
        await self._publish(
            Event(
                type=EventType.WORKER_COMPLETE,
                worker_id=result.worker_id,
                data={"result": result.model_dump(mode="json")},
            )
        )

    def get_run_snapshot(self) -> Optional[dict]:
        """Return a snapshot of the current run state for UI sync."""
        if not self._current_run:
            return None

        workers_snapshot = []
        for w in self._workers:
            workers_snapshot.append({
                "worker_id": w._id,
                "state": w.state_machine.state.value,
                "progress_pct": w.state_machine.progress,
            })

        run = self._current_run
        turn_count = (
            len(self._active_request.turns)
            if self._active_request and self._active_request.turns
            else 1
        )
        total_prompt_slots = (
            (len(run.prompts) if run.prompts else 0) * max(turn_count, 1)
        )
        return {
            "run_id": run.run_id,
            "running": True,
            "paused": self._paused,
            "cancelled": self._cancelled,
            "total_prompts": total_prompt_slots,
            "completed_prompts": len(run.window_results),
            "total_batches": run.total_batches,
            "workers": workers_snapshot,
            "results": [
                {
                    "worker_id": r.worker_id,
                    "prompt": (r.prompt or "")[:200],
                    "batch_index": r.batch_index,
                    "model_a_name": r.model_a_name,
                    "model_a_response": r.model_a_response,
                    "model_b_name": r.model_b_name,
                    "model_b_response": r.model_b_response,
                    "elapsed_seconds": r.elapsed_seconds,
                    "success": r.success,
                    "error": r.error,
                }
                for r in run.window_results
            ],
        }

    @property
    def last_result(self) -> Optional[RunResult]:
        return self._current_run
