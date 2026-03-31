# Arena Runner

Automated batch testing platform for [LM Arena](https://arena.ai/) — orchestrates parallel browser windows to submit prompts, capture side-by-side model responses, and export structured results.

## What It Does

Arena Runner automates the manual process of using LM Arena's side-by-side comparison interface. It launches multiple browser windows in parallel, submits prompts (single or batch from CSV/Excel), polls for responses, and collects results into exportable formats. Everything runs through a real-time dashboard with live progress tracking.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Start the server
python app.py
```

Open **http://localhost:8000** in your browser.

## Features

### Multi-Window Parallel Execution
- Launch 1–12 browser windows simultaneously
- Auto-tile windows across monitors for optimal layout
- Each window runs in an isolated browser context with separate cookies
- Persistent browser profiles survive restarts

### Prompt Modes
- **Manual**: Type or paste a single prompt
- **File Upload**: Upload CSV/Excel files for batch processing
  - Select which columns to combine into prompts
  - Specify row ranges (e.g., rows 5–50)
  - Automatic batching across available windows
- **System Prompt**: Optional system prompt sent before each user prompt (or combined as one message)
- **Image Attachments**: Drag-and-drop up to 10 images (PNG, JPEG, WebP, GIF) onto the prompt box

### Orchestration
- Sequential submission with configurable gap timing (default 30s) and jitter
- Parallel response polling across all windows
- Automatic batching: M prompts across N windows = ceil(M/N) batches
- Response stability detection (text unchanged for 3 consecutive polls)
- 5-minute response timeout per window

### Checkpointing & Resume
- Auto-saves checkpoint after each batch completes
- Resume interrupted runs from the exact batch where they stopped
- Preserves all partial results and run metadata

### Challenge Detection
- Detects Cloudflare Turnstile, Google reCAPTCHA, login walls, and rate limits
- Logs challenge events and supports pause/resume for manual intervention

### Data Export
| Format | Description |
|--------|-------------|
| **Excel (.xlsx)** | Styled Results sheet + Summary sheet with run metadata |
| **CSV (.csv)** | Flat rows for spreadsheet import |
| **JSON (.json)** | Structured data for programmatic analysis |

### HTML Preview
- Extract HTML code blocks from model responses
- Full-screen preview modal with carousel navigation between windows
- Side-by-side Compare mode for both models
- Zoom controls: Fit / 1:1 / 75% / 50%
- Copy raw HTML source to clipboard

### Real-Time Dashboard
Three-zone layout with live WebSocket updates:
- **Left Sidebar**: Configuration controls + window status cards
- **Center**: Prompt input, progress bar, results (Table / JSON / HTML tabs)
- **Right Sidebar**: Live log with per-worker entries

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | FastAPI + Uvicorn |
| Browser Automation | Playwright (Chromium, headed) |
| Anti-Detection | playwright-stealth |
| Real-Time Updates | WebSocket (native FastAPI) |
| Frontend | HTML + CSS + Vanilla JS (no build step) |
| Data Export | openpyxl (Excel), built-in CSV/JSON |
| Configuration | YAML + Pydantic validation |

## Project Structure

```
arena-runner/
├── app.py                        # FastAPI server, routes, WebSocket handler
├── config/
│   ├── default_config.yaml       # Default settings (windows, timing, display)
│   └── selectors.yaml            # Arena DOM selectors (centralized)
├── src/
│   ├── models/                   # Pydantic data models
│   │   ├── config.py             # AppConfig, DisplayConfig, TimingConfig
│   │   ├── messages.py           # WebSocket protocol (inbound + outbound)
│   │   ├── results.py            # WindowResult, RunResult, ExportableRow
│   │   └── worker.py             # WorkerState enum (12 states)
│   ├── core/                     # Business logic (no I/O)
│   │   ├── events.py             # Async EventBus (pub/sub)
│   │   ├── exceptions.py         # Exception hierarchy
│   │   ├── state_machine.py      # Worker state transitions
│   │   └── tiling.py             # Window layout computation
│   ├── browser/                  # Playwright interaction
│   │   ├── manager.py            # Browser context creation + stealth
│   │   ├── stealth.py            # Anti-detection patches
│   │   ├── selectors.py          # SelectorRegistry singleton
│   │   └── challenges.py         # Challenge detection logic
│   ├── workers/                  # Per-window Arena interaction
│   │   ├── arena_worker.py       # Full worker lifecycle
│   │   ├── human_sim.py          # Human-like typing and delays
│   │   └── response_poller.py    # DOM polling for stable responses
│   ├── orchestrator/
│   │   └── run_orchestrator.py   # Central coordinator + batch logic
│   ├── checkpoint/
│   │   └── manager.py            # Run persistence for resumption
│   ├── transport/                # WebSocket layer
│   │   ├── ws_handler.py         # Message parsing + dispatch
│   │   └── ws_broadcaster.py     # Event-to-WebSocket conversion
│   └── export/
│       └── excel_exporter.py     # Generate .xlsx/.csv/.json
├── templates/
│   └── index.html                # Dashboard UI
├── static/
│   ├── app.js                    # WebSocket client + DOM controller
│   └── style.css                 # Terminal-Luxe dark theme
├── outputs/                      # Export files (gitignored)
├── browser_profiles/             # Persistent cookies (gitignored)
└── requirements.txt
```

## Configuration

All settings are configurable from the dashboard UI. Defaults come from `config/default_config.yaml`:

```yaml
browser:
  window_count: 2
  window_size: { width: 900, height: 800 }
  headless: false
  incognito: false

display:
  monitor_count: 1
  monitor_width: 1920
  monitor_height: 1080
  taskbar_height: 40
  margin: 0

timing:
  submission_gap_seconds: 30.0
  jitter_pct: 0.30
  poll_interval_seconds: 2.0
  stable_polls_required: 3
  response_timeout_seconds: 300.0

arena_url: "https://arena.ai/text/side-by-side"
```

### Settings Modal Options
- **Model Selection**: Override Model A / Model B, retain output from one or both
- **Browser**: Zoom level, clear cookies, incognito mode, parallel prepare
- **Display & Tiling**: Monitor count/dimensions, taskbar height, margin

## Architecture

```
User Input (Dashboard)
    ↓
WebSocket → WsHandler → RunOrchestrator
    ↓
ArenaWorker × N (parallel browser windows)
    ↓
EventBus → WsBroadcaster → WebSocket → Dashboard
```

**Key principles:**
- **Event-driven**: Workers publish events, broadcaster converts to WebSocket messages
- **Error isolation**: One worker failing never kills the run
- **Separation of concerns**: Transport layer never imports browser/worker code
- **Async throughout**: All I/O is async with proper cleanup

## Keyboard Shortcuts

### HTML Preview Modal
| Key | Action |
|-----|--------|
| `←` / `→` | Previous / next window |
| `1` / `2` / `3` | Model A / Model B / Compare |
| `f` | Fit to viewport |
| `0` | 1:1 actual size |
| `Esc` | Close modal |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard UI |
| `GET` | `/ws` | WebSocket connection |
| `POST` | `/upload-prompts` | Parse CSV/Excel file |
| `GET` | `/export` | Download results as Excel |
| `GET` | `/export-csv` | Download results as CSV |
| `GET` | `/export-json` | Download results as JSON |
| `GET` | `/api/run-state` | Current run state (for reconnect) |
| `GET` | `/api/checkpoints` | List resumable checkpoints |
| `DELETE` | `/api/checkpoints/{id}` | Discard a checkpoint |

## License

Private project. All rights reserved.
