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
    model_a: Optional[str] = None
    model_b: Optional[str] = None
    clear_cookies: bool = False
    zoom_pct: int = Field(default=100, ge=25, le=200)
    # Display / tiling overrides (sent from UI, fall back to config defaults)
    monitor_count: Optional[int] = Field(default=None, ge=1, le=8)
    monitor_width: Optional[int] = Field(default=None, ge=800, le=7680)
    monitor_height: Optional[int] = Field(default=None, ge=600, le=4320)
    taskbar_height: Optional[int] = Field(default=None, ge=0, le=200)
    margin: Optional[int] = Field(default=None, ge=0, le=50)


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
    model_a_name: Optional[str] = None
    model_a_response: Optional[str] = None
    model_b_name: Optional[str] = None
    model_b_response: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    error: Optional[str] = None


class RunCompleteMessage(BaseModel):
    type: Literal["run_complete"] = "run_complete"
    results: List[WindowResultPayload]
    total_elapsed_seconds: float
    export_available: bool


class RunCancelledMessage(BaseModel):
    type: Literal["run_cancelled"] = "run_cancelled"


class ChallengeDetectedMessage(BaseModel):
    type: Literal["challenge_detected"] = "challenge_detected"
    worker_id: int
    challenge_type: str  # "turnstile", "recaptcha", "login_wall"
    message: str


class ToastMessage(BaseModel):
    type: Literal["toast"] = "toast"
    message: str
    level: str = "success"  # "success", "info", "warning", "error"


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
    RunCancelledMessage,
    ChallengeDetectedMessage,
    ToastMessage,
    PongMessage,
    ErrorMessage,
]
