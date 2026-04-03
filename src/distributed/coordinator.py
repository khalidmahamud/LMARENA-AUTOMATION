"""Coordinator — manages connected worker nodes.

Provides:
- NodeRegistry: tracks node health, capacity, and WebSocket connections
- NodeConnectionHandler: accepts and manages node WebSocket connections
- Heartbeat monitor: detects dead nodes
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
import math

from fastapi import WebSocket, WebSocketDisconnect

from src.core.events import Event, EventBus, EventType
from src.distributed.protocol import (
    AssignProxyPayload,
    CancelAllPayload,
    CancelWorkerPayload,
    HeartbeatPingPayload,
    HeartbeatPongPayload,
    NodeDisplayInfo,
    NodeMessage,
    NodeMessageType,
    NodeRegisterPayload,
    NodeShuttingDownPayload,
    NodeState,
    PauseWorkerPayload,
    ProxyReportPayload,
    RequestProxyPayload,
    ResultAckPayload,
    ResumeWorkerPayload,
    SyncConfigPayload,
    WorkerEventPayload,
    WorkerResultPayload,
    build_node_message,
    parse_node_message,
)
from src.models.config import AppConfig

logger = logging.getLogger(__name__)


def _worker_key(run_id: str, worker_id: int) -> str:
    return f"{run_id}::{worker_id}"


def _split_worker_key(key: str) -> tuple[str, int]:
    run_id, worker_id = key.rsplit("::", 1)
    return run_id, int(worker_id)


@dataclass
class NodeInfo:
    """Coordinator-side state for a connected worker node."""

    node_id: str
    ws: Optional[WebSocket] = None
    state: NodeState = NodeState.HEALTHY
    max_workers: int = 12
    allocated_workers: int = 0
    display: NodeDisplayInfo = field(default_factory=NodeDisplayInfo)
    platform: str = "linux"
    headless: bool = False
    generation: int = 0
    last_heartbeat: float = field(default_factory=time.monotonic)
    missed_heartbeats: int = 0
    active_worker_ids: Set[str] = field(default_factory=set)

    # Latest heartbeat info
    memory_pct: Optional[float] = None

    @property
    def available_capacity(self) -> float:
        if self.max_workers <= 0:
            return math.inf
        return max(0, self.max_workers - self.allocated_workers)

    @property
    def is_alive(self) -> bool:
        return self.state in (NodeState.HEALTHY, NodeState.SUSPECT)


class NodeRegistry:
    """Tracks all connected worker nodes and their state."""

    def __init__(self) -> None:
        self._nodes: Dict[str, NodeInfo] = {}

    def register(self, node_id: str, info: NodeInfo) -> None:
        existing = self._nodes.get(node_id)
        if existing:
            info.generation = existing.generation + 1
        self._nodes[node_id] = info
        logger.info(
            "Node registered: %s (max_workers=%d, gen=%d)",
            node_id,
            info.max_workers,
            info.generation,
        )

    def unregister(self, node_id: str) -> Optional[NodeInfo]:
        return self._nodes.pop(node_id, None)

    def get(self, node_id: str) -> Optional[NodeInfo]:
        return self._nodes.get(node_id)

    def get_all(self) -> Dict[str, NodeInfo]:
        return dict(self._nodes)

    def get_healthy(self) -> List[NodeInfo]:
        return [n for n in self._nodes.values() if n.is_alive]

    @property
    def total_capacity(self) -> int:
        return sum(n.available_capacity for n in self._nodes.values() if n.is_alive)

    @property
    def total_nodes(self) -> int:
        return len(self._nodes)

    @property
    def healthy_nodes(self) -> int:
        return sum(1 for n in self._nodes.values() if n.is_alive)

    def allocate_worker(self, node_id: str, run_id: str, worker_id: int) -> bool:
        """Mark a worker as allocated on a node. Returns False if no capacity."""
        node = self._nodes.get(node_id)
        if not node or not node.is_alive or node.available_capacity <= 0:
            return False
        node.allocated_workers += 1
        node.active_worker_ids.add(_worker_key(run_id, worker_id))
        return True

    def deallocate_worker(self, node_id: str, run_id: str, worker_id: int) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.allocated_workers = max(0, node.allocated_workers - 1)
            node.active_worker_ids.discard(_worker_key(run_id, worker_id))

    def mark_node_state(self, node_id: str, state: NodeState) -> None:
        node = self._nodes.get(node_id)
        if node:
            old = node.state
            node.state = state
            if old != state:
                logger.info("Node %s: %s -> %s", node_id, old.value, state.value)


class NodeConnectionHandler:
    """Handles WebSocket connections from worker nodes.

    Runs on the coordinator side, accepting connections on a dedicated
    endpoint (e.g. /node-ws). Routes incoming messages to handlers and
    publishes node lifecycle events to the EventBus.
    """

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        registry: NodeRegistry,
        proxy_pool: Optional[Any] = None,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._registry = registry
        self._proxy_pool = proxy_pool
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Callback for when a worker result is received
        self._on_worker_result: Optional[
            Callable[[str, WorkerResultPayload], Coroutine[Any, Any, None]]
        ] = None
        self._on_worker_event: Optional[
            Callable[[str, WorkerEventPayload], Coroutine[Any, Any, None]]
        ] = None
        self._on_proxy_report: Optional[
            Callable[[ProxyReportPayload], Coroutine[Any, Any, None]]
        ] = None
        self._on_request_proxy: Optional[
            Callable[[str, RequestProxyPayload], Coroutine[Any, Any, None]]
        ] = None
        self._on_node_dead: Optional[
            Callable[[str, NodeInfo], Coroutine[Any, Any, None]]
        ] = None
        self._run_worker_result_handlers: Dict[
            str, Callable[[str, WorkerResultPayload], Coroutine[Any, Any, None]]
        ] = {}
        self._run_worker_event_handlers: Dict[
            str, Callable[[str, WorkerEventPayload], Coroutine[Any, Any, None]]
        ] = {}
        self._run_request_proxy_handlers: Dict[
            str, Callable[[str, RequestProxyPayload], Coroutine[Any, Any, None]]
        ] = {}
        self._run_node_dead_handlers: Dict[
            str, Callable[[str, NodeInfo], Coroutine[Any, Any, None]]
        ] = {}

    def set_callbacks(
        self,
        on_worker_result=None,
        on_worker_event=None,
        on_proxy_report=None,
        on_request_proxy=None,
        on_node_dead=None,
    ) -> None:
        """Set callback functions for distributed orchestrator integration."""
        if on_worker_result:
            self._on_worker_result = on_worker_result
        if on_worker_event:
            self._on_worker_event = on_worker_event
        if on_proxy_report:
            self._on_proxy_report = on_proxy_report
        if on_request_proxy:
            self._on_request_proxy = on_request_proxy
        if on_node_dead:
            self._on_node_dead = on_node_dead

    def register_run_callbacks(
        self,
        run_id: str,
        *,
        on_worker_result=None,
        on_worker_event=None,
        on_request_proxy=None,
        on_node_dead=None,
    ) -> None:
        """Register run-scoped callbacks for concurrent distributed runs."""
        if on_worker_result:
            self._run_worker_result_handlers[run_id] = on_worker_result
        if on_worker_event:
            self._run_worker_event_handlers[run_id] = on_worker_event
        if on_request_proxy:
            self._run_request_proxy_handlers[run_id] = on_request_proxy
        if on_node_dead:
            self._run_node_dead_handlers[run_id] = on_node_dead

    def unregister_run_callbacks(self, run_id: str) -> None:
        self._run_worker_result_handlers.pop(run_id, None)
        self._run_worker_event_handlers.pop(run_id, None)
        self._run_request_proxy_handlers.pop(run_id, None)
        self._run_node_dead_handlers.pop(run_id, None)

    def start_heartbeat_monitor(self) -> None:
        """Start the background heartbeat monitor task."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())

    def stop_heartbeat_monitor(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

    async def handle_node_connection(self, ws: WebSocket, token: str = "") -> None:
        """Handle a single node WebSocket connection lifecycle."""
        # Validate auth token
        dist_config = self._config.distributed
        if dist_config and dist_config.auth_token != "change-me":
            if token != dist_config.auth_token:
                await ws.close(code=4001, reason="Invalid auth token")
                logger.warning("Node connection rejected: invalid token")
                return

        await ws.accept()
        node_id = ""

        try:
            async for raw_msg in ws.iter_text():
                try:
                    raw = json.loads(raw_msg)
                    envelope, payload = parse_node_message(raw)

                    if envelope.msg_type == NodeMessageType.NODE_REGISTER:
                        node_id = await self._handle_register(ws, payload)
                    elif node_id:
                        await self._dispatch(node_id, envelope, payload)
                    else:
                        logger.warning("Message before registration: %s", envelope.msg_type)

                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from node: %s", raw_msg[:200])
                except Exception:
                    logger.error("Error handling node message", exc_info=True)

        except WebSocketDisconnect:
            pass
        except Exception:
            logger.warning("Node connection error for %s", node_id, exc_info=True)
        finally:
            if node_id:
                await self._handle_disconnect(node_id)

    async def _handle_register(
        self, ws: WebSocket, payload: NodeRegisterPayload
    ) -> str:
        """Process node registration."""
        node_id = payload.node_id

        info = NodeInfo(
            node_id=node_id,
            ws=ws,
            max_workers=payload.max_workers,
            display=payload.display,
            platform=payload.platform,
            headless=payload.headless,
            last_heartbeat=time.monotonic(),
        )
        self._registry.register(node_id, info)

        # Send config sync
        selectors_yaml = ""
        selectors_path = Path("config/selectors.yaml")
        if selectors_path.exists():
            selectors_yaml = selectors_path.read_text()

        sync_payload = SyncConfigPayload(
            config=self._config.model_dump(mode="json"),
            selectors_yaml=selectors_yaml,
        )
        await self._send_to_node(
            ws, NodeMessageType.SYNC_CONFIG, sync_payload, node_id=node_id
        )

        # Publish node online event
        await self._event_bus.publish(Event(
            type=EventType.NODE_ONLINE,
            data={
                "node_id": node_id,
                "max_workers": payload.max_workers,
                "platform": payload.platform,
            },
        ))

        logger.info("Node %s registered and synced", node_id)
        return node_id

    async def _handle_disconnect(self, node_id: str) -> None:
        """Handle node disconnection."""
        node = self._registry.get(node_id)
        affected_worker_keys = list(node.active_worker_ids) if node else []
        affected_workers = [
            worker_id for _, worker_id in map(_split_worker_key, affected_worker_keys)
        ]

        self._registry.mark_node_state(node_id, NodeState.DEAD)

        # Publish offline event
        await self._event_bus.publish(Event(
            type=EventType.NODE_OFFLINE,
            data={
                "node_id": node_id,
                "affected_workers": affected_workers,
                "reason": "disconnected",
            },
        ))

        # Notify orchestrator
        if node:
            affected_runs = {run_id for run_id, _ in map(_split_worker_key, affected_worker_keys)}
            for run_id in affected_runs:
                handler = self._run_node_dead_handlers.get(run_id)
                if handler:
                    await handler(node_id, node)
            if self._on_node_dead:
                await self._on_node_dead(node_id, node)

        logger.warning(
            "Node %s disconnected, %d workers affected",
            node_id,
            len(affected_workers),
        )

    async def _dispatch(
        self, node_id: str, envelope: NodeMessage, payload: Any
    ) -> None:
        """Route a message from a registered node to the appropriate handler."""
        handlers = {
            NodeMessageType.WORKER_EVENT: self._handle_worker_event,
            NodeMessageType.WORKER_RESULT: self._handle_worker_result,
            NodeMessageType.HEARTBEAT_PONG: self._handle_heartbeat_pong,
            NodeMessageType.PROXY_REPORT: self._handle_proxy_report,
            NodeMessageType.NODE_SHUTTING_DOWN: self._handle_shutting_down,
            NodeMessageType.REQUEST_PROXY: self._handle_request_proxy,
        }
        handler = handlers.get(envelope.msg_type)
        if handler:
            await handler(node_id, envelope, payload)

    async def _handle_worker_event(
        self, node_id: str, envelope: NodeMessage, payload: WorkerEventPayload
    ) -> None:
        """Forward a worker event to the EventBus and orchestrator."""
        # Republish as a local event so WsBroadcaster picks it up
        try:
            event_type = EventType(payload.event_type)
        except ValueError:
            logger.debug("Unknown event type: %s", payload.event_type)
            return

        # Inject node_id into event data
        data = dict(payload.data)
        data["node_id"] = node_id

        event = Event(
            type=event_type,
            worker_id=payload.worker_id,
            run_id=payload.run_id,
            data=data,
            timestamp=payload.event_timestamp,
        )
        await self._event_bus.publish(event)

        handler = self._run_worker_event_handlers.get(payload.run_id or "")
        if handler:
            await handler(node_id, payload)
        elif self._on_worker_event:
            await self._on_worker_event(node_id, payload)

    async def _handle_worker_result(
        self, node_id: str, envelope: NodeMessage, payload: WorkerResultPayload
    ) -> None:
        """Process a completed worker result — ACK and forward to orchestrator."""
        # Send ACK
        node = self._registry.get(node_id)
        if node and node.ws:
            ack = ResultAckPayload(
                run_id=payload.run_id,
                worker_id=payload.worker_id,
                batch_index=payload.batch_index,
                turn_index=payload.turn_index,
            )
            await self._send_to_node(
                node.ws, NodeMessageType.RESULT_ACK, ack, node_id=node_id
            )

        # Forward to orchestrator
        handler = self._run_worker_result_handlers.get(payload.run_id)
        if handler:
            await handler(node_id, payload)
        elif self._on_worker_result:
            await self._on_worker_result(node_id, payload)

        logger.debug(
            "Result from %s: worker=%d batch=%d",
            node_id,
            payload.worker_id,
            payload.batch_index,
        )

    async def _handle_heartbeat_pong(
        self, node_id: str, envelope: NodeMessage, payload: HeartbeatPongPayload
    ) -> None:
        """Process heartbeat response."""
        node = self._registry.get(node_id)
        if node:
            node.last_heartbeat = time.monotonic()
            node.missed_heartbeats = 0
            node.memory_pct = payload.memory_pct
            if node.state == NodeState.SUSPECT:
                self._registry.mark_node_state(node_id, NodeState.HEALTHY)

    async def _handle_proxy_report(
        self, node_id: str, envelope: NodeMessage, payload: ProxyReportPayload
    ) -> None:
        """Forward proxy health report to the centralized proxy pool."""
        if self._proxy_pool:
            if payload.action == "mark_healthy":
                self._proxy_pool.mark_healthy(payload.server)
            elif payload.action == "mark_unhealthy":
                self._proxy_pool.mark_unhealthy(payload.server)

        if self._on_proxy_report:
            await self._on_proxy_report(payload)

    async def _handle_shutting_down(
        self, node_id: str, envelope: NodeMessage, payload: NodeShuttingDownPayload
    ) -> None:
        """Handle graceful node shutdown."""
        logger.info("Node %s shutting down: %s", node_id, payload.reason)

        # Process any buffered results
        for result_payload in payload.buffered_results:
            handler = self._run_worker_result_handlers.get(result_payload.run_id)
            if handler:
                await handler(node_id, result_payload)
            elif self._on_worker_result:
                await self._on_worker_result(node_id, result_payload)

        self._registry.mark_node_state(node_id, NodeState.SHUTTING_DOWN)

    async def _handle_request_proxy(
        self, node_id: str, envelope: NodeMessage, payload: RequestProxyPayload
    ) -> None:
        """Handle proxy request from node (e.g. after challenge)."""
        handler = self._run_request_proxy_handlers.get(payload.run_id)
        if handler:
            await handler(node_id, payload)
        elif self._on_request_proxy:
            await self._on_request_proxy(node_id, payload)

    # ──── Heartbeat Monitor ────

    async def _heartbeat_loop(self) -> None:
        """Periodically ping all nodes and detect dead ones."""
        dist_config = self._config.distributed
        interval = dist_config.heartbeat_interval_seconds if dist_config else 5.0
        max_missed = dist_config.heartbeat_timeout_missed if dist_config else 3

        try:
            while True:
                await asyncio.sleep(interval)
                await self._send_heartbeats()
                await self._check_heartbeats(max_missed)
        except asyncio.CancelledError:
            pass

    async def _send_heartbeats(self) -> None:
        """Send heartbeat ping to all connected nodes."""
        ping = HeartbeatPingPayload()
        for node_id, node in self._registry.get_all().items():
            if node.ws and node.is_alive:
                try:
                    await self._send_to_node(
                        node.ws,
                        NodeMessageType.HEARTBEAT_PING,
                        ping,
                        node_id=node_id,
                    )
                except Exception:
                    logger.debug("Failed to ping node %s", node_id)

    async def _check_heartbeats(self, max_missed: int) -> None:
        """Check for nodes that have missed too many heartbeats."""
        dist_config = self._config.distributed
        interval = dist_config.heartbeat_interval_seconds if dist_config else 5.0
        now = time.monotonic()

        for node_id, node in self._registry.get_all().items():
            if not node.is_alive:
                continue

            elapsed = now - node.last_heartbeat
            missed = int(elapsed / interval)
            node.missed_heartbeats = missed

            if missed >= max_missed and node.state != NodeState.DEAD:
                logger.warning(
                    "Node %s missed %d heartbeats — declaring DEAD",
                    node_id,
                    missed,
                )
                self._registry.mark_node_state(node_id, NodeState.DEAD)

                # Publish offline event
                await self._event_bus.publish(Event(
                    type=EventType.NODE_OFFLINE,
                    data={
                        "node_id": node_id,
                        "affected_workers": [
                            worker_id
                            for _, worker_id in map(
                                _split_worker_key, list(node.active_worker_ids)
                            )
                        ],
                        "reason": f"heartbeat_timeout ({missed} missed)",
                    },
                ))

                affected_runs = {
                    run_id
                    for run_id, _ in map(_split_worker_key, list(node.active_worker_ids))
                }
                for run_id in affected_runs:
                    handler = self._run_node_dead_handlers.get(run_id)
                    if handler:
                        await handler(node_id, node)
                if self._on_node_dead:
                    await self._on_node_dead(node_id, node)

            elif missed >= 1 and node.state == NodeState.HEALTHY:
                self._registry.mark_node_state(node_id, NodeState.SUSPECT)

    # ──── Send helpers ────

    async def _send_to_node(
        self,
        ws: WebSocket,
        msg_type: NodeMessageType,
        payload: Any,
        *,
        node_id: str = "",
        epoch: int = 0,
    ) -> None:
        """Send a message to a specific node."""
        msg = build_node_message(
            msg_type, payload, node_id=node_id, epoch=epoch
        )
        await ws.send_text(msg)

    async def send_to_node_by_id(
        self,
        node_id: str,
        msg_type: NodeMessageType,
        payload: Any,
        *,
        epoch: int = 0,
    ) -> bool:
        """Send a message to a node by its ID. Returns False if node not found."""
        node = self._registry.get(node_id)
        if not node or not node.ws:
            return False
        try:
            await self._send_to_node(
                node.ws, msg_type, payload, node_id=node_id, epoch=epoch
            )
            return True
        except Exception:
            logger.warning("Failed to send to node %s", node_id)
            return False
