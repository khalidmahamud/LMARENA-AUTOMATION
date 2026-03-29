from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class WorkerState(str, Enum):
    """Lifecycle states for a single browser window."""

    IDLE = "idle"
    LAUNCHING = "launching"
    NAVIGATING = "navigating"
    WAITING_FOR_CHALLENGE = "waiting_for_challenge"
    READY = "ready"
    SELECTING_MODEL = "selecting_model"
    PASTING = "pasting"
    SUBMITTING = "submitting"
    POLLING = "polling"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


class WorkerSnapshot(BaseModel):
    """Immutable snapshot of a worker's current status. Sent via WebSocket."""

    worker_id: int
    state: WorkerState
    progress_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    message: str = ""
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    elapsed_seconds: Optional[float] = None
