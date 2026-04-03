"""NodeClient — WebSocket client that connects a worker node to the coordinator.

Manages the connection lifecycle, receives commands (assign_work, pause, cancel),
and manages local ArenaWorker instances via a LocalWorkerSlot abstraction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import websockets
from websockets.asyncio.client import ClientConnection

from src.browser.manager import BrowserManager
from src.browser.selectors import SelectorRegistry
from src.core.events import Event, EventBus, EventType
from src.distributed.event_forwarder import EventForwarder
from src.distributed.protocol import (
    AssignProxyPayload,
    AssignWorkPayload,
    CancelAllPayload,
    CancelWorkerPayload,
    HeartbeatPingPayload,
    HeartbeatPongPayload,
    NodeDisplayInfo,
    NodeMessage,
    NodeMessageType,
    NodeRegisterPayload,
    NodeShuttingDownPayload,
    PauseWorkerPayload,
    ProxyReportPayload,
    RequestProxyPayload,
    ResumeWorkerPayload,
    ResultAckPayload,
    SyncConfigPayload,
    WorkerResultPayload,
    build_node_message,
    parse_node_message,
)
from src.models.config import AppConfig, DisplayConfig
from src.models.results import WindowResult
from src.models.worker import WorkerState
from src.workers.arena_worker import ArenaWorker

logger = logging.getLogger(__name__)


@dataclass
class LocalWorkerSlot:
    """Tracks a single ArenaWorker running on this node."""

    worker_id: int
    run_id: str
    worker: Optional[ArenaWorker] = None
    task: Optional[asyncio.Task] = None
    pause_event: asyncio.Event = field(default_factory=lambda: asyncio.Event())
    cancel_event: asyncio.Event = field(default_factory=lambda: asyncio.Event())
    epoch: int = 0
    proxy: Optional[Dict[str, Any]] = None
    work_payload: Optional[AssignWorkPayload] = None

    # Result buffering for ACK protocol
    pending_result: Optional[Dict[str, Any]] = None
    result_acked: bool = False

    def __post_init__(self) -> None:
        self.pause_event.set()  # Start unpaused


class NodeClient:
    """WebSocket client connecting a worker node to the coordinator.

    Handles:
    - Connection and reconnection with exponential backoff
    - Registration and config sync
    - Receiving work assignments and spawning ArenaWorkers
    - Forwarding events and results to the coordinator
    - Responding to pause/cancel/heartbeat commands
    """

    def __init__(
        self,
        coordinator_url: str,
        node_id: str,
        max_workers: int = 12,
        auth_token: str = "",
        display: Optional[NodeDisplayInfo] = None,
        headless: bool = False,
    ) -> None:
        self._coordinator_url = coordinator_url
        self._node_id = node_id
        self._max_workers = max_workers
        self._auth_token = auth_token
        self._display = display or NodeDisplayInfo()
        self._headless = headless

        self._ws: Optional[ClientConnection] = None
        self._config: Optional[AppConfig] = None
        self._browser_manager: Optional[BrowserManager] = None
        self._event_bus = EventBus()
        self._forwarder: Optional[EventForwarder] = None

        self._slots: Dict[int, LocalWorkerSlot] = {}  # worker_id -> slot
        self._running = False
        self._connected = False
        self._generation = 0  # Incremented on each reconnect

        # Unacked results buffer (survives reconnection)
        self._unacked_results: Dict[tuple, WorkerResultPayload] = {}

        # Reconnection backoff
        self._backoff_base = 1.0
        self._backoff_max = 30.0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def active_workers(self) -> int:
        return sum(
            1 for s in self._slots.values()
            if s.worker and not s.worker.state_machine.is_terminal
        )

    async def start(self) -> None:
        """Start the node client — connects and runs the message loop."""
        self._running = True
        logger.info("Node %s starting, coordinator: %s", self._node_id, self._coordinator_url)

        while self._running:
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Connection lost, reconnecting...", exc_info=True)

            if not self._running:
                break

            # Exponential backoff for reconnection
            delay = min(
                self._backoff_base * (2 ** min(self._generation, 5)),
                self._backoff_max,
            )
            logger.info("Reconnecting in %.1fs...", delay)
            await asyncio.sleep(delay)
            self._generation += 1

    async def stop(self) -> None:
        """Graceful shutdown — notify coordinator, close browsers, disconnect."""
        self._running = False
        logger.info("Node %s shutting down...", self._node_id)

        # Notify coordinator
        if self._ws and self._connected:
            try:
                buffered = [
                    v for v in self._unacked_results.values()
                ]
                payload = NodeShuttingDownPayload(
                    buffered_results=buffered,
                    reason="graceful_shutdown",
                )
                await self._send(
                    NodeMessageType.NODE_SHUTTING_DOWN, payload
                )
            except Exception:
                logger.debug("Failed to send shutdown notice", exc_info=True)

        # Cancel all worker tasks
        for slot in self._slots.values():
            if slot.task and not slot.task.done():
                slot.task.cancel()
        if self._slots:
            await asyncio.gather(
                *[s.task for s in self._slots.values() if s.task],
                return_exceptions=True,
            )

        # Close browsers
        if self._browser_manager:
            await self._browser_manager.close_all()

        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        if self._forwarder:
            self._forwarder.stop()

        logger.info("Node %s stopped.", self._node_id)

    async def _connect_and_run(self) -> None:
        """Establish connection, register, and run the message loop."""
        url = self._coordinator_url
        if self._auth_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={self._auth_token}"

        async with websockets.connect(
            url,
            max_size=67_108_864,  # 64MB, matching the coordinator
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            self._connected = True
            self._backoff_base = 1.0  # Reset backoff on successful connect
            logger.info("Connected to coordinator")

            try:
                await self._register()

                # Replay unacked results on reconnect
                if self._unacked_results:
                    logger.info(
                        "Replaying %d unacked results",
                        len(self._unacked_results),
                    )
                    await self.replay_unacked_results()

                # Resume paused workers on reconnect
                if self._generation > 0 and self._slots:
                    self._resume_all_workers()
                    logger.info("Resumed %d workers after reconnect", len(self._slots))

                await self._message_loop()
            finally:
                self._connected = False
                self._ws = None
                # Pause workers on disconnect (they'll resume on reconnect)
                self._pause_all_workers()

    async def _register(self) -> None:
        """Send registration message to coordinator."""
        payload = NodeRegisterPayload(
            node_id=self._node_id,
            max_workers=self._max_workers,
            display=self._display,
            platform="linux",
            headless=self._headless,
        )
        await self._send(NodeMessageType.NODE_REGISTER, payload)
        logger.info(
            "Registered: node_id=%s, max_workers=%d",
            self._node_id,
            self._max_workers,
        )

    async def _message_loop(self) -> None:
        """Main receive loop — dispatch messages to handlers."""
        async for raw_msg in self._ws:
            try:
                raw = json.loads(raw_msg)
                envelope, payload = parse_node_message(raw)
                await self._dispatch(envelope, payload)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from coordinator: %s", raw_msg[:200])
            except Exception:
                logger.error("Error handling message", exc_info=True)

    async def _dispatch(self, envelope: NodeMessage, payload: Any) -> None:
        """Route a message to the appropriate handler."""
        handlers = {
            NodeMessageType.SYNC_CONFIG: self._handle_sync_config,
            NodeMessageType.ASSIGN_WORK: self._handle_assign_work,
            NodeMessageType.CANCEL_WORKER: self._handle_cancel_worker,
            NodeMessageType.PAUSE_WORKER: self._handle_pause_worker,
            NodeMessageType.RESUME_WORKER: self._handle_resume_worker,
            NodeMessageType.HEARTBEAT_PING: self._handle_heartbeat_ping,
            NodeMessageType.RESULT_ACK: self._handle_result_ack,
            NodeMessageType.ASSIGN_PROXY: self._handle_assign_proxy,
            NodeMessageType.CANCEL_ALL: self._handle_cancel_all,
        }
        handler = handlers.get(envelope.msg_type)
        if handler:
            await handler(envelope, payload)
        else:
            logger.debug("Unhandled message type: %s", envelope.msg_type)

    # ──── Command Handlers ────

    async def _handle_sync_config(
        self, envelope: NodeMessage, payload: SyncConfigPayload
    ) -> None:
        """Apply configuration from coordinator."""
        self._config = AppConfig(**payload.config)
        # Override headless from node setting
        if self._headless:
            self._config.browser.headless = True

        # Load selectors from coordinator-provided YAML
        if payload.selectors_yaml:
            import yaml
            selectors_data = yaml.safe_load(payload.selectors_yaml)
            if isinstance(selectors_data, dict):
                SelectorRegistry._instance = SelectorRegistry(selectors_data)

        # Initialize BrowserManager if not yet done
        if not self._browser_manager:
            self._browser_manager = BrowserManager(self._config)
            await self._browser_manager.start()

        # Set up event forwarder
        if not self._forwarder:
            self._forwarder = EventForwarder(
                self._event_bus,
                self._send_raw,
                self._node_id,
                coalesce_ms=(
                    self._config.distributed.event_coalesce_ms
                    if self._config.distributed
                    else 100
                ),
            )
            self._forwarder.start()

        logger.info("Config synced from coordinator")

    async def _handle_assign_work(
        self, envelope: NodeMessage, payload: AssignWorkPayload
    ) -> None:
        """Create a worker and start executing the assigned work."""
        worker_id = payload.worker_id
        run_id = payload.run_id

        if worker_id in self._slots:
            # Cancel existing worker in this slot
            old_slot = self._slots[worker_id]
            if old_slot.task and not old_slot.task.done():
                old_slot.task.cancel()

        if len(self._slots) >= self._max_workers:
            logger.warning(
                "At capacity (%d workers), rejecting work for worker %d",
                self._max_workers,
                worker_id,
            )
            return

        # Create browser context
        display_override = None
        if payload.display_override:
            display_override = DisplayConfig(**payload.display_override)

        contexts = await self._browser_manager.create_contexts(
            count=1,
            display_override=display_override,
            incognito=payload.incognito,
            proxies=[payload.proxy] if payload.proxy else None,
            proxy_on_challenge=payload.proxy_on_challenge,
            windows_per_proxy=payload.windows_per_proxy,
            zoom_pct=payload.zoom_pct,
            run_id=f"{run_id}_w{worker_id}",
        )

        if not contexts:
            logger.error("Failed to create browser context for worker %d", worker_id)
            return

        context = contexts[0]

        # Create slot
        slot = LocalWorkerSlot(
            worker_id=worker_id,
            run_id=run_id,
            epoch=envelope.epoch,
            proxy=payload.proxy,
            work_payload=payload,
        )

        # Build callbacks
        async def recreator(idx: int):
            return await self._browser_manager.recreate_context(
                0, run_id=f"{run_id}_w{worker_id}"
            )

        def proxy_getter(idx: int) -> Optional[str]:
            return slot.proxy.get("server") if slot.proxy else None

        def proxy_success(idx: int) -> None:
            asyncio.ensure_future(self._report_proxy(
                slot.proxy.get("server", "") if slot.proxy else "",
                "mark_healthy",
                worker_id=worker_id,
            ))

        def proxy_failure(idx: int, reason: str) -> None:
            asyncio.ensure_future(self._report_proxy(
                slot.proxy.get("server", "") if slot.proxy else "",
                "mark_unhealthy",
                reason=reason,
                worker_id=worker_id,
            ))

        # Create ArenaWorker (UNCHANGED class)
        worker = ArenaWorker(
            worker_id=worker_id,
            context=context,
            config=self._config,
            event_bus=self._event_bus,
            context_recreator=recreator,
            proxy_getter=proxy_getter,
            proxy_success_reporter=proxy_success,
            proxy_failure_reporter=proxy_failure,
            run_id=run_id,
        )

        slot.worker = worker
        self._slots[worker_id] = slot

        # Launch worker task
        slot.task = asyncio.ensure_future(
            self._run_worker(slot, payload)
        )
        logger.info(
            "Worker %d assigned: run=%s batch=%d",
            worker_id,
            run_id,
            payload.batch_index,
        )

    async def _run_worker(
        self, slot: LocalWorkerSlot, work: AssignWorkPayload
    ) -> None:
        """Execute the full worker lifecycle for an assigned task."""
        worker = slot.worker
        try:
            # Navigate
            await worker.navigate_to_arena(
                clear_cookies=work.clear_cookies,
                zoom_pct=work.zoom_pct,
                pause_event=slot.pause_event,
            )

            # Handle turns (multi-turn) or single prompt
            if work.turns:
                await self._run_multi_turn(slot, work)
            else:
                await self._run_single_prompt(slot, work)

        except asyncio.CancelledError:
            await worker.cancel()
        except Exception as e:
            logger.error("Worker %d failed: %s", slot.worker_id, e, exc_info=True)

    async def _run_single_prompt(
        self, slot: LocalWorkerSlot, work: AssignWorkPayload
    ) -> None:
        """Execute a single prompt submission + poll cycle."""
        worker = slot.worker

        # System prompt first if provided
        if work.system_prompt:
            effective_system = work.system_prompt
            if work.combine_with_first:
                effective_system = f"{work.system_prompt}\n\n{work.prompt}"

            await worker.submit_prompt(
                prompt=effective_system if work.combine_with_first else work.system_prompt,
                pause_event=slot.pause_event,
            )
            result = await worker.poll_for_completion(
                pause_event=slot.pause_event,
            )
            if not work.combine_with_first:
                await worker.prepare_for_followup_prompt()

        # Main prompt (skip if combined with system prompt)
        if not (work.system_prompt and work.combine_with_first):
            prompt = work.prompt
            if work.prompts:
                # Single worker gets a specific prompt from the list
                prompt = work.prompts[0] if work.prompts else work.prompt

            await worker.prepare_prompt(
                prompt=prompt,
                model_a=work.model_a,
                model_b=work.model_b,
                pause_event=slot.pause_event,
                images=work.images,
            )
            await worker.submit_prepared_prompt(
                pause_event=slot.pause_event,
            )

            baseline = None
            if work.system_prompt and not work.combine_with_first:
                baseline = await worker.prepare_for_followup_prompt()
                # Actually we already submitted, so just use None
                baseline = None

            result = await worker.poll_for_completion(
                pause_event=slot.pause_event,
            )

        # Send result
        if worker.get_result():
            await self._send_worker_result(slot, worker.get_result())

    async def _run_multi_turn(
        self, slot: LocalWorkerSlot, work: AssignWorkPayload
    ) -> None:
        """Execute a multi-turn conversation."""
        worker = slot.worker
        baseline = None

        for turn_idx, turn in enumerate(work.turns):
            if turn_idx == 0:
                await worker.prepare_prompt(
                    prompt=turn["text"] if isinstance(turn, dict) else turn.text,
                    model_a=work.model_a,
                    model_b=work.model_b,
                    pause_event=slot.pause_event,
                    images=turn.get("images") if isinstance(turn, dict) else getattr(turn, "images", None),
                )
                await worker.submit_prepared_prompt(
                    pause_event=slot.pause_event,
                )
            else:
                turn_text = turn["text"] if isinstance(turn, dict) else turn.text
                turn_images = turn.get("images") if isinstance(turn, dict) else getattr(turn, "images", None)
                await worker.submit_prompt(
                    prompt=turn_text,
                    pause_event=slot.pause_event,
                    images=turn_images,
                )

            result = await worker.poll_for_completion(
                baseline_responses=baseline,
                pause_event=slot.pause_event,
            )

            # Send result for this turn
            if worker.get_result():
                wr = worker.get_result()
                wr.turn_index = turn_idx
                await self._send_worker_result(slot, wr)

            # Prepare for next turn
            if turn_idx < len(work.turns) - 1:
                baseline = await worker.prepare_for_followup_prompt()

    async def _send_worker_result(
        self, slot: LocalWorkerSlot, result: WindowResult
    ) -> None:
        """Send a completed result and buffer until ACKed."""
        result_dict = result.model_dump(mode="json")
        key = (slot.run_id, slot.worker_id, result.batch_index, result.turn_index)
        payload = WorkerResultPayload(
            run_id=slot.run_id,
            worker_id=slot.worker_id,
            batch_index=result.batch_index,
            turn_index=result.turn_index,
            result=result_dict,
        )
        self._unacked_results[key] = payload

        if self._forwarder:
            await self._forwarder.send_result(
                run_id=slot.run_id,
                worker_id=slot.worker_id,
                batch_index=result.batch_index,
                turn_index=result.turn_index,
                result_dict=result_dict,
            )

    async def _handle_cancel_worker(
        self, envelope: NodeMessage, payload: CancelWorkerPayload
    ) -> None:
        """Cancel a specific worker."""
        slot = self._slots.get(payload.worker_id)
        if slot and slot.worker:
            await slot.worker.cancel()
            if slot.task and not slot.task.done():
                slot.task.cancel()
            logger.info("Worker %d cancelled", payload.worker_id)

    async def _handle_cancel_all(
        self, envelope: NodeMessage, payload: CancelAllPayload
    ) -> None:
        """Cancel all workers for a run."""
        for slot in list(self._slots.values()):
            if slot.run_id == payload.run_id:
                if slot.worker:
                    await slot.worker.cancel()
                if slot.task and not slot.task.done():
                    slot.task.cancel()
        logger.info("All workers cancelled for run %s", payload.run_id)

    async def _handle_pause_worker(
        self, envelope: NodeMessage, payload: PauseWorkerPayload
    ) -> None:
        """Pause worker(s)."""
        if payload.worker_id is not None:
            slot = self._slots.get(payload.worker_id)
            if slot:
                slot.pause_event.clear()
                logger.debug("Worker %d paused", payload.worker_id)
        else:
            # Pause all workers for this run
            for slot in self._slots.values():
                if slot.run_id == payload.run_id:
                    slot.pause_event.clear()
            logger.debug("All workers paused for run %s", payload.run_id)

    async def _handle_resume_worker(
        self, envelope: NodeMessage, payload: ResumeWorkerPayload
    ) -> None:
        """Resume worker(s)."""
        if payload.worker_id is not None:
            slot = self._slots.get(payload.worker_id)
            if slot:
                slot.pause_event.set()
                logger.debug("Worker %d resumed", payload.worker_id)
        else:
            for slot in self._slots.values():
                if slot.run_id == payload.run_id:
                    slot.pause_event.set()
            logger.debug("All workers resumed for run %s", payload.run_id)

    async def _handle_heartbeat_ping(
        self, envelope: NodeMessage, payload: HeartbeatPingPayload
    ) -> None:
        """Respond to heartbeat ping."""
        mem_pct = None
        try:
            import psutil
            mem_pct = psutil.virtual_memory().percent
        except ImportError:
            pass
        except Exception:
            pass

        pong = HeartbeatPongPayload(
            active_workers=self.active_workers,
            memory_pct=mem_pct,
        )
        await self._send(NodeMessageType.HEARTBEAT_PONG, pong)

    async def _handle_result_ack(
        self, envelope: NodeMessage, payload: ResultAckPayload
    ) -> None:
        """Handle acknowledgement of a result — remove from buffer."""
        key = (payload.run_id, payload.worker_id, payload.batch_index, payload.turn_index)
        if key in self._unacked_results:
            del self._unacked_results[key]
            logger.debug("Result ACKed: worker=%d batch=%d", payload.worker_id, payload.batch_index)

        # Clean up completed worker slot
        slot = self._slots.get(payload.worker_id)
        if slot and slot.worker and slot.worker.state_machine.is_terminal:
            await self._cleanup_slot(payload.worker_id)

    async def _handle_assign_proxy(
        self, envelope: NodeMessage, payload: AssignProxyPayload
    ) -> None:
        """Receive a new proxy assignment for a worker."""
        slot = self._slots.get(payload.worker_id)
        if slot:
            slot.proxy = payload.proxy
            logger.debug("Worker %d assigned new proxy", payload.worker_id)

    # ──── Helpers ────

    def _pause_all_workers(self) -> None:
        """Pause all workers (called on disconnect)."""
        for slot in self._slots.values():
            slot.pause_event.clear()

    def _resume_all_workers(self) -> None:
        """Resume all workers (called on reconnect)."""
        for slot in self._slots.values():
            slot.pause_event.set()

    async def _cleanup_slot(self, worker_id: int) -> None:
        """Clean up a completed worker slot."""
        slot = self._slots.get(worker_id)
        if not slot:
            return

        # Close browser context
        run_key = f"{slot.run_id}_w{worker_id}"
        try:
            await self._browser_manager.close_contexts(run_id=run_key)
        except Exception:
            logger.debug("Error closing context for worker %d", worker_id, exc_info=True)

        del self._slots[worker_id]

    async def _report_proxy(
        self,
        server: str,
        action: str,
        reason: Optional[str] = None,
        worker_id: Optional[int] = None,
    ) -> None:
        """Report proxy health to coordinator."""
        if not server:
            return
        payload = ProxyReportPayload(
            server=server,
            action=action,
            reason=reason,
            worker_id=worker_id,
        )
        await self._send(NodeMessageType.PROXY_REPORT, payload)

    async def _request_new_proxy(
        self, run_id: str, worker_id: int, avoid_server: Optional[str] = None
    ) -> None:
        """Request a new proxy from coordinator (e.g. after challenge)."""
        payload = RequestProxyPayload(
            run_id=run_id,
            worker_id=worker_id,
            avoid_server=avoid_server,
        )
        await self._send(NodeMessageType.REQUEST_PROXY, payload)

    async def _send(self, msg_type: NodeMessageType, payload: Any) -> None:
        """Send a typed message to the coordinator."""
        msg = build_node_message(
            msg_type,
            payload,
            node_id=self._node_id,
            epoch=self._generation,
        )
        await self._send_raw(msg)

    async def _send_raw(self, data: str) -> None:
        """Send raw JSON string to coordinator."""
        if self._ws and self._connected:
            await self._ws.send(data)

    async def replay_unacked_results(self) -> None:
        """Replay any unacked results after reconnection."""
        for payload in list(self._unacked_results.values()):
            try:
                msg = build_node_message(
                    NodeMessageType.WORKER_RESULT,
                    payload,
                    node_id=self._node_id,
                    epoch=self._generation,
                )
                await self._send_raw(msg)
            except Exception:
                logger.warning("Failed to replay result", exc_info=True)
