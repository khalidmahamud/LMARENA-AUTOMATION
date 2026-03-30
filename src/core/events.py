from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

# Type alias for async event handlers
EventHandler = Callable[["Event"], Coroutine[Any, Any, None]]


class EventType(str, Enum):
    # Worker lifecycle
    WORKER_STATE_CHANGED = "worker.state_changed"
    WORKER_PROGRESS = "worker.progress"
    WORKER_ERROR = "worker.error"
    WORKER_COMPLETE = "worker.complete"

    # Challenge detection
    CHALLENGE_DETECTED = "challenge.detected"
    CHALLENGE_RESOLVED = "challenge.resolved"

    # Run lifecycle
    RUN_STARTED = "run.started"
    RUN_PROGRESS = "run.progress"
    RUN_COMPLETE = "run.complete"
    RUN_CANCELLED = "run.cancelled"
    RUN_ERROR = "run.error"

    # Notifications
    TOAST = "toast"

    # Logging
    LOG = "log"


@dataclass
class Event:
    type: EventType
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    data: Dict[str, Any] = field(default_factory=dict)
    worker_id: Optional[int] = None


class EventBus:
    """Async pub/sub event bus.

    Workers publish events; transport layer subscribes.
    Runs in a single asyncio event loop — no threading concerns.
    Handler exceptions are logged but never propagate (bus must not crash).
    """

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._global_handlers: List[EventHandler] = []

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe to a specific event type."""
        self._handlers.setdefault(event_type, []).append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe to ALL events (used by WebSocket broadcaster)."""
        self._global_handlers.append(handler)

    async def publish(self, event: Event) -> None:
        """Publish event to all matching handlers concurrently."""
        handlers = list(self._global_handlers)
        handlers.extend(self._handlers.get(event.type, []))

        if not handlers:
            return

        results = await asyncio.gather(
            *(h(event) for h in handlers),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.error(
                    "Handler error for %s: %s", event.type, r, exc_info=r
                )

    def clear(self) -> None:
        """Remove all subscriptions. Useful in tests."""
        self._handlers.clear()
        self._global_handlers.clear()
