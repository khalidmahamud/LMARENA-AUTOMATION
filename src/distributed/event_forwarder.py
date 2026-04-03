"""EventForwarder — bridges a local EventBus to the coordinator over WebSocket.

Subscribes to all events on a local EventBus, serializes them into
NodeMessage envelopes, and queues them for transmission. Supports
coalescing of rapid state-change events to reduce network chatter.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional

from src.core.events import Event, EventBus, EventType
from src.distributed.protocol import (
    NodeMessageType,
    WorkerEventPayload,
    WorkerResultPayload,
    build_node_message,
)

logger = logging.getLogger(__name__)

# Type alias for the send function provided by NodeClient
SendFn = Callable[[str], Coroutine[Any, Any, None]]

# Event types that should be sent immediately (no coalescing)
IMMEDIATE_EVENTS = frozenset({
    EventType.WORKER_PARTIAL_RESULT,
    EventType.WORKER_COMPLETE,
    EventType.WORKER_ERROR,
    EventType.RUN_COMPLETE,
    EventType.RUN_CANCELLED,
    EventType.CHALLENGE_DETECTED,
    EventType.CHALLENGE_RESOLVED,
})


class EventForwarder:
    """Subscribes to a local EventBus and forwards events to the coordinator.

    State-change events are coalesced within a configurable window (default 100ms)
    to avoid flooding the network. Partial results and terminal events are sent
    immediately.
    """

    def __init__(
        self,
        event_bus: EventBus,
        send_fn: SendFn,
        node_id: str,
        epoch: int = 0,
        coalesce_ms: int = 100,
    ) -> None:
        self._event_bus = event_bus
        self._send_fn = send_fn
        self._node_id = node_id
        self._epoch = epoch
        self._coalesce_ms = coalesce_ms

        # Coalescing state: keyed by (event_type, worker_id)
        self._pending: Dict[tuple, Event] = {}
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

        event_bus.subscribe_all(self._on_event)

    @property
    def epoch(self) -> int:
        return self._epoch

    @epoch.setter
    def epoch(self, value: int) -> None:
        self._epoch = value

    def start(self) -> None:
        """Start the coalescing flush loop."""
        if self._running:
            return
        self._running = True
        if self._coalesce_ms > 0:
            self._flush_task = asyncio.ensure_future(self._flush_loop())

    def stop(self) -> None:
        """Stop the flush loop and send any pending events."""
        self._running = False
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

    async def _on_event(self, event: Event) -> None:
        """Handle an event from the local EventBus."""
        if event.type in IMMEDIATE_EVENTS:
            await self._send_event(event)
        elif self._coalesce_ms > 0:
            # Coalesce: keep only the latest event per (type, worker_id)
            key = (event.type, event.worker_id)
            self._pending[key] = event
        else:
            # No coalescing configured
            await self._send_event(event)

    async def _flush_loop(self) -> None:
        """Periodically flush coalesced events."""
        interval = self._coalesce_ms / 1000.0
        try:
            while self._running:
                await asyncio.sleep(interval)
                await self._flush_pending()
        except asyncio.CancelledError:
            # Final flush on shutdown
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        """Send all pending coalesced events."""
        if not self._pending:
            return
        pending = self._pending.copy()
        self._pending.clear()
        for event in pending.values():
            await self._send_event(event)

    async def _send_event(self, event: Event) -> None:
        """Serialize and send a single event to the coordinator."""
        try:
            payload = WorkerEventPayload(
                event_type=event.type.value,
                worker_id=event.worker_id,
                run_id=event.run_id,
                data=event.data,
                event_timestamp=event.timestamp,
            )
            msg = build_node_message(
                NodeMessageType.WORKER_EVENT,
                payload,
                node_id=self._node_id,
                epoch=self._epoch,
            )
            await self._send_fn(msg)
        except Exception:
            logger.warning(
                "Failed to forward event %s for worker %s",
                event.type,
                event.worker_id,
                exc_info=True,
            )

    async def send_result(
        self,
        run_id: str,
        worker_id: int,
        batch_index: int,
        turn_index: int,
        result_dict: Dict[str, Any],
    ) -> None:
        """Send a completed WindowResult to the coordinator (requires ACK)."""
        try:
            payload = WorkerResultPayload(
                run_id=run_id,
                worker_id=worker_id,
                batch_index=batch_index,
                turn_index=turn_index,
                result=result_dict,
            )
            msg = build_node_message(
                NodeMessageType.WORKER_RESULT,
                payload,
                node_id=self._node_id,
                epoch=self._epoch,
            )
            await self._send_fn(msg)
        except Exception:
            logger.error(
                "Failed to send result for worker %d batch %d",
                worker_id,
                batch_index,
                exc_info=True,
            )
