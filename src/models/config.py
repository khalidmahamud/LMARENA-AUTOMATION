from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class ProxyConfig(BaseModel):
    """Single proxy entry — maps directly to Playwright's proxy dict."""

    server: str  # e.g. "http://host:port" or "socks5://host:port"
    username: Optional[str] = None
    password: Optional[str] = None


class WindowSize(BaseModel):
    width: int = Field(default=900, ge=400, le=3840)
    height: int = Field(default=800, ge=300, le=2160)


class DisplayConfig(BaseModel):
    """Monitor and tiling configuration."""

    monitor_count: int = Field(default=1, ge=1, le=8)
    monitor_width: int = Field(default=1920, ge=800, le=7680)
    monitor_height: int = Field(default=1080, ge=600, le=4320)
    taskbar_height: int = Field(default=40, ge=0, le=200)
    margin: int = Field(default=0, ge=0, le=50)
    border_offset: int = Field(default=7, ge=0, le=20)


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
    headless: bool = Field(default=False)
    incognito: bool = Field(default=False)
    proxies: List[ProxyConfig] = Field(default_factory=list)


class DistributedConfig(BaseModel):
    """Configuration for distributed execution mode."""

    enabled: bool = Field(default=False)
    coordinator_port: int = Field(default=8001, ge=1024, le=65535)
    auth_token: str = Field(default="change-me")
    scheduling_policy: str = Field(default="fill")  # "fill" or "spread"
    heartbeat_interval_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    heartbeat_timeout_missed: int = Field(default=3, ge=1, le=10)
    reconnect_grace_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    event_coalesce_ms: int = Field(default=100, ge=0, le=1000)
    allow_local_workers: bool = Field(default=False)


class AppConfig(BaseModel):
    """Root configuration — validated from YAML at startup."""

    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    typing: TypingConfig = Field(default_factory=TypingConfig)
    arena_url: str = Field(default="https://arena.ai/text/direct")
    output_dir: str = Field(default="outputs")
    log_level: str = Field(default="INFO")
    distributed: Optional[DistributedConfig] = Field(default=None)

    @classmethod
    def from_yaml(cls, path: str) -> AppConfig:
        import yaml

        raw = yaml.safe_load(Path(path).read_text()) or {}
        return cls(**raw)
