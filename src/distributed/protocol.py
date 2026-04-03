"""Distributed protocol message definitions.

All messages exchanged between coordinator and worker nodes over WebSocket.
Each message is a Pydantic model that can be serialized to/from JSON.

Message flow:
  Node → Coordinator: node_register, worker_event, worker_result,
                      heartbeat_pong, proxy_report, node_shutting_down
  Coordinator → Node: sync_config, assign_work, result_ack,
                      cancel_worker, pause_worker, resume_worker,
                      heartbeat_ping, request_proxy
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field


# ──── Envelope ────


class NodeMessageType(str, Enum):
    """All message types in the coordinator ↔ node protocol."""

    # Node → Coordinator
    NODE_REGISTER = "node_register"
    WORKER_EVENT = "worker_event"
    WORKER_RESULT = "worker_result"
    HEARTBEAT_PONG = "heartbeat_pong"
    PROXY_REPORT = "proxy_report"
    NODE_SHUTTING_DOWN = "node_shutting_down"
    REQUEST_PROXY = "request_proxy"

    # Coordinator → Node
    SYNC_CONFIG = "sync_config"
    ASSIGN_WORK = "assign_work"
    RESULT_ACK = "result_ack"
    CANCEL_WORKER = "cancel_worker"
    PAUSE_WORKER = "pause_worker"
    RESUME_WORKER = "resume_worker"
    HEARTBEAT_PING = "heartbeat_ping"
    ASSIGN_PROXY = "assign_proxy"
    CANCEL_ALL = "cancel_all"


class NodeMessage(BaseModel):
    """Envelope for all coordinator ↔ node messages."""

    msg_id: str = Field(default_factory=lambda: uuid4().hex)
    msg_type: NodeMessageType
    node_id: str = ""
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    epoch: int = 0  # Assignment epoch for fencing
    payload: Dict[str, Any] = Field(default_factory=dict)


# ──── Node → Coordinator Payloads ────


class NodeDisplayInfo(BaseModel):
    """Display configuration reported by a worker node."""

    monitor_count: int = Field(default=1, ge=1, le=8)
    monitor_width: int = Field(default=1920, ge=800, le=7680)
    monitor_height: int = Field(default=1080, ge=600, le=4320)
    taskbar_height: int = Field(default=40, ge=0, le=200)
    margin: int = Field(default=0, ge=0, le=50)
    border_offset: int = Field(default=7, ge=0, le=20)


class NodeRegisterPayload(BaseModel):
    """Sent by node on connection to register with coordinator."""

    node_id: str
    max_workers: int = Field(default=12, ge=0, le=50)
    display: NodeDisplayInfo = Field(default_factory=NodeDisplayInfo)
    platform: str = "linux"
    version: str = "1.0.0"
    headless: bool = False


class WorkerEventPayload(BaseModel):
    """Wraps an internal Event for network transmission."""

    event_type: str  # EventType value e.g. "worker.state_changed"
    worker_id: Optional[int] = None
    run_id: Optional[str] = None
    data: Dict[str, Any] = Field(default_factory=dict)
    event_timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class WorkerResultPayload(BaseModel):
    """Completed WindowResult from a worker, sent for ACK."""

    run_id: str
    worker_id: int
    batch_index: int = 0
    turn_index: int = 0
    result: Dict[str, Any] = Field(default_factory=dict)  # WindowResult.model_dump()


class HeartbeatPongPayload(BaseModel):
    """Node's response to a heartbeat ping."""

    active_workers: int = 0
    memory_pct: Optional[float] = None  # 0-100, optional resource reporting


class ProxyReportPayload(BaseModel):
    """Proxy health report from node to coordinator."""

    server: str
    action: Literal["mark_healthy", "mark_unhealthy"]
    reason: Optional[str] = None
    worker_id: Optional[int] = None


class NodeShuttingDownPayload(BaseModel):
    """Sent by node during graceful shutdown."""

    buffered_results: List[WorkerResultPayload] = Field(default_factory=list)
    reason: str = "shutdown"


class RequestProxyPayload(BaseModel):
    """Node requests a new proxy assignment (e.g. after challenge)."""

    run_id: str
    worker_id: int
    avoid_server: Optional[str] = None  # Current proxy to avoid


# ──── Coordinator → Node Payloads ────


class SyncConfigPayload(BaseModel):
    """Full configuration push to node after registration."""

    config: Dict[str, Any] = Field(default_factory=dict)  # AppConfig.model_dump()
    selectors_yaml: str = ""
    proxies: List[Dict[str, Any]] = Field(default_factory=list)


class AssignWorkPayload(BaseModel):
    """Assign a worker slot to a node."""

    run_id: str
    worker_id: int
    batch_index: int = 0
    prompt: str = ""
    prompts: Optional[List[str]] = None
    turns: Optional[List[Dict[str, Any]]] = None  # List of PromptTurn dicts
    system_prompt: str = ""
    combine_with_first: bool = False
    model_a: Optional[str] = None
    model_b: Optional[str] = None
    retain_output: str = "both"
    images: Optional[List[Dict[str, Any]]] = None  # List of ImagePayload dicts
    simultaneous_start: bool = False
    proxy: Optional[Dict[str, Any]] = None  # Playwright proxy dict
    clear_cookies: bool = False
    incognito: bool = False
    zoom_pct: int = 100
    proxy_on_challenge: bool = False
    windows_per_proxy: int = 4
    submission_gap_seconds: Optional[float] = None
    submit_after_seconds: Optional[float] = None
    # Display tile for this specific worker
    display_override: Optional[Dict[str, Any]] = None


