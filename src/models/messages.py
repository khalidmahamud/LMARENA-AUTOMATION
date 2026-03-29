from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ──── Inbound (GUI → Backend) ────


class StartRunRequest(BaseModel):
    type: Literal["start_run"] = "start_run"
    prompt: str = Field(..., min_length=1, max_length=50_000)
    window_count: int = Field(default=2, ge=1, le=12)
    submission_gap_seconds: Optional[float] = Field(default=None, ge=5.0)
    model: Optional[str] = None


class StopRunRequest(BaseModel):
    type: Literal["stop_run"] = "stop_run"


class PingRequest(BaseModel):
    type: Literal["ping"] = "ping"


InboundMessage = Union[StartRunRequest, StopRunRequest, PingRequest]


# ──── Outbound (Backend → GUI) ────


class WorkerUpdateMessage(BaseModel):
    type: Literal["worker_update"] = "worker_update"
    worker_id: int
    state: str
    progress_pct: float
    message: str
    error: Optional[str] = None


class RunProgressMessage(BaseModel):
    type: Literal["run_progress"] = "run_progress"
    total_workers: int
    completed_workers: int
    overall_pct: float


class LogMessage(BaseModel):
    type: Literal["log"] = "log"
    level: str  # "info", "warning", "error"
    text: str
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    worker_id: Optional[int] = None


class WindowResultPayload(BaseModel):
    worker_id: int
    model_name: Optional[str] = None
    response: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    error: Optional[str] = None


class RunCompleteMessage(BaseModel):
    type: Literal["run_complete"] = "run_complete"
    results: List[WindowResultPayload]
    total_elapsed_seconds: float
    export_available: bool


class ChallengeDetectedMessage(BaseModel):
    type: Literal["challenge_detected"] = "challenge_detected"
    worker_id: int
    challenge_type: str  # "turnstile", "recaptcha", "login_wall"
    message: str


class PongMessage(BaseModel):
    type: Literal["pong"] = "pong"


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str
    code: Optional[str] = None


OutboundMessage = Union[
    WorkerUpdateMessage,
    RunProgressMessage,
    LogMessage,
    RunCompleteMessage,
    ChallengeDetectedMessage,
    PongMessage,
    ErrorMessage,
]
