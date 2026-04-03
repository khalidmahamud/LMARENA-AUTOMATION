"""DistributedOrchestrator — coordinates runs across remote worker nodes.

Implements the same interface as RunOrchestrator (execute_run, cancel, pause,
resume, get_run_snapshot, last_result) but dispatches work to remote nodes
via the NodeConnectionHandler instead of creating local ArenaWorkers.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.checkpoint.manager import CheckpointManager, RunCheckpoint
from src.core.events import Event, EventBus, EventType
from src.distributed.coordinator import NodeConnectionHandler, NodeInfo, NodeRegistry
from src.distributed.protocol import (
    AssignProxyPayload,
    AssignWorkPayload,
    CancelAllPayload,
    CancelWorkerPayload,
    NodeMessageType,
    PauseWorkerPayload,
    ProxyReportPayload,
    RequestProxyPayload,
    ResumeWorkerPayload,
    WorkerEventPayload,
    WorkerResultPayload,
)
from src.models.config import AppConfig
from src.models.messages import StartRunRequest
from src.models.results import RunResult, WindowResult

logger = logging.getLogger(__name__)


class DistributedOrchestrator:
    """Coordinates batch runs across remote worker nodes.

    Mirrors the RunOrchestrator interface:
    - execute_run(request, resume_checkpoint) -> RunResult
    - cancel() / pause() / resume()
    - get_run_snapshot() / last_result
    """

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        registry: NodeRegistry,
        node_handler: NodeConnectionHandler,
        checkpoint_manager: Optional[CheckpointManager] = None,
        proxy_pool: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._registry = registry
        self._node_handler = node_handler
        self._checkpoint_manager = checkpoint_manager
        self._proxy_pool = proxy_pool

        self._cancelled = False
        self._paused = False
        self._resume_event = asyncio.Event()
        self._resume_event.set()

        self._current_run: Optional[RunResult] = None
        self._active_request: Optional[StartRunRequest] = None
        self._active_run_id: Optional[str] = None
        self._active_started_at: Optional[datetime] = None

        # Worker assignment tracking
        self._worker_assignments: Dict[int, str] = {}  # worker_id -> node_id
        self._worker_epochs: Dict[int, int] = {}  # worker_id -> epoch

        # Result collection
        self._completed_results: List[WindowResult] = []
        self._live_results: Dict[Tuple[int, int, int], WindowResult] = {}  # (batch, worker, turn)
        self._pending_workers: Set[int] = set()  # Workers we're waiting on
        self._result_events: Dict[int, asyncio.Event] = {}  # worker_id -> completion signal

        # Epoch counter for fencing
        self._epoch = 0

        # Register callbacks with the node handler
        node_handler.set_callbacks(
            on_worker_result=self._on_worker_result,
            on_worker_event=self._on_worker_event,
            on_proxy_report=self._on_proxy_report,
            on_request_proxy=self._on_request_proxy,
            on_node_dead=self._on_node_dead,
        )

    @property
    def last_result(self) -> Optional[RunResult]:
        return self._current_run

    async def execute_run(
        self,
        request: StartRunRequest,
        resume_checkpoint: Optional[RunCheckpoint] = None,
    ) -> RunResult:
        """Execute a distributed run across worker nodes."""
        count = request.window_count
        self._cancelled = False
        self._paused = False
        self._resume_event.set()

        run_id = request.run_id or uuid.uuid4().hex[:12]
        self._active_run_id = run_id
        self._active_request = request
        self._active_started_at = datetime.now(timezone.utc)
        self._completed_results = []
        self._live_results = {}

        # Build prompt list
        all_prompts = self._build_prompt_list(request)
        batch_size = count
        total_batches = (len(all_prompts) + batch_size - 1) // batch_size

        # Resume from checkpoint if provided
        start_batch = 0
        if resume_checkpoint:
            start_batch = resume_checkpoint.next_batch_index
            self._completed_results = [
                WindowResult(**wr) for wr in resume_checkpoint.window_results
            ]
            self._active_started_at = datetime.fromisoformat(
                resume_checkpoint.original_started_at
            )

        # Initialize run result
        self._current_run = RunResult(
            run_id=run_id,
            prompt=all_prompts[0] if all_prompts else "",
            prompts=all_prompts,
            total_batches=total_batches,
            started_at=self._active_started_at,
            total_windows=count * total_batches,
        )

        # Check capacity
        available = self._registry.total_capacity
        if available < count:
            raise RuntimeError(
                f"Not enough worker nodes: need {count} workers, "
                f"have {available} available across {self._registry.healthy_nodes} nodes"
            )

        # Publish run started
        await self._publish(Event(
            type=EventType.RUN_STARTED,
            data={
                "run_id": run_id,
                "window_count": count,
                "prompt": all_prompts[0] if all_prompts else "",
                "total_prompts": len(all_prompts),
                "total_batches": total_batches,
            },
        ))

        try:
            # Execute batches
            for batch_idx in range(start_batch, total_batches):
                if self._cancelled:
                    break

                await self._wait_if_paused()

                batch_start = batch_idx * batch_size
                batch_prompts = all_prompts[batch_start:batch_start + batch_size]
                actual_workers = len(batch_prompts)

                await self._publish(Event(
                    type=EventType.LOG,
                    data={
                        "level": "info",
                        "text": f"Starting batch {batch_idx + 1}/{total_batches} "
                                f"({actual_workers} workers)",
                    },
                ))

                # Distribute workers across nodes
                assignments = self._distribute_workers(actual_workers)

                # Assign work to nodes
                await self._assign_batch(
                    run_id=run_id,
                    batch_idx=batch_idx,
                    prompts=batch_prompts,
                    assignments=assignments,
                    request=request,
                )

                # Wait for all workers to complete
                batch_results = await self._collect_batch_results(
                    batch_idx=batch_idx,
                    worker_ids=list(range(actual_workers)),
                )

                # Store results
                for result in batch_results:
                    self._completed_results.append(result)

                # Save checkpoint
                if self._checkpoint_manager:
                    self._save_checkpoint(
                        run_id=run_id,
                        all_prompts=all_prompts,
                        next_batch=batch_idx + 1,
                        total_batches=total_batches,
                        request=request,
                    )

                # Update progress
                await self._publish(Event(
                    type=EventType.RUN_PROGRESS,
                    data={
                        "submitted": len(self._completed_results),
                        "total_workers": count * total_batches,
                        "phase": "batch_complete",
                        "batch": batch_idx + 1,
                        "total_batches": total_batches,
                    },
                ))

        except asyncio.CancelledError:
            self._cancelled = True

        # Build final result
        self._current_run = self._build_result(run_id, all_prompts, total_batches)

        if self._cancelled:
            await self._publish(Event(
                type=EventType.RUN_CANCELLED,
                data={"run_id": run_id},
            ))
        else:
            await self._publish(Event(
                type=EventType.RUN_COMPLETE,
                data={
                    "run_id": run_id,
                    "run_result": self._current_run.model_dump(mode="json"),
                },
            ))

        return self._current_run

    async def cancel(self) -> None:
        """Cancel the current run."""
        self._cancelled = True
        self._paused = False
        self._resume_event.set()

        # Send cancel to all assigned nodes
        if self._active_run_id:
            for worker_id, node_id in self._worker_assignments.items():
                payload = CancelWorkerPayload(
                    run_id=self._active_run_id,
                    worker_id=worker_id,
                )
                await self._node_handler.send_to_node_by_id(
                    node_id, NodeMessageType.CANCEL_WORKER, payload
                )

        # Signal all pending result events
        for evt in self._result_events.values():
            evt.set()

        await self._publish(Event(type=EventType.RUN_CANCELLED))
        logger.info("Distributed run cancelled")

    async def pause(self) -> None:
        """Pause the current run."""
        if self._cancelled or self._paused:
            return

        self._paused = True
        self._resume_event.clear()

        # Send pause to all assigned nodes
        if self._active_run_id:
            for node_id in set(self._worker_assignments.values()):
                payload = PauseWorkerPayload(run_id=self._active_run_id)
                await self._node_handler.send_to_node_by_id(
                    node_id, NodeMessageType.PAUSE_WORKER, payload
                )

        await self._publish(Event(type=EventType.RUN_PAUSED))

    async def resume(self) -> None:
        """Resume the current run."""
        if self._cancelled or not self._paused:
            return

        self._paused = False
        self._resume_event.set()

        # Send resume to all assigned nodes
        if self._active_run_id:
            for node_id in set(self._worker_assignments.values()):
                payload = ResumeWorkerPayload(run_id=self._active_run_id)
                await self._node_handler.send_to_node_by_id(
                    node_id, NodeMessageType.RESUME_WORKER, payload
                )

        await self._publish(Event(type=EventType.RUN_RESUMED))

    def get_run_snapshot(self) -> Optional[dict]:
        """Return current run state for UI reconnection."""
        if not self._current_run:
            return None

        workers_snapshot = []
        for worker_id, node_id in self._worker_assignments.items():
            workers_snapshot.append({
                "worker_id": worker_id,
                "node_id": node_id,
                "state": "running" if worker_id in self._pending_workers else "complete",
                "progress_pct": 0.0 if worker_id in self._pending_workers else 100.0,
            })

        return {
            "running": not self._cancelled and bool(self._pending_workers),
            "paused": self._paused,
            "run_id": self._active_run_id,
            "prompt": self._current_run.prompt,
            "prompts": self._current_run.prompts,
            "total_batches": self._current_run.total_batches,
            "total_windows": self._current_run.total_windows,
            "completed_results": len(self._completed_results),
            "workers": workers_snapshot,
            "nodes": {
                nid: {
                    "state": n.state.value,
                    "max_workers": n.max_workers,
                    "allocated": n.allocated_workers,
                }
                for nid, n in self._registry.get_all().items()
            },
        }

    # ──── Worker Distribution ────

    def _distribute_workers(self, count: int) -> Dict[int, str]:
        """Assign worker_ids to nodes based on scheduling policy.

        Returns: {worker_id: node_id}
        """
        dist_config = self._config.distributed
        policy = dist_config.scheduling_policy if dist_config else "fill"
        healthy = self._registry.get_healthy()

        if not healthy:
            raise RuntimeError("No healthy worker nodes available")

        assignments: Dict[int, str] = {}

        if policy == "spread":
            # Round-robin across nodes
            sorted_nodes = sorted(healthy, key=lambda n: n.allocated_workers)
            for i in range(count):
                node = sorted_nodes[i % len(sorted_nodes)]
                if node.available_capacity > 0:
                    assignments[i] = node.node_id
                else:
                    # Find any node with capacity
                    for n in sorted_nodes:
                        if n.available_capacity > 0:
                            assignments[i] = n.node_id
                            break
        else:
            # Fill: pack workers onto fewest nodes
            sorted_nodes = sorted(healthy, key=lambda n: -n.available_capacity)
            worker_idx = 0
            for node in sorted_nodes:
                while worker_idx < count and node.available_capacity > 0:
                    assignments[worker_idx] = node.node_id
                    self._registry.allocate_worker(node.node_id, worker_idx)
                    worker_idx += 1
                if worker_idx >= count:
                    break

        if len(assignments) < count:
            raise RuntimeError(
                f"Could only assign {len(assignments)}/{count} workers "
                f"across {len(healthy)} nodes"
            )

        return assignments

    # ──── Batch Execution ────

    async def _assign_batch(
        self,
        run_id: str,
        batch_idx: int,
        prompts: List[str],
        assignments: Dict[int, str],
        request: StartRunRequest,
    ) -> None:
        """Send assign_work to each node for this batch."""
        self._worker_assignments = assignments
        self._pending_workers = set(assignments.keys())
        self._result_events = {
            wid: asyncio.Event() for wid in assignments.keys()
        }

        gap = request.submission_gap_seconds or self._config.timing.submission_gap_seconds

        for worker_id, node_id in assignments.items():
            if self._cancelled:
                break

            self._epoch += 1
            self._worker_epochs[worker_id] = self._epoch

            # Get proxy from pool if available
            proxy = None
            if self._proxy_pool:
                proxy_dict = self._proxy_pool.get_next_healthy()
                if proxy_dict:
                    proxy = proxy_dict

            prompt = prompts[worker_id] if worker_id < len(prompts) else prompts[-1]

            payload = AssignWorkPayload(
                run_id=run_id,
                worker_id=worker_id,
                batch_index=batch_idx,
                prompt=prompt,
                turns=[t.model_dump() for t in request.turns] if request.turns else None,
                system_prompt=request.system_prompt,
                combine_with_first=request.combine_with_first,
                model_a=request.model_a,
                model_b=request.model_b,
                retain_output=request.retain_output,
                images=[img.model_dump() for img in request.images] if request.images else None,
                simultaneous_start=request.simultaneous_start,
                proxy=proxy,
                clear_cookies=request.clear_cookies,
                incognito=request.incognito,
                zoom_pct=request.zoom_pct,
                proxy_on_challenge=request.proxy_on_challenge,
                windows_per_proxy=request.windows_per_proxy,
                submission_gap_seconds=gap,
            )

            await self._node_handler.send_to_node_by_id(
                node_id,
                NodeMessageType.ASSIGN_WORK,
                payload,
                epoch=self._epoch,
            )

            # Submission gap between workers
            if worker_id < len(assignments) - 1 and gap > 0:
                jitter = gap * self._config.timing.jitter_pct
                import random
                actual_gap = gap + random.uniform(-jitter, jitter)
                await asyncio.sleep(max(0, actual_gap))

    async def _collect_batch_results(
        self,
        batch_idx: int,
        worker_ids: List[int],
    ) -> List[WindowResult]:
        """Wait for all workers in a batch to report results."""
        timeout = self._config.timing.response_timeout_seconds + 60  # Extra grace

        results: List[WindowResult] = []

        # Wait for each worker's result
        tasks = [
            self._wait_for_worker_result(wid, batch_idx, timeout)
            for wid in worker_ids
        ]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)

        for wid, result in zip(worker_ids, gathered):
            if isinstance(result, Exception):
                # Worker failed
                results.append(WindowResult(
                    worker_id=wid,
                    run_id=self._active_run_id,
                    prompt="",
                    batch_index=batch_idx,
                    success=False,
                    error=str(result),
                ))
            elif result:
                results.append(result)

        return results

    async def _wait_for_worker_result(
        self, worker_id: int, batch_idx: int, timeout: float
    ) -> Optional[WindowResult]:
        """Wait for a specific worker to complete."""
        evt = self._result_events.get(worker_id)
        if not evt:
            return None

        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Worker %d timed out after %.0fs", worker_id, timeout
            )
            return WindowResult(
                worker_id=worker_id,
                run_id=self._active_run_id,
                prompt="",
                batch_index=batch_idx,
                success=False,
                error=f"Timeout after {timeout:.0f}s",
            )

        if self._cancelled:
            return None

        # Look up result
        for key, result in self._live_results.items():
            if key[0] == batch_idx and key[1] == worker_id:
                return result

        return None

    # ──── Callbacks from NodeConnectionHandler ────

    async def _on_worker_result(
        self, node_id: str, payload: WorkerResultPayload
    ) -> None:
        """Handle a completed worker result from a node."""
        key = (payload.batch_index, payload.worker_id, payload.turn_index)

        # Duplicate detection
        if key in self._live_results:
            logger.debug(
                "Duplicate result for worker %d batch %d — discarding",
                payload.worker_id,
                payload.batch_index,
            )
            return

        result = WindowResult(**payload.result)
        self._live_results[key] = result
        self._pending_workers.discard(payload.worker_id)

        # Signal completion
        evt = self._result_events.get(payload.worker_id)
        if evt:
            evt.set()

        # Deallocate from node
        self._registry.deallocate_worker(node_id, payload.worker_id)

    async def _on_worker_event(
        self, node_id: str, payload: WorkerEventPayload
    ) -> None:
        """Handle forwarded worker events (for state tracking)."""
        # Events are already republished to EventBus by NodeConnectionHandler
        pass

    async def _on_proxy_report(self, payload: ProxyReportPayload) -> None:
        """Handle proxy health report — already processed by NodeConnectionHandler."""
        pass

    async def _on_request_proxy(
        self, node_id: str, payload: RequestProxyPayload
    ) -> None:
        """Handle proxy request from a node (after challenge)."""
        proxy = None
        if self._proxy_pool:
            proxy_dict = self._proxy_pool.get_next_healthy()
            if proxy_dict:
                proxy = proxy_dict

        resp = AssignProxyPayload(
            run_id=payload.run_id,
            worker_id=payload.worker_id,
            proxy=proxy,
        )
        await self._node_handler.send_to_node_by_id(
            node_id, NodeMessageType.ASSIGN_PROXY, resp
        )

    async def _on_node_dead(self, node_id: str, node: NodeInfo) -> None:
        """Handle a node dying — mark affected workers as failed."""
        affected = list(node.active_worker_ids)
        for worker_id in affected:
            # Create a failed result for the lost worker
            key_matches = [
                k for k in self._live_results
                if k[1] == worker_id
            ]
            if not key_matches:
                # Worker had no result yet — mark as failed
                self._pending_workers.discard(worker_id)
                evt = self._result_events.get(worker_id)
                if evt:
                    evt.set()  # Unblock the waiter

            self._registry.deallocate_worker(node_id, worker_id)

        logger.warning(
            "Node %s died — %d workers affected: %s",
            node_id,
            len(affected),
            affected,
        )

    # ──── Helpers ────

    def _build_prompt_list(self, request: StartRunRequest) -> List[str]:
        """Build flat list of prompts from the request."""
        if request.prompts:
            return list(request.prompts)
        if request.turns:
            return [request.turns[0].text]
        return [request.prompt]

    def _build_result(
        self, run_id: str, all_prompts: List[str], total_batches: int
    ) -> RunResult:
        """Build the final RunResult."""
        now = datetime.now(timezone.utc)
        all_results = list(self._completed_results) + [
            r for r in self._live_results.values()
            if r not in self._completed_results
        ]

        successful = sum(1 for r in all_results if r.success)
        failed = len(all_results) - successful

        started = self._active_started_at or now
        elapsed = (now - started).total_seconds()

        return RunResult(
            run_id=run_id,
            prompt=all_prompts[0] if all_prompts else "",
            prompts=all_prompts,
            total_batches=total_batches,
            started_at=started,
            completed_at=now,
            total_elapsed_seconds=elapsed,
            window_results=all_results,
            total_windows=len(all_results),
            successful_windows=successful,
            failed_windows=failed,
        )

    def _save_checkpoint(
        self,
        run_id: str,
        all_prompts: List[str],
        next_batch: int,
        total_batches: int,
        request: StartRunRequest,
    ) -> None:
        """Save a checkpoint after a batch completes."""
        if not self._checkpoint_manager:
            return

        checkpoint = RunCheckpoint(
            run_id=run_id,
            original_request=request.model_dump(mode="json"),
            all_prompts=all_prompts,
            completed_prompt_indices=list(range(next_batch * request.window_count)),
            next_batch_index=next_batch,
            total_batches=total_batches,
            window_results=[r.model_dump(mode="json") for r in self._completed_results],
            original_started_at=(
                self._active_started_at or datetime.now(timezone.utc)
            ).isoformat(),
            last_checkpoint_at=datetime.now(timezone.utc).isoformat(),
            status="in_progress",
        )
        self._checkpoint_manager.save(checkpoint)

    async def _publish(self, event: Event) -> None:
        """Publish an event with run_id stamped."""
        if not event.run_id:
            event.run_id = self._active_run_id
        await self._event_bus.publish(event)

    async def _wait_if_paused(self) -> None:
        """Block until resumed or cancelled."""
        while self._paused and not self._cancelled:
            await asyncio.sleep(0.25)
            if self._resume_event.is_set():
                break