class ResultAckPayload(BaseModel):
    """Acknowledge receipt of a worker result."""

    run_id: str
    worker_id: int
    batch_index: int = 0
    turn_index: int = 0


class CancelWorkerPayload(BaseModel):
    """Cancel a specific worker on a node."""

    run_id: str
    worker_id: int


class PauseWorkerPayload(BaseModel):
    """Pause a specific worker (or all workers for a run)."""

    run_id: str
    worker_id: Optional[int] = None  # None = pause all workers for this run


class ResumeWorkerPayload(BaseModel):
    """Resume a specific worker (or all workers for a run)."""

    run_id: str
    worker_id: Optional[int] = None  # None = resume all workers for this run


class HeartbeatPingPayload(BaseModel):
    """Coordinator's heartbeat ping."""

    coordinator_time: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class AssignProxyPayload(BaseModel):
    """Assign a new proxy to a worker (response to request_proxy)."""

    run_id: str
    worker_id: int
    proxy: Optional[Dict[str, Any]] = None  # Playwright proxy dict, None if exhausted


class CancelAllPayload(BaseModel):
    """Cancel all workers for a run on this node."""

    run_id: str


# ──── Typed payload unions (for deserialization convenience) ────

NodeToCoordinatorPayload = Union[
    NodeRegisterPayload,
    WorkerEventPayload,
    WorkerResultPayload,
    HeartbeatPongPayload,
    ProxyReportPayload,
    NodeShuttingDownPayload,
    RequestProxyPayload,
]

CoordinatorToNodePayload = Union[
    SyncConfigPayload,
    AssignWorkPayload,
    ResultAckPayload,
    CancelWorkerPayload,
    PauseWorkerPayload,
    ResumeWorkerPayload,
    HeartbeatPingPayload,
    AssignProxyPayload,
    CancelAllPayload,
]

# ──── Payload type mapping for deserialization ────

PAYLOAD_TYPE_MAP: Dict[NodeMessageType, type] = {
    # Node → Coordinator
    NodeMessageType.NODE_REGISTER: NodeRegisterPayload,
    NodeMessageType.WORKER_EVENT: WorkerEventPayload,
    NodeMessageType.WORKER_RESULT: WorkerResultPayload,
    NodeMessageType.HEARTBEAT_PONG: HeartbeatPongPayload,
    NodeMessageType.PROXY_REPORT: ProxyReportPayload,
    NodeMessageType.NODE_SHUTTING_DOWN: NodeShuttingDownPayload,
    NodeMessageType.REQUEST_PROXY: RequestProxyPayload,
    # Coordinator → Node
    NodeMessageType.SYNC_CONFIG: SyncConfigPayload,
    NodeMessageType.ASSIGN_WORK: AssignWorkPayload,
    NodeMessageType.RESULT_ACK: ResultAckPayload,
    NodeMessageType.CANCEL_WORKER: CancelWorkerPayload,
    NodeMessageType.PAUSE_WORKER: PauseWorkerPayload,
    NodeMessageType.RESUME_WORKER: ResumeWorkerPayload,
    NodeMessageType.HEARTBEAT_PING: HeartbeatPingPayload,
    NodeMessageType.ASSIGN_PROXY: AssignProxyPayload,
    NodeMessageType.CANCEL_ALL: CancelAllPayload,
}


def parse_node_message(raw: Dict[str, Any]) -> tuple[NodeMessage, Any]:
    """Parse a raw dict into a NodeMessage envelope + typed payload.

    Returns:
        (envelope, typed_payload) where typed_payload is the appropriate
        Pydantic model for the message type, or the raw dict if unknown.
    """
    envelope = NodeMessage(**raw)
    payload_cls = PAYLOAD_TYPE_MAP.get(envelope.msg_type)
    if payload_cls is not None:
        typed_payload = payload_cls(**envelope.payload)
    else:
        typed_payload = envelope.payload
    return envelope, typed_payload


def build_node_message(
    msg_type: NodeMessageType,
    payload: BaseModel,
    *,
    node_id: str = "",
    epoch: int = 0,
) -> str:
    """Build a NodeMessage JSON string from a typed payload.

    Returns:
        JSON string ready to send over WebSocket.
    """
    envelope = NodeMessage(
        msg_type=msg_type,
        node_id=node_id,
        epoch=epoch,
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json()


# ──── Node state enum (coordinator-side tracking) ────


class NodeState(str, Enum):
    """Health state of a worker node as tracked by the coordinator."""

    HEALTHY = "healthy"
    SUSPECT = "suspect"
    DEAD = "dead"
    SHUTTING_DOWN = "shutting_down"
