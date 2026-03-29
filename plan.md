# LM Arena Side-by-Side Automation — Implementation Plan

## Architecture

**Python Backend (FastAPI)** + **HTML/CSS/JS GUI (Browser-based)**

Run `python app.py` → open `localhost:8000` in browser → done. No build step.

### Why This Architecture

- Zero frontend tooling — plain HTML/CSS/JS, no React/Webpack/npm
- FastAPI gives us WebSocket support natively for live status updates
- Playwright runs server-side, GUI just displays status
- Single `python app.py` to start everything

### Reference

- [LMArenaBridge](https://github.com/CloudWaddie/LMArenaBridge) — prior art for Arena automation, reports on current Cloudflare/reCAPTCHA challenges

---

## Tech Stack

| Part | Tool |
|------|------|
| Backend | FastAPI + Uvicorn |
| Browser Automation | Playwright (Chromium, headed mode) |
| Anti-Detection | playwright-stealth |
| Live Updates | WebSocket (FastAPI native) |
| GUI | HTML + CSS + Vanilla JS |
| Excel Export | openpyxl |
| Config | YAML (pyyaml) |

---

## Project Structure

```
arena-runner/
├── app.py                  # FastAPI server + Playwright orchestration
├── browser_manager.py      # Browser launch, stealth, persistent profile, window tiling
├── arena_worker.py         # Per-window Arena interaction: paste, submit, wait, extract
├── human_sim.py            # Human-like behavior: typing, mouse, delays
├── config/
│   ├── default_config.yaml # Default settings (delays, window count, sizes)
│   └── selectors.yaml      # All Arena DOM selectors (centralized)
├── templates/
│   └── index.html          # Dashboard GUI
├── static/
│   └── style.css           # Styling
├── outputs/                # Excel files saved here (gitignored)
├── browser_profile/        # Persistent Chromium profile (gitignored)
├── requirements.txt
├── plan.md
├── progress.md
└── .gitignore
```

---

## GUI Design

### Top Section — Settings

- Number of browser windows (2/4/6/custom)
- Browser window size (width x height)
- Gap between submissions (default 30s, adjustable)
- Arena URL field (pre-filled with `https://arena.ai/text/side-by-side`)
- Model selection — left/right dropdowns (optional, can be pre-set in Arena)

### Middle Section — Prompt & Controls

- Large textarea for prompt input
- "Start Run" button
- "Stop" button

### Bottom Section — Live Status

- Per-window status cards:
  - Window #1: Pasting... → Submitted → Done
  - Window #2: Waiting...
  - Window #3: Queue
- Overall progress bar
- Real-time log box (what's happening in each window)

### Result Section

- Table: Window #, Model A Response, Model B Response, Time taken
- "Export to Excel" button

---

## Backend Flow

```
"Start Run" clicked (via WebSocket)
  │
  ├─ Playwright launches N browser windows
  │  └─ Each window: headed mode, stealth, persistent profile
  │  └─ Auto-tile windows on screen (fit to display)
  │
  ├─ Sequential submission loop (Window 1 → N):
  │  ├─ Find prompt textarea → paste/type prompt
  │  ├─ Click Submit
  │  ├─ Send status update via WebSocket ("Window #X: Submitted")
  │  ├─ Wait configurable gap (default 30s ± jitter)
  │  └─ Next window
  │
  ├─ Parallel polling (all windows):
  │  ├─ Poll each window's response panels every 1-2s
  │  ├─ Response complete = text stable for 3+ consecutive polls
  │  ├─ Send status update via WebSocket ("Window #X: Done")
  │  └─ Extract left + right response text
  │
  ├─ Collect all results
  │  └─ Send results to GUI via WebSocket
  │
  └─ Excel export ready in outputs/
```

---

## Phases

### Phase 1 — Project Scaffolding & Server

**Goal:** FastAPI server running, serves the dashboard GUI, basic WebSocket connection established.

| Step | File(s) | Details |
|------|---------|---------|
| 1.1 | `requirements.txt` | fastapi, uvicorn, playwright, playwright-stealth, openpyxl, pyyaml |
| 1.2 | `.gitignore` | browser_profile/, outputs/, __pycache__/, *.pyc |
| 1.3 | `app.py` | FastAPI app with routes: `GET /` (serve index.html), `WS /ws` (WebSocket endpoint) |
| 1.4 | `templates/index.html` | Dashboard layout — settings panel, prompt area, status cards, results table |
| 1.5 | `static/style.css` | Clean dashboard styling |
| 1.6 | `config/default_config.yaml` | window_count, window_size, submission_gap, jitter_pct, arena_url |

### Phase 2 — Browser Management

**Goal:** Launch N headed Playwright browsers with stealth, tiled on screen, persistent profile.

| Step | File(s) | Details |
|------|---------|---------|
| 2.1 | `browser_manager.py` | `launch_browsers(n, size)` — launch N Chromium contexts with playwright-stealth, persistent profile |
| 2.2 | `browser_manager.py` | `tile_windows(n, screen_size)` — calculate grid positions, move/resize each window to tile on screen |
| 2.3 | `browser_manager.py` | Navigate all windows to Arena side-by-side URL |
| 2.4 | `browser_manager.py` | Cloudflare challenge detection — if any window hits a challenge, pause and notify GUI via WebSocket |

### Phase 3 — Human Simulation & Arena Interaction

**Goal:** Human-like prompt entry, submission, and response extraction per window.

| Step | File(s) | Details |
|------|---------|---------|
| 3.1 | `human_sim.py` | `human_type(page, selector, text)` — press_sequentially with 50-150ms random delay per keystroke |
| 3.2 | `human_sim.py` | `human_click(page, selector)` — mouse move + pause + click |
| 3.3 | `human_sim.py` | `random_delay(base, jitter_pct)` — sleep with ±jitter |
| 3.4 | `arena_worker.py` | `paste_prompt(page, text)` — click textarea, type prompt (human-like) |
| 3.5 | `arena_worker.py` | `submit_prompt(page)` — click send or press Enter |
| 3.6 | `arena_worker.py` | `select_model(page, panel, model_name)` — optional, handle custom dropdown |
| 3.7 | `arena_worker.py` | `wait_for_response(page, timeout)` — DOM polling, text stable for 3+ checks = done |
| 3.8 | `arena_worker.py` | `extract_responses(page)` — read left + right panel text |
| 3.9 | `config/selectors.yaml` | All Arena DOM selectors centralized here |

### Phase 4 — Orchestration & WebSocket Integration

**Goal:** Wire everything together — GUI controls Playwright via WebSocket, live status flows back.

| Step | File(s) | Details |
|------|---------|---------|
| 4.1 | `app.py` | WebSocket handler: receive "start" with config (prompt, window count, gap) |
| 4.2 | `app.py` | Orchestration: launch browsers → sequential submit (window 1→N with gap) → parallel poll → collect |
| 4.3 | `app.py` | Send per-window status updates via WebSocket (Pasting / Submitted / Polling / Done / Error) |
| 4.4 | `templates/index.html` | JS WebSocket client: update status cards, progress bar, log box in real-time |
| 4.5 | `app.py` | Error handling: if a window fails, log error, mark that window, continue others |

### Phase 5 — Results & Export

**Goal:** Display results in GUI, export to Excel.

| Step | File(s) | Details |
|------|---------|---------|
| 5.1 | `app.py` | After all windows done, send results payload via WebSocket |
| 5.2 | `templates/index.html` | Populate results table (Window #, Model A response, Model B response, time) |
| 5.3 | `app.py` | `GET /export` — generate Excel file with openpyxl, return as download |
| 5.4 | `templates/index.html` | "Export to Excel" button triggers download |

### Phase 6 — Resilience & Polish

**Goal:** Handle edge cases, improve UX.

| Step | File(s) | Details |
|------|---------|---------|
| 6.1 | Selector health check on startup — verify critical selectors exist |
| 6.2 | Login wall detection — pause for manual login, save to persistent profile |
| 6.3 | Graceful stop — "Stop" button cleanly kills browsers, saves partial results |
| 6.4 | Multiple rounds — after one run, user can paste new prompt and run again without restarting |
| 6.5 | Structured logging — log file in outputs/ alongside Excel |

---

## Streaming Completion Detection

| Method | Pros | Cons |
|--------|------|------|
| **DOM polling** (text stable 3s) | Reliable, works with streaming | Slightly slower |
| **Network idle detect** | Faster | Breaks with streaming, fragile |

**Decision:** DOM polling — poll every 1-2s, text unchanged for 3 consecutive polls = complete.

---

## Dependency List

| Package | Version | Purpose |
|---------|---------|---------|
| `fastapi` | >=0.100 | Web server + WebSocket |
| `uvicorn` | >=0.20 | ASGI server |
| `playwright` | >=1.40 | Browser automation |
| `playwright-stealth` | >=1.0 | Anti-bot-detection patches |
| `openpyxl` | >=3.1 | Excel export |
| `pyyaml` | >=6.0 | Config file parsing |

```bash
pip install fastapi uvicorn playwright playwright-stealth openpyxl pyyaml
playwright install chromium
```

---

## Requirement Traceability

| Requirement | Phase | Implementation |
|-------------|-------|----------------|
| FR-1 Open browser to Arena | P2 (2.3) | browser_manager.py |
| FR-2 Select left model | P3 (3.6) | arena_worker.py:select_model() |
| FR-3 Select right model | P3 (3.6) | arena_worker.py:select_model() |
| FR-4 Input prompt | P3 (3.4) | arena_worker.py:paste_prompt() |
| FR-5 Submit prompt | P3 (3.5) | arena_worker.py:submit_prompt() |
| FR-6 Detect streaming completion | P3 (3.7) | arena_worker.py:wait_for_response() |
| FR-7 Extract outputs | P3 (3.8) | arena_worker.py:extract_responses() |
| FR-8 Save to structured format | P5 (5.3) | Excel via openpyxl |
| FR-9 Multiple prompts sequentially | P6 (6.4) | Multiple rounds support |
| FR-10 Multiple model pairs | P3 (3.6) | Model selection per window |
| FR-11 Login/session persistence | P6 (6.2) | Persistent browser profile |
| FR-12 Configurable delays | P3 (3.3) | human_sim.py:random_delay() |
| FR-13 Parallel sessions | P2 (2.1) | N browser windows launched together |
| FR-14 Summary report | P5 (5.2) | Results table in GUI |
| FR-15 Human-like input | P3 (3.1-3.3) | human_sim.py |
| FR-16 Cloudflare bypass | P2 (2.1) | Headed mode + playwright-stealth |
| FR-17 reCAPTCHA handling | P3 (3.1-3.3) | Human-like behavior throughout |
| FR-18 Persistent profile | P2 (2.1) | launch_persistent_context() |
| FR-20 Batch size cap | P4 (4.2) | Config-driven window count |
| FR-21 Challenge detection | P2 (2.4) | Cloudflare detection + GUI alert |
| FR-22 Delay jitter | P3 (3.3) | ±30% jitter on all delays |

---

## Out of Scope (Initial Version)

- Arena battle mode automation (only side-by-side)
- Automated voting or leaderboard interaction (NFR-6)
- Proxy rotation or IP spoofing
- Automated CAPTCHA solving (manual intervention only)
- Headless mode (headed only — Cloudflare blocks headless)
