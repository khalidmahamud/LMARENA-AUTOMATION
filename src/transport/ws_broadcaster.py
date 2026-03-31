from __future__ import annotations

import logging
from typing import Optional, Set

from fastapi import WebSocket

from src.core.events import Event, EventBus, EventType
from src.models.messages import (
    ChallengeDetectedMessage,
    LogMessage,
    OutboundMessage,
    RunCancelledMessage,
    RunCompleteMessage,
    RunPausedMessage,
    RunProgressMessage,
    RunResumedMessage,
    ToastMessage,
    WindowResultPayload,
    WorkerPartialResultMessage,
    WorkerPartialResultPayload,
    WorkerResultMessage,
    WorkerUpdateMessage,
)

logger = logging.getLogger(__name__)


class WsBroadcaster:
    """Subscribes to the EventBus and broadcasts formatted messages
    to all connected WebSocket clients.

    This is the **only** module that converts internal events into
    WebSocket messages.  Workers and orchestrator never touch
    WebSocket directly.
    """

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus
        self._clients: Set[WebSocket] = set()
        event_bus.subscribe_all(self._handle_event)

    def add_client(self, ws: WebSocket) -> None:
        self._clients.add(ws)

    def remove_client(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def _handle_event(self, event: Event) -> None:
        message = self._event_to_message(event)
        if message is None:
            return

        payload = message.model_dump_json()
        dead: Set[WebSocket] = set()

        for client in list(self._clients):
            try:
                await client.send_text(payload)
            except Exception:
                dead.add(client)

        self._clients -= dead

    def _event_to_message(self, event: Event) -> Optional[OutboundMessage]:
        """Map an internal event to an outbound WebSocket message."""
        d = event.data

        if event.type == EventType.WORKER_STATE_CHANGED:
            return WorkerUpdateMessage(
                worker_id=event.worker_id or 0,
                state=d.get("new_state", ""),
                progress_pct=d.get("progress", 0),
                message=f"State: {d.get('new_state', '')}",
                proxy=d.get("proxy"),
            )

        if event.type == EventType.WORKER_ERROR:
            return WorkerUpdateMessage(
                worker_id=event.worker_id or 0,
                state="error",
                progress_pct=100.0,
                message="Error occurred",
                error=d.get("error"),
            )

        if event.type == EventType.WORKER_PARTIAL_RESULT:
            return WorkerPartialResultMessage(
                result=WorkerPartialResultPayload(
                    worker_id=event.worker_id or 0,
                    slide=d.get("slide", "a"),
                    model_name=d.get("model_name"),
                    response=d.get("response"),
                    response_html=d.get("response_html"),
                ),
            )

        if event.type == EventType.WORKER_COMPLETE:
            result_data = d.get("result", {})
            return WorkerResultMessage(
                result=WindowResultPayload(**result_data),
            )

        if event.type == EventType.RUN_PROGRESS:
            total = d.get("total_workers", 1)
            submitted = d.get("submitted", 0)
            return RunProgressMessage(
                total_workers=total,
                completed_workers=submitted,
                overall_pct=submitted / max(total, 1) * 100,
                phase=d.get("phase"),
                batch=d.get("batch"),
                total_batches=d.get("total_batches"),
            )

        if event.type == EventType.RUN_COMPLETE:
            run_data = d.get("run_result", {})
            results = [
                WindowResultPayload(**wr)
                for wr in run_data.get("window_results", [])
            ]
            return RunCompleteMessage(
                results=results,
                total_elapsed_seconds=run_data.get(
                    "total_elapsed_seconds", 0
                ),
                export_available=True,
            )

        if event.type == EventType.RUN_CANCELLED:
            return RunCancelledMessage()

        if event.type == EventType.RUN_PAUSED:
            return RunPausedMessage()

        if event.type == EventType.RUN_RESUMED:
            return RunResumedMessage()

        if event.type == EventType.CHALLENGE_DETECTED:
            return ChallengeDetectedMessage(
                worker_id=event.worker_id or 0,
                challenge_type=d.get("challenge_type", "unknown"),
                message=(
                    f"Challenge detected on window {event.worker_id}. "
                    "Automatic recovery is in progress."
                ),
            )

        if event.type == EventType.TOAST:
            return ToastMessage(
                message=d.get("message", ""),
                level=d.get("level", "success"),
            )

        if event.type == EventType.LOG:
            return LogMessage(
                level=d.get("level", "info"),
                text=d.get("text", ""),
                worker_id=event.worker_id,
            )

        return None
