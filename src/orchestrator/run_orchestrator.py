from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.browser.manager import BrowserManager
from src.checkpoint.manager import CheckpointManager, RunCheckpoint
from src.core.events import Event, EventBus, EventType
from src.core.exceptions import AllWorkersFailedError, RunCancelledError
from src.export.excel_exporter import export_to_csv, export_to_excel, export_to_json
from src.models.config import AppConfig, DisplayConfig
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
            run_id = str(uuid.uuid4())[:8]
            started_at = datetime.now(timezone.utc)
            start_batch_idx = 0
            restored_results = []

            # Determine all prompts and chunk into batches
            all_prompts = request.prompts if request.prompts else [request.prompt]

            # Single prompt with multiple windows: replicate across all windows
            if len(all_prompts) == 1 and count > 1:
                all_prompts = all_prompts * count

            if combine_with_first:
                all_prompts = [f"{system_prompt}\n\n{p}" for p in all_prompts]
                system_prompt = ""  # Skip Phase 1 entirely

        batches: List[List[str]] = [
            all_prompts[i : i + count]
            for i in range(0, len(all_prompts), count)
        ]
        total_batches = len(batches)
        self._active_request = request
        self._active_run_id = run_id
        self._active_started_at = started_at
        self._active_all_prompts = list(all_prompts)
        self._active_total_batches = total_batches
        self._active_batch_size = count
        self._completed_results = list(restored_results)
        self._live_results = {}
        self._current_batch_index = None
        self._refresh_current_run()

        await self._event_bus.publish(
            Event(
                type=EventType.RUN_STARTED,
                data={
                    "run_id": run_id,
                    "window_count": count,
                    "prompt": all_prompts[0][:200],
                    "total_prompts": len(all_prompts),
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
        )

        # Phase 1: Launch browsers
        contexts = await self._browser_manager.create_contexts(
            count,
            display_override=display_override,
            incognito=request.incognito,
        )

        # Phase 2: Create workers
        self._workers = [
            ArenaWorker(
                worker_id=i,
                context=contexts[i],
                config=self._config,
                event_bus=self._event_bus,
                context_recreator=self._browser_manager.recreate_context,
            )
            for i in range(count)
        ]

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
            if isinstance(result, Exception):
                logger.error("Worker %d navigation failed: %s", i, result)

        if request.clear_cookies:
            await self._event_bus.publish(
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
            await self._event_bus.publish(
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
            await self._wait_if_paused()
            if self._cancelled:
                break

            # Log batch start
            await self._event_bus.publish(
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
                nav_results = await asyncio.gather(
                    *(w.navigate_to_arena(
                        zoom_pct=request.zoom_pct,
                        pause_event=self._resume_event,
                    )
                      for w in self._workers),
                    return_exceptions=True,
                )
                for i, result in enumerate(nav_results):
                    if isinstance(result, Exception):
                        logger.error(
                            "Worker %d re-navigation failed (batch %d): %s",
                            i, batch_idx, result,
                        )

            if self._cancelled:
                break

            # Submit this batch's prompts (sequential with gap)
            workers_in_batch = min(len(batch_prompts), count)
            batch_results: List[WindowResult] = []
            early_failures: List[Tuple[int, ArenaWorker, str]] = []
            submitted: List[
                Tuple[int, ArenaWorker, str, Optional[Tuple[str, str]]]
            ] = []
            ready_for_actual: dict[int, Tuple[str, str]] = {}

            if system_prompt:
                await self._event_bus.publish(
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
                    await self._event_bus.publish(
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
                        if isinstance(result, Exception):
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

                await self._event_bus.publish(
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
                await self._event_bus.publish(
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
                        if isinstance(result, Exception):
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
                    await self._event_bus.publish(
                        Event(
                            type=EventType.RUN_PROGRESS,
                            data={
                                "total_workers": len(all_prompts),
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
                    await self._event_bus.publish(
                        Event(
                            type=EventType.RUN_PROGRESS,
                            data={
                                "total_workers": len(all_prompts),
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
                await self._event_bus.publish(
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
                    )
                    for widx, worker, prompt in all_failures
                ]
                recovery_results = await asyncio.gather(
                    *recovery_tasks, return_exceptions=True
                )
                for (widx, _, prompt), recovery_out in zip(
                    all_failures, recovery_results
                ):
                    if isinstance(recovery_out, Exception):
                        batch_results.append(
                            self._failed_result(
                                worker_id=widx,
                                prompt=prompt,
                                batch_idx=batch_idx,
                                error=f"Recovery raised exception: {recovery_out}",
                            )
                        )
                    else:
                        if recovery_out.success:
                            if retain == "model_a":
                                recovery_out.model_b_name = None
                                recovery_out.model_b_response = None
                            elif retain == "model_b":
                                recovery_out.model_a_name = None
                                recovery_out.model_a_response = None
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
            await self._event_bus.publish(
                Event(
                    type=EventType.RUN_PROGRESS,
                    data={
                        "total_workers": len(all_prompts),
                        "submitted": len(all_window_results),
                        "phase": "batch_complete",
                        "batch": batch_idx + 1,
                        "total_batches": total_batches,
                    },
                )
            )

            await self._event_bus.publish(
                Event(
                    type=EventType.LOG,
                    data={
                        "level": "info",
                        "text": (
                            f"Batch {batch_idx + 1}/{total_batches} complete "
                            f"— {len(all_window_results)}/{len(all_prompts)} prompts done"
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

        await self._event_bus.publish(
            Event(
                type=EventType.RUN_COMPLETE,
                data={"run_result": run_result.model_dump(mode="json")},
            )
        )

        # Close browser windows after successful completion
        await self._browser_manager.close_contexts()

        if run_result.successful_windows == 0 and len(all_prompts) > 0:
            raise AllWorkersFailedError(len(all_prompts))

        return run_result

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
        await self._browser_manager.close_contexts()
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
        await self._browser_manager.close_contexts()
        await self._event_bus.publish(Event(type=EventType.RUN_CANCELLED))
        logger.info("Run cancelled")

    async def pause(self) -> None:
        if self._cancelled or self._paused:
            return

        self._paused = True
        self._resume_event.clear()
        self._refresh_current_run()
        self._save_pause_checkpoint()
        await self._event_bus.publish(Event(type=EventType.RUN_PAUSED))
        await self._event_bus.publish(
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
        await self._event_bus.publish(Event(type=EventType.RUN_RESUMED))
        await self._event_bus.publish(
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

    @staticmethod
    def _build_result(
        run_id: str,
        all_prompts: List[str],
        total_batches: int,
        started_at: datetime,
        window_results: List[WindowResult],
    ) -> RunResult:
        successful = sum(1 for r in window_results if r.success)
        failed = sum(1 for r in window_results if not r.success)
        now = datetime.now(timezone.utc)
        return RunResult(
            run_id=run_id,
            prompt=all_prompts[0] if all_prompts else "",
            prompts=all_prompts,
            total_batches=total_batches,
            started_at=started_at,
            completed_at=now,
            total_elapsed_seconds=(now - started_at).total_seconds(),
            window_results=window_results,
            total_windows=len(window_results),
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
    ) -> WindowResult:
        result = await worker.poll_for_completion(
            baseline_responses=baseline_responses,
            pause_event=self._resume_event,
        )
        result.prompt = prompt
        result.batch_index = batch_idx
        if retain == "model_a":
            result.model_b_name = None
            result.model_b_response = None
        elif retain == "model_b":
            result.model_a_name = None
            result.model_a_response = None

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
    ) -> WindowResult:
        return WindowResult(
            worker_id=worker_id,
            prompt=prompt,
            batch_index=batch_idx,
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
    ) -> WindowResult:
        """Attempt a single orchestrator-level retry for a failed worker.

        Closes the browser window, reopens fresh, re-navigates, and
        re-submits the same prompt.  Returns the result (success or failure).
        """
        if self._cancelled:
            return self._failed_result(
                worker_id=worker_idx,
                prompt=prompt,
                batch_idx=batch_idx,
                error="Recovery skipped: run was cancelled",
            )

        await self._event_bus.publish(
            Event(
                type=EventType.LOG,
                worker_id=worker_idx,
                data={
                    "level": "warning",
                    "text": (
                        f"Worker {worker_idx}: attempting recovery — "
                        "reopening browser and retrying prompt"
                    ),
                },
            )
        )

        await self._wait_if_paused()

        try:
            await worker.reset_with_fresh_context(
                zoom_pct=request.zoom_pct,
                clear_cookies=request.clear_cookies,
                pause_event=self._resume_event,
            )
        except Exception as exc:
            return self._failed_result(
                worker_id=worker_idx,
                prompt=prompt,
                batch_idx=batch_idx,
                error=f"Recovery failed (context recreation): {exc}",
            )

        try:
            # System prompt phase (if applicable)
            recovery_baseline: Optional[Tuple[str, str]] = None
            if system_prompt:
                await self._wait_if_paused()
                await worker.submit_prompt(
                    prompt=system_prompt,
                    model_a=request.model_a,
                    model_b=request.model_b,
                    pause_event=self._resume_event,
                )
                sys_result = await worker.poll_for_completion(
                    pause_event=self._resume_event,
                )
                if not sys_result.success:
                    return self._failed_result(
                        worker_id=worker_idx,
                        prompt=prompt,
                        batch_idx=batch_idx,
                        error=(
                            f"Recovery failed (system prompt): "
                            f"{sys_result.error or 'unknown error'}"
                        ),
                        source=sys_result,
                    )
                await self._wait_if_paused()
                recovery_baseline = await worker.prepare_for_followup_prompt()

            # Submit actual prompt
            await self._wait_if_paused()
            await worker.submit_prompt(
                prompt=prompt,
                model_a=None if system_prompt else request.model_a,
                model_b=None if system_prompt else request.model_b,
                pause_event=self._resume_event,
            )

            # Poll for completion
            result = await worker.poll_for_completion(
                baseline_responses=recovery_baseline,
                pause_event=self._resume_event,
            )
            result.prompt = prompt
            result.batch_index = batch_idx
            return result

        except Exception as exc:
            return self._failed_result(
                worker_id=worker_idx,
                prompt=prompt,
                batch_idx=batch_idx,
                error=f"Recovery failed (retry attempt): {exc}",
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
        return {
            "run_id": run.run_id,
            "running": True,
            "paused": self._paused,
            "cancelled": self._cancelled,
            "total_prompts": len(run.prompts) if run.prompts else 0,
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
