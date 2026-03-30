from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from src.browser.manager import BrowserManager
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
        system_prompt = request.system_prompt.strip()
        combine_with_first = request.combine_with_first and bool(system_prompt)

        # Determine all prompts and chunk into batches
        all_prompts = request.prompts if request.prompts else [request.prompt]

        if combine_with_first:
            all_prompts = [f"{system_prompt}\n\n{p}" for p in all_prompts]
            system_prompt = ""  # Skip Phase 1 entirely

        batches: List[List[str]] = [
            all_prompts[i : i + count]
            for i in range(0, len(all_prompts), count)
        ]
        total_batches = len(batches)

        await self._event_bus.publish(
            Event(
                type=EventType.RUN_STARTED,
                data={
                    "run_id": run_id,
                    "window_count": count,
                    "prompt": all_prompts[0][:200],
                    "total_prompts": len(all_prompts),
                    "total_batches": total_batches,
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
            count, display_override=display_override
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

        if self._cancelled:
            return await self._finish_cancelled(
                run_id, request, started_at, count, all_prompts, total_batches, []
            )

        # Phase 4-6: Batch loop
        gap = request.submission_gap_seconds or self._config.timing.submission_gap_seconds
        all_window_results: List[WindowResult] = []

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
                for worker in self._workers:
                    await worker.state_machine.reset()
                nav_results = await asyncio.gather(
                    *(w.navigate_to_arena(zoom_pct=request.zoom_pct)
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
                for i in range(workers_in_batch):
                    worker = self._workers[i]
                    prompt = batch_prompts[i]

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

                    try:
                        await worker.submit_prompt(
                            prompt=system_prompt,
                            model_a=request.model_a,
                            model_b=request.model_b,
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
                        await asyncio.sleep(jittered)

                if self._cancelled:
                    break

                system_poll_tasks = [
                    asyncio.create_task(worker.poll_for_completion())
                    for _, worker in submitted_system
                ]
                if system_poll_tasks:
                    await asyncio.gather(
                        *system_poll_tasks, return_exceptions=True
                    )

                if self._cancelled:
                    break

                for worker_idx, worker in submitted_system:
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

            for i in range(workers_in_batch):
                worker = self._workers[i]
                prompt = batch_prompts[i]
                baseline_responses: Optional[Tuple[str, str]] = None

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
                    await asyncio.sleep(jittered)

            if self._cancelled:
                break

            # Poll submitted workers
            poll_tasks = [
                asyncio.create_task(
                    worker.poll_for_completion(
                        baseline_responses=baseline_responses
                    )
                )
                for _, worker, _, baseline_responses in submitted
            ]
            if poll_tasks:
                await asyncio.gather(*poll_tasks, return_exceptions=True)

            if self._cancelled:
                break

            # Collect batch results — separate successes from failures
            retain = request.retain_output
            poll_failures: List[Tuple[int, ArenaWorker, str]] = []
            for worker_idx, worker, prompt, _ in submitted:
                result = worker.get_result()
                if result and result.success:
                    result.prompt = prompt
                    result.batch_index = batch_idx
                    if retain == "model_a":
                        result.model_b_name = None
                        result.model_b_response = None
                    elif retain == "model_b":
                        result.model_a_name = None
                        result.model_a_response = None
                    batch_results.append(result)
                else:
                    poll_failures.append((worker_idx, worker, prompt))

            # Attempt recovery for all failed workers (early + polling failures)
            all_failures = early_failures + poll_failures
            if all_failures and not self._cancelled:
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

            # Save incremental results after each batch
            self._current_run = self._build_result(
                run_id, all_prompts, total_batches,
                started_at, all_window_results,
            )
            self._save_incremental(self._current_run)

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
        self._save_incremental(run_result)

        await self._event_bus.publish(
            Event(
                type=EventType.RUN_COMPLETE,
                data={"run_result": run_result.model_dump(mode="json")},
            )
        )

        # Close browser windows after successful completion
        await self._browser_manager.close_contexts()

        if successful == 0 and len(all_prompts) > 0:
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
        if partial_results:
            self._save_incremental(run_result)
        # RUN_CANCELLED already published by cancel()
        return run_result

    async def cancel(self) -> None:
        """Signal cancellation: stop workers, close browsers, and notify UI."""
        self._cancelled = True
        for worker in self._workers:
            await worker.cancel()
        # Close browsers immediately — don't wait for execute_run to detect the flag
        await self._browser_manager.close_contexts()
        await self._event_bus.publish(Event(type=EventType.RUN_CANCELLED))
        logger.info("Run cancelled")

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

    def _apply_jitter(self, base: float) -> float:
        jitter_range = base * self._config.timing.jitter_pct
        return base + random.uniform(-jitter_range, jitter_range)

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

        try:
            await worker.reset_with_fresh_context(
                zoom_pct=request.zoom_pct,
                clear_cookies=request.clear_cookies,
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
                await worker.submit_prompt(
                    prompt=system_prompt,
                    model_a=request.model_a,
                    model_b=request.model_b,
                )
                sys_result = await worker.poll_for_completion()
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
                recovery_baseline = await worker.prepare_for_followup_prompt()

            # Submit actual prompt
            await worker.submit_prompt(
                prompt=prompt,
                model_a=None if system_prompt else request.model_a,
                model_b=None if system_prompt else request.model_b,
            )

            # Poll for completion
            result = await worker.poll_for_completion(
                baseline_responses=recovery_baseline,
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

    @property
    def last_result(self) -> Optional[RunResult]:
        return self._current_run
