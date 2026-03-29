# LM Arena Side-by-Side Automation — Progress

## Architecture (Revised)

Redesigned from the original flat plan into a modular, layered architecture:
- **`src/models/`** — Pure Pydantic data models (config, worker state, WS messages, results)
- **`src/core/`** — Business logic with no I/O (EventBus, state machine, exceptions, tiling math)
- **`src/browser/`** — Playwright interaction (manager, stealth, selectors, challenges)
- **`src/workers/`** — Per-window Arena lifecycle (ArenaWorker, human simulation, response poller)
- **`src/orchestrator/`** — Central run coordinator (sequential submit → parallel poll → collect)
- **`src/transport/`** — WebSocket layer (handler + broadcaster). Never imports browser/workers.
- **`src/export/`** — Excel export from RunResult

**Dependency rule**: Workers publish events through EventBus. Transport subscribes. They never import each other.

---

## Phase 0 — Foundation ✅

- [x] `src/models/config.py` — `AppConfig` with nested `BrowserConfig`, `TimingConfig`, `TypingConfig`, all with Pydantic field constraints
- [x] `src/models/worker.py` — `WorkerState` enum (12 states: IDLE → LAUNCHING → NAVIGATING → WAITING_FOR_CHALLENGE → READY → SELECTING_MODEL → PASTING → SUBMITTING → POLLING → COMPLETE/ERROR/CANCELLED), `WorkerSnapshot`
- [x] `src/models/messages.py` — Typed WebSocket protocol. Inbound: `StartRunRequest`, `StopRunRequest`, `PingRequest`. Outbound: `WorkerUpdateMessage`, `RunProgressMessage`, `LogMessage`, `RunCompleteMessage`, `ChallengeDetectedMessage`, `PongMessage`, `ErrorMessage`
- [x] `src/models/results.py` — `WindowResult`, `RunResult`, `ExportableRow`
- [x] `src/core/exceptions.py` — Full hierarchy: `ArenaAutomationError` → `BrowserError`/`WorkerError`/`RunError`/`ConfigError` with specific subtypes
- [x] `src/core/events.py` — Async `EventBus` with `subscribe()`, `subscribe_all()`, `publish()`. Handler errors logged but never propagate.
- [x] `src/core/state_machine.py` — `WorkerStateMachine` with transition table, progress percentages, async callback on every transition, `force_error()`, `reset()`
- [x] `src/core/tiling.py` — `compute_tile_positions()` — grid layout calculator

**Verified**: Config validation, state machine transitions (full lifecycle), EventBus pub/sub, tiling math.

## Phase 1 — Selector Registry + Config YAML ✅

- [x] `config/default_config.yaml` — Updated to match nested Pydantic schema (browser/timing/typing sections)
- [x] `config/selectors.yaml` — All Arena DOM selectors centralized. Placeholder values — must be refined by inspecting the live site.
- [x] `src/browser/selectors.py` — `SelectorRegistry` singleton with dotted-key accessor (e.g. `get("response_panel.left")`), health check method

**Verified**: YAML loading, dotted-key resolution, missing key errors, singleton pattern.

## Phase 2 — Browser Management ✅

- [x] `src/browser/manager.py` — `BrowserManager` with `start()`, `create_contexts(n)`, `close_all()`. Uses `launch_persistent_context()` per window with separate `user_data_dir` for cookie persistence.
- [x] `src/browser/stealth.py` — `apply_stealth()` applies `playwright-stealth` to existing pages + auto-patches new pages via `context.on("page", ...)`

**Context**: Each window gets `browser_profiles/context_N/` directory. Cookies (`cf_clearance`, `arena-auth-prod-v1`) persist across runs. Trade-off: one process per context, but persistence is essential for Cloudflare bypass.

## Phase 3 — Challenge Detection ✅

- [x] `src/browser/challenges.py` — `detect_challenge(page)` checks for Turnstile iframe, Turnstile container, "Just a moment" page title, reCAPTCHA iframe, login wall. Returns `ChallengeType` enum.

## Phase 4 — Human Simulation + Response Poller ✅

- [x] `src/workers/human_sim.py` — `HumanSimulator` with `type_text()` (per-keystroke random delay), `click()` (mouse move to random point in bounding box + pause), `random_delay()` (jitter-aware sleep)
- [x] `src/workers/response_poller.py` — `ResponsePoller.poll()` — polls left + right response panels every N seconds. Text must be stable for M consecutive polls to be considered complete. Extracts model names. Raises `PollingTimeoutError` on timeout.

