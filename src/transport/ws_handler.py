from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Callable, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect

from src.checkpoint.manager import CheckpointManager, RunCheckpoint
from src.models.messages import (
    ErrorMessage,
    PauseRunRequest,
    PongMessage,
    ResumeRunRequest,
    StartRunRequest,
    StopRunRequest,
)
from src.orchestrator.run_orchestrator import RunOrchestrator
from src.transport.ws_broadcaster import WsBroadcaster

logger = logging.getLogger(__name__)

OrchestratorFactory = Callable[[], RunOrchestrator]


class WsHandler:
    """Handles a single WebSocket connection.

    Parses inbound messages, dispatches to orchestrator(s), and manages
    the broadcaster client list.  Supports multiple concurrent runs keyed
    by ``run_id``.
    """

    def __init__(
        self,
        orchestrator_factory: OrchestratorFactory,
        broadcaster: WsBroadcaster,
        checkpoint_manager: Optional[CheckpointManager] = None,
        screenshot_service=None,
    ) -> None:
        self._orchestrator_factory = orchestrator_factory
        self._broadcaster = broadcaster
        self._checkpoint_manager = checkpoint_manager
        self._screenshot_service = screenshot_service
        self._orchestrators: Dict[str, RunOrchestrator] = {}
        self._run_tasks: Dict[str, asyncio.Task] = {}

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
                    try:
                        request = StartRunRequest(**data)
                    except Exception as exc:
                        logger.error("Invalid start_run request: %s", exc)
                        await websocket.send_text(
                            ErrorMessage(
                                message=f"Invalid request: {exc}"
                            ).model_dump_json()
                        )
                        continue
                    await self._handle_start_run(request)

                elif msg_type == "stop_run":
                    run_id = data.get("run_id")
                    await self._handle_stop_run(run_id)

                elif msg_type == "pause_run":
                    request = PauseRunRequest(**data)
                    await self._handle_pause_run(request)

                elif msg_type == "resume_run":
                    request = ResumeRunRequest(**data)
                    await self._handle_resume_run(request)

                elif msg_type == "resume_from_checkpoint":
                    run_id = data.get("run_id")
                    if not run_id or not self._checkpoint_manager:
                        await websocket.send_text(
                            ErrorMessage(
                                message="Resume not available"
                            ).model_dump_json()
                        )
                        continue
                    checkpoint = self._checkpoint_manager.load(run_id)
                    if not checkpoint:
                        await websocket.send_text(
                            ErrorMessage(
                                message=f"Checkpoint {run_id} not found or corrupt"
                            ).model_dump_json()
                        )
                        continue
                    request = StartRunRequest(**checkpoint.original_request)
                    await self._handle_start_run(
                        request, resume_checkpoint=checkpoint
                    )

                elif msg_type == "subscribe_preview":
                    if self._screenshot_service:
                        self._screenshot_service.add_subscriber(websocket)

                elif msg_type == "unsubscribe_preview":
                    if self._screenshot_service:
                        self._screenshot_service.remove_subscriber(websocket)

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
            if self._screenshot_service:
                self._screenshot_service.remove_subscriber(websocket)

    async def _handle_start_run(
        self,
        request: StartRunRequest,
        resume_checkpoint: Optional[RunCheckpoint] = None,
    ) -> None:
        """Start a new run (or resume) as a background asyncio task."""
        run_id = request.run_id or str(uuid.uuid4())[:8]
        request.run_id = run_id

        orchestrator = self._orchestrator_factory()
        self._orchestrators[run_id] = orchestrator

        task = asyncio.create_task(
            self._run_with_error_handling(run_id, orchestrator, request, resume_checkpoint)
        )
        self._run_tasks[run_id] = task
        task.add_done_callback(lambda _t: self._cleanup_run(run_id))

    def _cleanup_run(self, run_id: str) -> None:
        """Remove the task entry when a run finishes. Keep orchestrator for exports."""
        self._run_tasks.pop(run_id, None)

    async def _run_with_error_handling(
        self,
        run_id: str,
        orchestrator: RunOrchestrator,
        request: StartRunRequest,
        resume_checkpoint: Optional[RunCheckpoint] = None,
    ) -> None:
        try:
            await orchestrator.execute_run(request, resume_checkpoint)
        except Exception as exc:
            logger.error("Run %s failed: %s", run_id, exc, exc_info=True)

    async def _handle_stop_run(self, run_id: Optional[str] = None) -> None:
        if run_id:
            # Stop a specific run
            orchestrator = self._orchestrators.get(run_id)
            if orchestrator:
                try:
                    await orchestrator.cancel()
                except Exception as exc:
                    logger.error("Error during cancel of run %s: %s", run_id, exc)
            task = self._run_tasks.get(run_id)
            if task and not task.done():
                task.cancel()
        else:
            # Backward compat: stop all runs
            for rid in list(self._orchestrators):
                await self._handle_stop_run(rid)

    async def _handle_pause_run(self, request: PauseRunRequest) -> None:
        run_id = request.run_id
        if run_id:
            orchestrator = self._orchestrators.get(run_id)
            if orchestrator:
                try:
                    await orchestrator.pause()
                except Exception as exc:
                    logger.error("Error during pause of run %s: %s", run_id, exc)
        else:
            # Backward compat: pause all
            for orch in self._orchestrators.values():
                try:
                    await orch.pause()
                except Exception as exc:
                    logger.error("Error during pause: %s", exc)

    async def _handle_resume_run(self, request: ResumeRunRequest) -> None:
        run_id = request.run_id
        if run_id:
            orchestrator = self._orchestrators.get(run_id)
            if orchestrator:
                try:
                    await orchestrator.resume()
                except Exception as exc:
                    logger.error("Error during resume of run %s: %s", run_id, exc)
        else:
            # Backward compat: resume all
            for orch in self._orchestrators.values():
                try:
                    await orch.resume()
                except Exception as exc:
                    logger.error("Error during resume: %s", exc)

    @property
    def orchestrator(self) -> Optional[RunOrchestrator]:
        """Backward-compatible: return the most recent orchestrator."""
        if not self._orchestrators:
            return None
        return next(reversed(self._orchestrators.values()))

    def get_orchestrator(self, run_id: str) -> Optional[RunOrchestrator]:
        """Get a specific orchestrator by run_id."""
        return self._orchestrators.get(run_id)

    def get_all_orchestrators(self) -> Dict[str, RunOrchestrator]:
        return self._orchestrators

    @property
    def is_run_active(self) -> bool:
        return any(not t.done() for t in self._run_tasks.values())

    def get_run_state(self) -> Optional[dict]:
        """Return current run states for UI sync on reconnect."""
        if not self._orchestrators:
            return None

        runs = {}
        for rid, orch in self._orchestrators.items():
            snapshot = orch.get_run_snapshot()
            if snapshot:
                task = self._run_tasks.get(rid)
                snapshot["running"] = bool(task and not task.done())
                runs[rid] = snapshot

        if not runs:
            return None

        # Backward compat: if only one run, return it directly
        if len(runs) == 1:
            return next(iter(runs.values()))

        return {"runs": runs}
