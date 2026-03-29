from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class WindowSize(BaseModel):
    width: int = Field(default=900, ge=400, le=3840)
    height: int = Field(default=800, ge=300, le=2160)


class TimingConfig(BaseModel):
    submission_gap_seconds: float = Field(default=30.0, ge=5.0, le=300.0)
    jitter_pct: float = Field(default=0.30, ge=0.0, le=1.0)
    poll_interval_seconds: float = Field(default=2.0, ge=0.5, le=10.0)
    stable_polls_required: int = Field(default=3, ge=2, le=10)
    response_timeout_seconds: float = Field(default=300.0, ge=30.0, le=900.0)


class TypingConfig(BaseModel):
    min_delay_ms: int = Field(default=50, ge=10, le=500)
    max_delay_ms: int = Field(default=150, ge=50, le=1000)

    @field_validator("max_delay_ms")
    @classmethod
    def max_gte_min(cls, v: int, info) -> int:
        if "min_delay_ms" in info.data and v < info.data["min_delay_ms"]:
            raise ValueError("max_delay_ms must be >= min_delay_ms")
        return v


class BrowserConfig(BaseModel):
    window_count: int = Field(default=2, ge=1, le=12)
    window_size: WindowSize = Field(default_factory=WindowSize)
    profile_dir: str = Field(default="browser_profiles")
    headless: bool = Field(default=False)


class AppConfig(BaseModel):
    """Root configuration — validated from YAML at startup."""

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    typing: TypingConfig = Field(default_factory=TypingConfig)
    arena_url: str = Field(default="https://arena.ai/text/direct")
    output_dir: str = Field(default="outputs")
    log_level: str = Field(default="INFO")

    @classmethod
    def from_yaml(cls, path: str) -> AppConfig:
        import yaml

        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls(**raw)