## Phase 5 — ArenaWorker ✅

- [x] `src/workers/arena_worker.py` — `ArenaWorker` manages full lifecycle per window:
  - `navigate_to_arena()` — launch → navigate → handle challenges → ready
  - `submit_prompt()` — optional model selection → paste → submit → transition to polling
  - `poll_for_completion()` — poll DOM → extract responses → emit WORKER_COMPLETE
  - State machine callback publishes `WORKER_STATE_CHANGED` on every transition
  - Error handling: creates failed `WindowResult`, emits `WORKER_ERROR`, does not propagate

## Phase 6 — Orchestrator ✅

- [x] `src/orchestrator/run_orchestrator.py` — `RunOrchestrator.execute_run()`:
  1. Launch N browser contexts (parallel)
  2. Navigate all to Arena (parallel, errors per-worker)
  3. Submit prompts sequentially with jittered gap
  4. Poll all submitted workers in parallel
  5. Collect results → emit `RUN_COMPLETE`
  - Cancellation: sets flag, cancels workers, emits `RUN_CANCELLED`
  - Error isolation: one worker failing never kills the run

## Phase 7 — WebSocket Transport ✅

- [x] `src/transport/ws_handler.py` — `WsHandler.handle()` — parses inbound JSON, dispatches `start_run`/`stop_run`/`ping`. Runs orchestrator as background asyncio task.
- [x] `src/transport/ws_broadcaster.py` — `WsBroadcaster` subscribes to all EventBus events, converts to typed outbound WS messages, broadcasts to all connected clients. Dead clients auto-cleaned.

**Key design**: WsBroadcaster is the ONLY module that converts internal events to WebSocket messages. Workers and orchestrator never touch WebSocket.

## Phase 8 — GUI + Excel Export ✅

- [x] `templates/index.html` — Dashboard with settings panel (windows, gap, model L/R), prompt textarea, Start/Stop/Export buttons, progress bar, worker cards grid, log box, results table
- [x] `static/style.css` — Dark theme, responsive grid, state-dependent card borders (green=complete, red=error, blue=polling, yellow=challenge)
- [x] `static/app.js` — WebSocket client with auto-reconnect, message handlers for all outbound types, dynamic worker card creation, log appending, results table population
- [x] `src/export/excel_exporter.py` — `export_to_excel()` generates styled `.xlsx` with Results sheet (per-window data) + Summary sheet (run metadata)

## Phase 9 — App Wiring ✅

- [x] `app.py` — FastAPI app with `lifespan` context manager for startup/shutdown. Routes: `GET /` (dashboard), `WS /ws` (WebSocket), `GET /export` (Excel download). Wires EventBus → WsBroadcaster → WsHandler → RunOrchestrator → BrowserManager.
- [x] `requirements.txt` — fastapi, uvicorn, playwright, playwright-stealth, openpyxl, pyyaml, pydantic
- [x] `.gitignore` — browser_profiles/, outputs/, __pycache__/, .venv/

---

## Blockers

- **Arena DOM selectors are placeholders** — `config/selectors.yaml` has best-guess selectors based on common patterns and LMArenaBridge reference. Must inspect the live site (`https://arena.ai/text/side-by-side`) in DevTools to get actual selectors for: prompt textarea, submit button, model dropdowns, response panels, model name labels.

## How to Run

```bash
# Setup
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Run
python app.py
# Open http://localhost:8000
```

## Key Learnings / Context

- **LMArenaBridge reference**: Arena uses Cloudflare Turnstile + reCAPTCHA v3/v2. Multiple challenge layers. `cf_clearance` and `arena-auth-prod-v1` cookies are critical. Persistent browser profiles essential.
- **Architecture**: EventBus pub/sub decouples workers from WebSocket transport. Workers never know about WebSocket. Transport never knows about Playwright.
- **State machine**: 12 states, enforced transitions via lookup table. Each state maps to a progress percentage. Callback fires on every transition for real-time GUI updates.
- **Persistent contexts**: `launch_persistent_context()` with separate `user_data_dir` per window. One process per context (trade-off for cookie persistence).
- **Error isolation**: One window failing never kills the run. Orchestrator wraps each worker in individual exception handlers.
