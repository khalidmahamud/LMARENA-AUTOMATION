from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ──── Inbound (GUI → Backend) ────


class ImagePayload(BaseModel):
    """Base64-encoded image attached to a prompt."""

    data: str  # raw base64 (no data URI prefix)
    mime_type: str
    filename: str = ""

    @field_validator("mime_type")
    @classmethod
    def validate_mime(cls, v: str) -> str:
        allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
        if v not in allowed:
            raise ValueError(f"Unsupported image type: {v}")
        return v

    @field_validator("data")
    @classmethod
    def validate_size(cls, v: str) -> str:
        # ~5 MB of base64 ≈ 6.67M chars
        if len(v) > 7_000_000:
            raise ValueError("Image data exceeds 5 MB limit")
        return v


class StartRunRequest(BaseModel):
    type: Literal["start_run"] = "start_run"
    prompt: str = Field(default="", max_length=50_000)
    prompts: Optional[List[str]] = Field(default=None)
    system_prompt: str = Field(default="", max_length=100_000)
    combine_with_first: bool = False
    window_count: int = Field(default=2, ge=1, le=12)
    submission_gap_seconds: Optional[float] = Field(default=None, ge=5.0)
    model_a: Optional[str] = None
    model_b: Optional[str] = None
    retain_output: str = Field(default="both")  # "both", "model_a", "model_b"
    clear_cookies: bool = False
    incognito: bool = False
    images: Optional[List[ImagePayload]] = Field(default=None, max_length=10)
    simultaneous_start: bool = False
    zoom_pct: int = Field(default=100, ge=25, le=200)
    # Display / tiling overrides (sent from UI, fall back to config defaults)
    monitor_count: Optional[int] = Field(default=None, ge=1, le=8)
    monitor_width: Optional[int] = Field(default=None, ge=800, le=7680)
    monitor_height: Optional[int] = Field(default=None, ge=600, le=4320)
    taskbar_height: Optional[int] = Field(default=None, ge=0, le=200)
    margin: Optional[int] = Field(default=None, ge=0, le=50)
    border_offset: Optional[int] = Field(default=None, ge=0, le=20)
    # Proxy list — each dict: {"server": "http://host:port", "username": "...", "password": "..."}
    proxies: Optional[List[dict]] = Field(default=None)
    proxy_on_challenge: bool = False

    @model_validator(mode="after")
    def validate_has_prompt(self):
        if self.prompts:
            self.prompts = [p for p in self.prompts if p.strip()]
        if not self.prompt and not self.prompts:
            raise ValueError("Either 'prompt' or 'prompts' must be provided")
        return self

    def get_prompt_for_worker(self, worker_index: int) -> str:
        """Return the actual prompt assigned to a specific worker."""
        if self.prompts and len(self.prompts) > 0:
            idx = min(worker_index, len(self.prompts) - 1)
            return self.prompts[idx]
        return self.prompt


class StopRunRequest(BaseModel):
    type: Literal["stop_run"] = "stop_run"


class PauseRunRequest(BaseModel):
    type: Literal["pause_run"] = "pause_run"


class ResumeRunRequest(BaseModel):
    type: Literal["resume_run"] = "resume_run"


class PingRequest(BaseModel):
    type: Literal["ping"] = "ping"


class ResumeFromCheckpointRequest(BaseModel):
    type: Literal["resume_from_checkpoint"] = "resume_from_checkpoint"
    run_id: str


InboundMessage = Union[
    StartRunRequest,
    StopRunRequest,
    PauseRunRequest,
    ResumeRunRequest,
    PingRequest,
    ResumeFromCheckpointRequest,
]


# ──── Outbound (Backend → GUI) ────


class WorkerUpdateMessage(BaseModel):
    type: Literal["worker_update"] = "worker_update"
    worker_id: int
    state: str
    progress_pct: float
    message: str
    error: Optional[str] = None
    proxy: Optional[str] = None


class RunProgressMessage(BaseModel):
    type: Literal["run_progress"] = "run_progress"
    total_workers: int
    completed_workers: int
    overall_pct: float
    phase: Optional[str] = None
    batch: Optional[int] = None
    total_batches: Optional[int] = None


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
    prompt: Optional[str] = None
    batch_index: Optional[int] = None
    model_a_name: Optional[str] = None
    model_a_response: Optional[str] = None
    model_b_name: Optional[str] = None
    model_b_response: Optional[str] = None
    model_a_response_html: Optional[str] = None
    model_b_response_html: Optional[str] = None
    elapsed_seconds: Optional[float] = None
    error: Optional[str] = None


class WorkerPartialResultPayload(BaseModel):
    worker_id: int
    slide: str  # "a" or "b"
    model_name: Optional[str] = None
    response: Optional[str] = None
    response_html: Optional[str] = None


class WorkerPartialResultMessage(BaseModel):
    type: Literal["worker_partial_result"] = "worker_partial_result"
    result: WorkerPartialResultPayload


class WorkerResultMessage(BaseModel):
    type: Literal["worker_result"] = "worker_result"
    result: WindowResultPayload


class RunCompleteMessage(BaseModel):
    type: Literal["run_complete"] = "run_complete"
    results: List[WindowResultPayload]
    total_elapsed_seconds: float
    export_available: bool


class RunCancelledMessage(BaseModel):
    type: Literal["run_cancelled"] = "run_cancelled"


class RunPausedMessage(BaseModel):
    type: Literal["run_paused"] = "run_paused"


class RunResumedMessage(BaseModel):
    type: Literal["run_resumed"] = "run_resumed"


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
    WorkerPartialResultMessage,
    WorkerResultMessage,
    RunProgressMessage,
    LogMessage,
    RunCompleteMessage,
    RunCancelledMessage,
    RunPausedMessage,
    RunResumedMessage,
    ChallengeDetectedMessage,
    ToastMessage,
    PongMessage,
    ErrorMessage,
]
