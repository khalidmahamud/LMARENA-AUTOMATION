"""LM Arena Side-by-Side Automation — Entry Point.

Run:  python app.py
Open: http://localhost:8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.browser.manager import BrowserManager
from src.browser.selectors import SelectorRegistry
from src.core.events import EventBus
from src.export.excel_exporter import export_to_excel
from src.models.config import AppConfig
from src.orchestrator.run_orchestrator import RunOrchestrator
from src.transport.ws_broadcaster import WsBroadcaster
from src.transport.ws_handler import WsHandler

# ── Logging ──

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Globals (wired at startup) ──

config: AppConfig
event_bus: EventBus
broadcaster: WsBroadcaster
browser_manager: BrowserManager
ws_handler: WsHandler


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup / shutdown lifecycle."""
    global config, event_bus, broadcaster, browser_manager, ws_handler

    config = AppConfig.from_yaml("config/default_config.yaml")
    SelectorRegistry.load("config/selectors.yaml")

    event_bus = EventBus()
    broadcaster = WsBroadcaster(event_bus)
    browser_manager = BrowserManager(config)
    await browser_manager.start()

    def orchestrator_factory() -> RunOrchestrator:
        return RunOrchestrator(config, event_bus, browser_manager)

    ws_handler = WsHandler(orchestrator_factory, broadcaster)

    logger.info("Server ready — open http://localhost:8000")
    yield

    await browser_manager.close_all()
    logger.info("Shutdown complete")


app = FastAPI(title="LM Arena Automation", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Routes ──


@app.get("/", response_class=HTMLResponse)
async def index():
    return Path("templates/index.html").read_text()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_handler.handle(websocket)


@app.get("/export")
async def export_excel():
    orch = ws_handler.orchestrator
    if orch and orch.last_result:
        path = export_to_excel(orch.last_result, config.output_dir)
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    return {"error": "No results available"}


# ── Entry point ──

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
