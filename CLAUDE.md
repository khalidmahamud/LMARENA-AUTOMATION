# CLAUDE.md

## Project Overview

LMARENA-Automation is an automated batch testing platform for [LM Arena](https://arena.ai/). It orchestrates parallel browser windows to submit prompts, capture model responses, and export structured results. Built with FastAPI + Playwright + vanilla JS.

## Quick Start

```bash
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
python app.py
# Open http://localhost:8000
```

## Architecture

Layered, event-driven design:

```
models/     Pure Pydantic data models (no I/O)
core/       Business logic (EventBus, state machine, exceptions)
browser/    Playwright interaction (contexts, stealth, selectors)
workers/    Per-window Arena lifecycle (submit, poll, collect)
orchestrator/ Central run coordinator (batching, parallelism)
transport/  WebSocket layer (never imports browser/workers)
checkpoint/ Run persistence and resume
export/     Excel/CSV/JSON output
proxy/      Proxy pool management + health tracking
```

**Key rule:** Transport layer never imports browser or workers. Communication flows through `EventBus` (pub/sub).

## Key Files

- `app.py` — FastAPI entry point, lifespan, routes, WebSocket endpoint
- `src/orchestrator/run_orchestrator.py` — Central run logic
- `src/workers/arena_worker.py` — Per-window lifecycle (12-state FSM)
- `src/browser/manager.py` — Browser context creation, tiling
- `src/transport/ws_handler.py` — WebSocket message dispatch
- `src/transport/ws_broadcaster.py` — Event-to-WebSocket bridge
- `config/default_config.yaml` — Default settings (Pydantic-validated)
- `config/selectors.yaml` — Centralized DOM selectors
- `templates/index.html` — Dashboard UI
- `static/app.js` — WebSocket client + DOM controller

## Code Conventions

- **Python 3.12+**, async/await throughout
- `snake_case` for functions/variables, `CamelCase` for classes
- `_private` prefix for internal methods/attributes
- Pydantic models for all data validation
- Module-level logger: `logger = logging.getLogger(__name__)`
- Custom exception hierarchy rooted at `ArenaAutomationError`
- Worker failures are isolated — one crash never kills the run
- No shared mutable state except `EventBus` and `ProxyPool`

## Config

Settings defined in `config/default_config.yaml`, validated by Pydantic models in `src/models/config.py`. All settings are overridable from the UI via `StartRunRequest` sent over WebSocket.

## API Endpoints

```
GET   /                  Dashboard UI
WS    /ws                WebSocket (all real-time updates)
POST  /upload-prompts    Parse CSV/Excel file
GET   /export            Download .xlsx
GET   /export-csv        Download .csv
GET   /export-json       Download .json
GET   /api/run-state     Current run state (reconnect)
GET   /api/checkpoints   List resumable checkpoints
DELETE /api/checkpoints/{id}  Discard checkpoint
GET   /proxy-pool-stats  Proxy pool health
POST  /proxy-pool/refresh-config  Auto-refresh config
POST  /proxy-pool/fetch  Fetch proxies from CDN
```

## Worker State Machine

```
IDLE → LAUNCHING → NAVIGATING → [WAITING_FOR_CHALLENGE] → READY
→ [SELECTING_MODEL] → PASTING → [PREPARED] → SUBMITTING
→ POLLING → COMPLETE / ERROR / CANCELLED
```

## Dependencies

Key packages: `fastapi`, `uvicorn`, `playwright`, `playwright-stealth`, `pydantic`, `openpyxl`, `PyYAML`, `uvloop`. Full list in `requirements.txt`.
