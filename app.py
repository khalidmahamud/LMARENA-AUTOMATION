"""LM Arena Side-by-Side Automation — Entry Point.

Run:  python app.py
Open: http://localhost:8000
"""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook

from src.browser.manager import BrowserManager
from src.browser.selectors import SelectorRegistry
from src.checkpoint.manager import CheckpointManager
from src.core.events import EventBus
from src.export.excel_exporter import export_to_csv, export_to_excel, export_to_json
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
checkpoint_manager: CheckpointManager
ws_handler: WsHandler


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup / shutdown lifecycle."""
    global config, event_bus, broadcaster, browser_manager, checkpoint_manager, ws_handler

    config = AppConfig.from_yaml("config/default_config.yaml")
    SelectorRegistry.load("config/selectors.yaml")

    event_bus = EventBus()
    broadcaster = WsBroadcaster(event_bus)
    browser_manager = BrowserManager(config)
    checkpoint_manager = CheckpointManager(config.output_dir)
    await browser_manager.start()

    def orchestrator_factory() -> RunOrchestrator:
        return RunOrchestrator(
            config, event_bus, browser_manager, checkpoint_manager
        )

    ws_handler = WsHandler(orchestrator_factory, broadcaster, checkpoint_manager)

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


@app.post("/upload-prompts")
async def upload_prompts(file: UploadFile):
    """Parse a CSV or Excel file and return columns + all rows."""
    if not file.filename:
        return JSONResponse({"error": "No file provided"}, status_code=400)

    ext = Path(file.filename).suffix.lower()
    raw = await file.read()

    try:
        if ext == ".csv":
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            columns = reader.fieldnames or []
            rows = [dict(r) for r in reader]
        elif ext == ".xlsx":
            wb = load_workbook(filename=io.BytesIO(raw), read_only=True)
            ws = wb.active
            all_rows = list(ws.iter_rows(values_only=True))
            if not all_rows:
                return JSONResponse({"error": "Empty spreadsheet"}, status_code=400)
            columns = [str(c) if c else f"Column {i+1}" for i, c in enumerate(all_rows[0])]
            rows = [
                {columns[j]: (str(cell) if cell is not None else "")
                 for j, cell in enumerate(row)}
                for row in all_rows[1:]
            ]
            wb.close()
        else:
            return JSONResponse(
                {"error": f"Unsupported file type: {ext}. Use .csv or .xlsx"},
                status_code=400,
            )
    except Exception as exc:
        logger.error("Failed to parse upload: %s", exc)
        return JSONResponse({"error": f"Parse error: {exc}"}, status_code=400)

    return {
        "filename": file.filename,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "preview": rows[:5],
    }


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


@app.get("/export-csv")
async def export_csv_file():
    orch = ws_handler.orchestrator
    if orch and orch.last_result:
        path = export_to_csv(orch.last_result, config.output_dir)
        return FileResponse(path, filename=path.name, media_type="text/csv")
    return {"error": "No results available"}


@app.get("/export-json")
async def export_json():
    orch = ws_handler.orchestrator
    if orch and orch.last_result:
        path = export_to_json(orch.last_result, config.output_dir)
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/json",
        )
    return {"error": "No results available"}


# ── Run state endpoint ──


@app.get("/api/run-state")
async def get_run_state():
    """Return current run state for UI sync on reconnect."""
    state = ws_handler.get_run_state()
    if state:
        return state
    return {"running": False}


# ── Checkpoint endpoints ──


@app.get("/api/checkpoints")
async def list_checkpoints():
    """Return all resumable (in_progress) checkpoint summaries."""
    checkpoints = checkpoint_manager.list_resumable()
    return [
        {
            "run_id": cp.run_id,
            "total_prompts": len(cp.all_prompts),
            "completed_prompts": len(cp.completed_prompt_indices),
            "next_batch": cp.next_batch_index,
            "total_batches": cp.total_batches,
            "last_checkpoint_at": cp.last_checkpoint_at,
            "status": cp.status,
        }
        for cp in checkpoints
    ]


@app.delete("/api/checkpoints/{run_id}")
async def delete_checkpoint(run_id: str):
    """Discard a checkpoint."""
    checkpoint_manager.delete(run_id)
    return {"deleted": run_id}


# ── Entry point ──

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, ws_max_size=67_108_864)
