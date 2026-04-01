"""LM Arena Side-by-Side Automation — Entry Point.

Run:  python app.py
Open: http://localhost:8000
"""

from __future__ import annotations

import asyncio
import csv
import io
import json as json_mod
import logging
import random
import urllib.request
from contextlib import asynccontextmanager
from functools import partial
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
from src.proxy.pool import ProxyPool
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
proxy_pool: ProxyPool


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Startup / shutdown lifecycle."""
    global config, event_bus, broadcaster, browser_manager, checkpoint_manager, ws_handler, proxy_pool

    config = AppConfig.from_yaml("config/default_config.yaml")
    SelectorRegistry.load("config/selectors.yaml")

    # Initialize proxy pool (load persisted proxies and settings)
    proxy_pool = ProxyPool(check_fn=_check_proxy)
    proxy_pool.load_from_file()

    # Resume auto-refresh if it was enabled before shutdown
    ar = proxy_pool.auto_refresh_settings
    if ar["enabled"]:
        await proxy_pool.start_auto_refresh(protocol=ar["protocol"], interval=ar["interval"])
        logger.info("Auto-refresh resumed (protocol=%s, interval=%.0fs)", ar["protocol"], ar["interval"])
    elif proxy_pool.total_count > 0:
        # Not auto-refreshing, but have loaded proxies — health-check them in background
        asyncio.create_task(proxy_pool.maintain_pool(protocol=ar["protocol"]))
        logger.info("Background health check started for %d loaded proxies", proxy_pool.total_count)

    event_bus = EventBus()
    broadcaster = WsBroadcaster(event_bus)
    browser_manager = BrowserManager(config, proxy_pool=proxy_pool)
    checkpoint_manager = CheckpointManager(config.output_dir)
    await browser_manager.start()

    def orchestrator_factory() -> RunOrchestrator:
        return RunOrchestrator(
            config, event_bus, browser_manager, checkpoint_manager
        )

    ws_handler = WsHandler(orchestrator_factory, broadcaster, checkpoint_manager)

    logger.info("Server ready — open http://localhost:8000")
    yield

    # Shutdown: stop the task but preserve the saved auto-refresh preference
    await proxy_pool.stop_auto_refresh(clear_enabled=False)
    proxy_pool.save_to_file()
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


# ── Free proxy endpoint ──

PROXIFLY_URL = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
    "/proxies/protocols/{protocol}/data.json"
)


TEST_URL = "https://arena.ai/"


def _check_proxy(proxy_server: str, timeout: int = 5) -> float:
    """Test if a proxy can HTTPS-tunnel to arena.ai. Returns latency in ms, or -1.0 on failure."""
    import socket
    import ssl
    import time as _time

    start = _time.monotonic()
    try:
        # Parse proxy host:port
        stripped = proxy_server.split("://")[-1]
        proxy_host, proxy_port = stripped.rsplit(":", 1)
        proxy_port = int(proxy_port)

        if proxy_server.startswith("socks"):
            # SOCKS: try PySocks, fall back to TCP connect
            try:
                import socks as pysocks
                proto = pysocks.SOCKS5 if "socks5" in proxy_server else pysocks.SOCKS4
                s = pysocks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
                s.set_proxy(proto, proxy_host, proxy_port)
                s.settimeout(timeout)
                s.connect(("arena.ai", 443))
                # Try TLS handshake
                ctx = ssl.create_default_context()
                ss = ctx.wrap_socket(s, server_hostname="arena.ai")
                ss.close()
                return (_time.monotonic() - start) * 1000.0
            except ImportError:
                sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
                sock.close()
                return (_time.monotonic() - start) * 1000.0
        else:
            # HTTP proxy: send CONNECT request (this is exactly what Chromium does)
            sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
            connect_req = f"CONNECT arena.ai:443 HTTP/1.1\r\nHost: arena.ai:443\r\n\r\n"
            sock.sendall(connect_req.encode())

            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    sock.close()
                    return -1.0
                response += chunk

            # Check for 200 Connection established
            status_line = response.split(b"\r\n")[0].decode(errors="ignore")
            if "200" not in status_line:
                sock.close()
                return -1.0

            # TLS handshake through the tunnel — proves it actually works
            ctx = ssl.create_default_context()
            ss = ctx.wrap_socket(sock, server_hostname="arena.ai")
            ss.close()
            return (_time.monotonic() - start) * 1000.0
    except Exception:
        return -1.0


@app.get("/api/free-proxies")
async def fetch_free_proxies(
    protocol: str = "http", limit: int = 10, test: bool = False
):
    """Fetch free proxies from proxifly. If test=true, health-check each proxy."""
    allowed = {"http", "https", "socks4", "socks5"}
    if protocol not in allowed:
        return JSONResponse(
            {"error": f"Invalid protocol. Choose from: {', '.join(sorted(allowed))}"},
            status_code=400,
        )
    limit = max(1, min(limit, 100))

    url = PROXIFLY_URL.format(protocol=protocol)
    try:
        def _fetch():
            req = urllib.request.Request(url, headers={"User-Agent": "LMArenaAutomation/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json_mod.loads(resp.read().decode())

        data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    except Exception as exc:
        logger.error("Failed to fetch proxies from proxifly: %s", exc)
        return JSONResponse({"error": f"Failed to fetch proxies: {exc}"}, status_code=502)

    random.shuffle(data)

    if not test:
        proxies = [{"server": entry["proxy"]} for entry in data[:limit] if "proxy" in entry]
        return {"proxies": proxies, "count": len(proxies), "protocol": protocol, "tested": False}

    # Health-check mode: test a larger pool to find enough working proxies
    pool_size = min(len(data), limit * 10)
    candidates = [entry["proxy"] for entry in data[:pool_size] if "proxy" in entry]
    logger.info("Testing %d proxies to find %d working ones...", len(candidates), limit)

    # Check in batches of 20 to avoid overwhelming the network
    loop = asyncio.get_event_loop()
    results = []
    batch_size = 20
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]
        batch_results = await asyncio.gather(
            *(loop.run_in_executor(None, _check_proxy, proxy) for proxy in batch)
        )
        results.extend(batch_results)

    alive = [
        {"server": proxy, "latency_ms": round(latency, 1)}
        for proxy, latency in zip(candidates, results)
        if latency > 0
    ]
    alive.sort(key=lambda p: p["latency_ms"])
    alive = alive[:limit]
    logger.info("Found %d working proxies out of %d tested", len(alive), len(candidates))

    # Also add to proxy pool
    if alive:
        proxy_pool.add_proxies(alive, source="auto_fetch")

    return {"proxies": alive, "count": len(alive), "protocol": protocol, "tested": True, "total_tested": len(candidates)}


# ── Proxy pool endpoints ──


@app.get("/api/proxy-pool/status")
async def proxy_pool_status():
    """Return current pool status for UI display."""
    try:
        return proxy_pool.get_status()
    except Exception as exc:
        logger.error("proxy_pool_status error: %s", exc)
        return {
            "total": 0,
            "healthy": 0,
            "unhealthy": 0,
            "auto_refresh_enabled": False,
            "auto_refresh_active": False,
            "proxies": [],
        }


@app.post("/api/proxy-pool/save")
async def save_proxy_pool():
    """Persist current pool to disk."""
    path = proxy_pool.save_to_file()
    return {"saved": True, "path": str(path), "count": proxy_pool.total_count}


@app.post("/api/proxy-pool/load")
async def load_proxy_pool():
    """Load pool from disk."""
    count = proxy_pool.load_from_file()
    return {"loaded": True, "count": count}


@app.post("/api/proxy-pool/add")
async def add_to_proxy_pool(request: dict):
    """Manually add proxies to the pool. Body: {"proxies": [...]}"""
    proxies = request.get("proxies", [])
    added = proxy_pool.add_proxies(proxies, source="manual")
    return {"added": added, "total": proxy_pool.total_count}


@app.post("/api/proxy-pool/auto-refresh/start")
async def start_auto_refresh(
    protocol: str = "http", limit: int = 20, interval: int = 300
):
    """Start the background auto-refresh task and persist the preference."""
    await proxy_pool.start_auto_refresh(
        protocol=protocol, fetch_limit=limit, interval=float(interval)
    )
    proxy_pool.save_to_file()
    return {"started": True, "interval": interval}


@app.post("/api/proxy-pool/auto-refresh/stop")
async def stop_auto_refresh():
    """Stop the background auto-refresh task and persist the preference."""
    await proxy_pool.stop_auto_refresh()
    proxy_pool.save_to_file()
    return {"stopped": True}


@app.post("/api/proxy-pool/max-size")
async def set_pool_max_size(limit: int = 50):
    """Update the max healthy proxies cap."""
    proxy_pool.set_max_healthy(limit)
    proxy_pool.save_to_file()
    return {"max_healthy": limit}


@app.post("/api/proxy-pool/max-latency")
async def set_pool_max_latency(ms: int = 5000):
    """Update the max latency threshold in ms."""
    proxy_pool.set_max_latency(float(ms))
    proxy_pool.save_to_file()
    return {"max_latency_ms": ms}


@app.post("/api/proxy-pool/health-check")
async def health_check_pool():
    """Trigger an immediate health check of all proxies in the pool."""
    stats = await proxy_pool.health_check_all()
    return stats


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
