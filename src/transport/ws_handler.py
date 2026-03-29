from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Optional

from fastapi import WebSocket, WebSocketDisconnect

from src.models.messages import (
    ErrorMessage,
    PongMessage,
    StartRunRequest,
)
from src.orchestrator.run_orchestrator import RunOrchestrator
from src.transport.ws_broadcaster import WsBroadcaster

logger = logging.getLogger(__name__)

OrchestratorFactory = Callable[[], RunOrchestrator]


class WsHandler:
    """Handles a single WebSocket connection.

    Parses inbound messages, dispatches to orchestrator, and manages
    the broadcaster client list.
    """

    def __init__(
        self,
        orchestrator_factory: OrchestratorFactory,
        broadcaster: WsBroadcaster,
    ) -> None:
        self._orchestrator_factory = orchestrator_factory
        self._broadcaster = broadcaster
        self._orchestrator: Optional[RunOrchestrator] = None
        self._run_task: Optional[asyncio.Task] = None

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._broadcaster.add_client(websocket)

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_text(
                        ErrorMessage(message="Invalid JSON").model_dump_json()
                    )
                    continue

                msg_type = data.get("type")

                if msg_type == "start_run":
                    request = StartRunRequest(**data)
                    await self._handle_start_run(request)

                elif msg_type == "stop_run":
                    await self._handle_stop_run()

                elif msg_type == "ping":
                    await websocket.send_text(
                        PongMessage().model_dump_json()
                    )

                else:
                    await websocket.send_text(
                        ErrorMessage(
                            message=f"Unknown message type: {msg_type}"
                        ).model_dump_json()
                    )

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected")
        except Exception as exc:
            logger.error("WebSocket error: %s", exc, exc_info=True)
        finally:
            self._broadcaster.remove_client(websocket)

    async def _handle_start_run(self, request: StartRunRequest) -> None:
        """Start a new run as a background asyncio task."""
        if self._run_task and not self._run_task.done():
            logger.warning("Run already in progress, ignoring start request")
            return

        self._orchestrator = self._orchestrator_factory()
        self._run_task = asyncio.create_task(
            self._run_with_error_handling(request)
        )

    async def _run_with_error_handling(
        self, request: StartRunRequest
    ) -> None:
        try:
            await self._orchestrator.execute_run(request)
        except Exception as exc:
            logger.error("Run failed: %s", exc, exc_info=True)

    async def _handle_stop_run(self) -> None:
        if self._orchestrator:
            await self._orchestrator.cancel()

    @property
    def orchestrator(self) -> Optional[RunOrchestrator]:
        return self._orchestrator
