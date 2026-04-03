# Arena Runner

Automated batch testing platform for [LM Arena](https://arena.ai/) — orchestrates parallel browser windows to submit prompts, capture side-by-side model responses, and export structured results. Supports distributed execution across multiple machines.

## What It Does

Arena Runner automates the manual process of using LM Arena's side-by-side comparison interface. It launches multiple browser windows in parallel, submits prompts (single or batch from CSV/Excel), polls for responses, and collects results into exportable formats. Everything runs through a real-time dashboard with live progress tracking.

---

## Quick Start

### Prerequisites

- **Python 3.10+**
- **Git**
- A display server (X11/Wayland) for headed mode, or use `--headless`

### Installation

```bash
git clone <repo-url>
cd LMARENA-AUTOMATION

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate    # Linux/macOS
# .venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### Run

```bash
python app.py
```

Open **http://localhost:8000** in your browser.

### First Run

1. Type a prompt in the text area (e.g. "Compare Python vs Rust for web development")
2. Set **Windows** to 2 (or more)
3. Click **Start Run**
4. Browser windows will open, navigate to Arena, submit your prompt, and poll for responses
5. Results appear in the dashboard in real-time
6. Click **Export** to download as Excel/CSV/JSON

---

## Features

### Multi-Window Parallel Execution
- Launch 1-12 browser windows simultaneously
- Auto-tile windows across monitors for optimal layout
- Each window runs in an isolated browser context with separate cookies
- Configurable window size and zoom level

### Prompt Modes
- **Manual**: Type or paste a single prompt
- **File Upload**: Upload CSV/Excel files for batch processing
  - Select which columns to combine into prompts
  - Specify row ranges (e.g. rows 5-50)
  - Automatic batching across available windows
- **Instruction Load**: Upload JSON/CSV/Excel instruction files with per-row overrides (window count, models, images, multi-turn conversations)
- **System Prompt**: Optional system prompt sent before each user prompt (or combined as one message)
- **Image Attachments**: Drag-and-drop up to 10 images (PNG, JPEG, WebP, GIF)
- **Multi-Turn Conversations**: Define sequential turns via instruction files or numbered columns (prompt_1, prompt_2, ...)

### Orchestration
- Sequential submission with configurable gap timing (default 30s) and jitter
- Parallel response polling across all windows
- Automatic batching: M prompts across N windows = ceil(M/N) batches
- Simultaneous start mode: prepare all windows first, then submit in sequence
- Response stability detection (text unchanged for 3 consecutive polls)
- 5-minute response timeout per window

### Proxy Support
- Manual proxy list (HTTP, HTTPS, SOCKS4, SOCKS5)
- Auto-fetch free proxies from proxifly with health checking
- Smart proxy distribution: rotate proxy on challenge detection
- Proxy pool with auto-refresh, health monitoring, and persistence
- Configurable max pool size and latency thresholds

### Challenge Detection & Recovery
- Detects Cloudflare Turnstile, Google reCAPTCHA, login walls, and rate limits
- Automatic recovery: recreates browser context with fresh proxy
- Logs challenge events in real-time
- Supports pause/resume for manual intervention

### Checkpointing & Resume
- Auto-saves checkpoint after each batch completes
- Resume interrupted runs from the exact batch where they stopped
- Preserves all partial results and run metadata

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
- **Left Sidebar**: Configuration, proxy pool tracker, worker node status, window cards
- **Center**: Prompt input, progress bar, results (Table / JSON / HTML tabs)
- **Right Sidebar**: Processing + Proxy logs with auto-scroll

---

## Distributed Mode

Run browser windows across multiple machines. One machine acts as the **coordinator** (serves the dashboard), and other machines act as **worker nodes** (run the browsers).

### Why Use Distributed Mode?
- Scale beyond one machine's 12-window limit
- Run browsers on machines closer to different regions/proxies
- Use a headless server as coordinator, with browsers on desktop machines

### Setup

#### 1. Configure the Coordinator

Add to `config/default_config.yaml`:

```yaml
distributed:
  enabled: true
  auth_token: "your-secret-token"     # generate with: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  scheduling_policy: "spread"          # "fill" (pack fewest nodes) or "spread" (distribute evenly)
  heartbeat_interval_seconds: 5
  heartbeat_timeout_missed: 3
  reconnect_grace_seconds: 30
  event_coalesce_ms: 100
  allow_local_workers: false           # true = also run browsers on coordinator
```

Start the coordinator:

```bash
python app.py
```

#### 2. Set Up Worker Nodes

On each worker machine, install the project (same steps as Quick Start), then run:

```bash
python worker_node.py \
  --coordinator ws://COORDINATOR_IP:8000/node-ws \
  --node-id worker-1 \
  --max-workers 8 \
  --token "your-secret-token"
```

**Worker node flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--coordinator` | `ws://localhost:8001/node-ws` | Coordinator WebSocket URL |
| `--node-id` | hostname | Unique name for this node |
| `--max-workers` | 12 | Max browser windows on this machine |
| `--token` | `""` | Must match coordinator's `auth_token` |
| `--headless` | false | Run browsers without GUI |
| `--display-monitors` | 1 | Number of monitors for tiling |
| `--display-width` | 1920 | Monitor width in pixels |
| `--display-height` | 1080 | Monitor height in pixels |
| `--log-level` | INFO | DEBUG / INFO / WARNING / ERROR |

Environment variables (alternative to flags):
```bash
export LM_ARENA_COORDINATOR="ws://192.168.1.10:8000/node-ws"
export LM_ARENA_NODE_TOKEN="your-secret-token"
```

#### 3. Networking

Worker nodes must be able to reach the coordinator's IP and port. Options:

| Setup | How |
|-------|-----|
| **Same LAN** | Use local IP (e.g. `192.168.x.x`) |
| **Different networks** | Use a VPN like [Tailscale](https://tailscale.com/) (free, easiest) |
| **Cloud coordinator** | Run `app.py` on a VPS, workers connect from anywhere |
| **Port forwarding** | Forward port 8000 on coordinator's router |

**Tailscale setup (recommended for remote):**
```bash
# Install on ALL machines
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip   # get your Tailscale IP (100.x.y.z)

# Worker connects via Tailscale IP
python worker_node.py --coordinator ws://100.x.y.z:8000/node-ws ...
```

#### 4. Dashboard

Once nodes connect:
- **Worker Nodes** panel appears in the left sidebar
- Each worker card shows which node it runs on
- **Settings > Distributed Mode** lets you switch scheduling policy and view the auth token
- Logs show node connect/disconnect events

#### 5. How Work Is Distributed

| Scenario | Behavior |
|----------|----------|
| 1 prompt, 4 windows, 2 nodes | Each node gets 2 windows, all submit same prompt (different model pairs) |
| 10 prompts, 4 windows/batch, 2 nodes | Work is split: 2 prompts per node per batch, each prompt runs once |
| Node disconnects mid-run | Workers are reassigned to healthy nodes |

### Fault Tolerance

- **Heartbeat monitoring**: nodes are pinged every 5s; declared dead after 3 misses (15s)
- **Automatic reassignment**: work from dead nodes is redistributed
- **Result buffering**: completed results are persisted until acknowledged
- **Reconnection**: nodes reconnect with exponential backoff and replay unacked results
- **Epoch fencing**: prevents duplicate results from stale connections

---

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

### Settings Modal
- **Model Selection**: Override Model A / Model B, retain output from one or both
- **Browser**: Zoom level, clear cookies, incognito mode, simultaneous start
- **Proxies**: Manual proxy list, auto-fetch, smart distribution, pool management
- **Display & Tiling**: Monitor count/dimensions, taskbar height, margin
- **Distributed Mode**: Scheduling policy, auth token, connected node status

---

## Architecture

```
User Input (Dashboard)
    |
WebSocket --> WsHandler --> RunOrchestrator (local) or DistributedOrchestrator (distributed)
    |                              |                              |
    |                     ArenaWorker x N              Send to remote nodes
    |                     (local browsers)             via NodeConnectionHandler
    |                              |                              |
    |                              v                              v
EventBus --> WsBroadcaster --> WebSocket --> Dashboard    Remote: BrowserManager
                                                                + ArenaWorker
                                                                + EventForwarder
```

**Key principles:**
- **Event-driven**: Workers publish events, broadcaster converts to WebSocket messages
- **Error isolation**: One worker failing never kills the run
- **Separation of concerns**: Transport layer never imports browser/worker code
- **Async throughout**: All I/O is async with proper cleanup
- **ArenaWorker is unchanged**: Same class runs locally or on remote nodes

## Project Structure

```
LMARENA-AUTOMATION/
├── app.py                          # FastAPI server, routes, WebSocket handler
├── worker_node.py                  # Worker node entry point (distributed mode)
├── config/
│   ├── default_config.yaml         # Default settings
│   └── selectors.yaml              # Arena DOM selectors (centralized)
├── src/
│   ├── models/                     # Pydantic data models
│   │   ├── config.py               # AppConfig, DistributedConfig
│   │   ├── messages.py             # WebSocket protocol (inbound + outbound)
│   │   ├── results.py              # WindowResult, RunResult
│   │   └── worker.py               # WorkerState enum (12 states)
│   ├── core/                       # Business logic (no I/O)
│   │   ├── events.py               # Async EventBus (pub/sub)
│   │   ├── exceptions.py           # Exception hierarchy
│   │   ├── state_machine.py        # Worker state transitions
│   │   └── tiling.py               # Window layout computation
│   ├── browser/                    # Playwright interaction
│   │   ├── manager.py              # Browser context creation + stealth
│   │   ├── stealth.py              # Anti-detection patches
│   │   ├── selectors.py            # SelectorRegistry singleton
│   │   └── challenges.py           # Challenge detection logic
│   ├── workers/                    # Per-window Arena interaction
│   │   ├── arena_worker.py         # Full worker lifecycle (12-state FSM)
│   │   ├── human_sim.py            # Human-like typing delays
│   │   └── response_poller.py      # DOM polling for stable responses
│   ├── orchestrator/
│   │   └── run_orchestrator.py     # Local run coordinator + batch logic
│   ├── distributed/                # Distributed execution
│   │   ├── protocol.py             # Message types for coordinator <-> node
│   │   ├── coordinator.py          # NodeRegistry, connection handler, heartbeat
│   │   ├── distributed_orchestrator.py  # Distributed run coordinator
│   │   ├── node_client.py          # Worker node WebSocket client
│   │   └── event_forwarder.py      # Local EventBus -> coordinator bridge
│   ├── checkpoint/
│   │   └── manager.py              # Run persistence for resumption
│   ├── transport/
│   │   ├── ws_handler.py           # Dashboard WebSocket dispatch
│   │   └── ws_broadcaster.py       # Event-to-WebSocket conversion
│   ├── proxy/
│   │   └── pool.py                 # Proxy pool with health tracking
│   └── export/
│       └── excel_exporter.py       # Generate .xlsx/.csv/.json
├── templates/
│   └── index.html                  # Dashboard UI
├── static/
│   ├── app.js                      # WebSocket client + DOM controller
│   └── style.css                   # Terminal-Luxe dark theme
├── data/
│   └── proxy_pool.json             # Persisted proxy pool
├── outputs/                        # Export files (gitignored)
└── requirements.txt
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard UI |
| `WS` | `/ws` | Dashboard WebSocket |
| `WS` | `/node-ws` | Worker node WebSocket (distributed mode) |
| `POST` | `/upload-prompts` | Parse CSV/Excel file |
| `POST` | `/upload-instructions` | Parse instruction file + images |
| `GET` | `/export` | Download results as Excel |
| `GET` | `/export-csv` | Download results as CSV |
| `GET` | `/export-json` | Download results as JSON |
| `GET` | `/api/run-state` | Current run state (for reconnect) |
| `GET` | `/api/nodes` | Connected worker nodes + distributed config |
| `GET` | `/api/checkpoints` | List resumable checkpoints |
| `DELETE` | `/api/checkpoints/{id}` | Discard a checkpoint |
| `GET` | `/api/proxy-pool/status` | Proxy pool health |
| `POST` | `/api/proxy-pool/add` | Add proxies to pool |
| `POST` | `/api/proxy-pool/auto-refresh/start` | Start proxy auto-refresh |
| `POST` | `/api/proxy-pool/auto-refresh/stop` | Stop proxy auto-refresh |
| `POST` | `/api/proxy-pool/health-check` | Trigger pool health check |

## Keyboard Shortcuts

### HTML Preview Modal
| Key | Action |
|-----|--------|
| `<` / `>` | Previous / next window |
| `1` / `2` / `3` | Model A / Model B / Compare |
| `f` | Fit to viewport |
| `0` | 1:1 actual size |
| `Esc` | Close modal |

## Troubleshooting

### Browser windows don't open
- Make sure Playwright is installed: `playwright install chromium`
- On Linux without a display: use `--headless` flag or install Xvfb

### "Not enough worker nodes" error
- You're in distributed mode but no worker nodes are connected
- Either connect a worker node or set `distributed.enabled: false` in config

### Worker node can't connect
- Check that the coordinator IP/port is reachable: `curl http://COORDINATOR_IP:8000`
- Ensure the auth token matches on both sides
- Check firewall: `sudo ufw allow 8000` (Linux)

### Challenge detection loops
- Arena may be rate-limiting your IP — enable proxy support
- Use `proxy_on_challenge: true` for automatic proxy rotation

## License

Private project. All rights reserved.
