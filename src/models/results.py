from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class WindowResult(BaseModel):
    """Result from a single browser window."""

    worker_id: int
    prompt: str
    model_a_name: Optional[str] = None
    model_b_name: Optional[str] = None
    response_a: Optional[str] = None
    response_b: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    elapsed_seconds: Optional[float] = None
    success: bool = False
    error: Optional[str] = None


class RunResult(BaseModel):
    """Aggregate result for an entire run across N windows."""

    run_id: str
    prompt: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    total_elapsed_seconds: Optional[float] = None
    window_results: List[WindowResult] = Field(default_factory=list)
    total_windows: int = 0
    successful_windows: int = 0
    failed_windows: int = 0


class ExportableRow(BaseModel):
    """Flat row for Excel export."""

    window_number: int
    prompt: str
    model_a: str
    model_b: str
    response_a: str
    response_b: str
    elapsed_seconds: float
    status: str  # "success" | "error" | "timeout"
    error_detail: Optional[str] = None
