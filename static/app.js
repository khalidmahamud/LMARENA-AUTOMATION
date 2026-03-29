// LM Arena Automation — WebSocket client & DOM controller

(function () {
  "use strict";

  // ── State ──
  let ws = null;
  let connected = false;
  let running = false;

  // ── DOM refs ──
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const windowCountInput = document.getElementById("window-count");
  const submissionGapInput = document.getElementById("submission-gap");
  const modelLeftInput = document.getElementById("model-left");
  const modelRightInput = document.getElementById("model-right");
  const promptInput = document.getElementById("prompt");
  const startBtn = document.getElementById("btn-start");
  const stopBtn = document.getElementById("btn-stop");
  const exportBtn = document.getElementById("btn-export");
  const workersContainer = document.getElementById("workers");
  const progressBar = document.getElementById("progress-fill");
  const progressText = document.getElementById("progress-text");
  const logBox = document.getElementById("log-box");
  const resultsBody = document.getElementById("results-body");
  const resultsSection = document.getElementById("results-section");

  // ── WebSocket ──

  function connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws`);

    ws.onopen = () => {
      connected = true;
      setStatus("connected", "Connected");
    };

    ws.onclose = () => {
      connected = false;
      setStatus("disconnected", "Disconnected");
      setTimeout(connect, 3000); // auto-reconnect
    };

    ws.onerror = () => {
      setStatus("disconnected", "Error");
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      handleMessage(msg);
    };
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  // ── Message handlers ──

  function handleMessage(msg) {
    switch (msg.type) {
      case "worker_update":
        updateWorkerCard(msg);
        break;
      case "run_progress":
        updateProgress(msg);
        break;
      case "run_complete":
        onRunComplete(msg);
        break;
      case "challenge_detected":
        appendLog("warning", msg.message, msg.worker_id);
        highlightWorker(msg.worker_id, "challenge");
        break;
      case "log":
        appendLog(msg.level, msg.text, msg.worker_id);
        break;
      case "error":
        appendLog("error", msg.message);
        break;
      case "pong":
        break;
    }
  }

  // ── Worker cards ──

  function ensureWorkerCard(id) {
    let card = document.getElementById(`worker-${id}`);
    if (!card) {
      card = document.createElement("div");
      card.id = `worker-${id}`;
      card.className = "worker-card";
      card.innerHTML = `
        <div class="worker-header">Window #${id + 1}</div>
        <div class="worker-state">idle</div>
        <div class="worker-progress-bar"><div class="worker-progress-fill"></div></div>
        <div class="worker-message"></div>
      `;
      workersContainer.appendChild(card);
    }
    return card;
  }

  function updateWorkerCard(msg) {
    const card = ensureWorkerCard(msg.worker_id);
    const stateEl = card.querySelector(".worker-state");
    const fillEl = card.querySelector(".worker-progress-fill");
    const msgEl = card.querySelector(".worker-message");

    stateEl.textContent = msg.state;
    fillEl.style.width = `${msg.progress_pct}%`;
    msgEl.textContent = msg.error || msg.message || "";

    // State-dependent styling
    card.className = "worker-card";
    if (msg.state === "complete") card.classList.add("state-complete");
    else if (msg.state === "error") card.classList.add("state-error");
    else if (msg.state === "polling") card.classList.add("state-polling");
    else if (msg.state === "waiting_for_challenge")
      card.classList.add("state-challenge");
  }

  function highlightWorker(id, cls) {
    const card = ensureWorkerCard(id);
    card.classList.add(`state-${cls}`);
  }

  // ── Progress ──

  function updateProgress(msg) {
    const pct = msg.overall_pct;
    progressBar.style.width = `${pct}%`;
    progressText.textContent = `${msg.completed_workers} / ${msg.total_workers} submitted`;
  }

  // ── Run complete ──

  function onRunComplete(msg) {
    running = false;
    startBtn.disabled = false;
    stopBtn.disabled = true;
    exportBtn.disabled = false;
    progressBar.style.width = "100%";
    progressText.textContent = "Complete";

    // Populate results table
    resultsBody.innerHTML = "";
    msg.results.forEach((r) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${r.worker_id + 1}</td>
        <td>${r.model_a_name || "—"}</td>
        <td>${r.model_b_name || "—"}</td>
        <td class="response-cell">${escapeHtml(r.response_a || "—")}</td>
        <td class="response-cell">${escapeHtml(r.response_b || "—")}</td>
        <td>${r.elapsed_seconds ? r.elapsed_seconds.toFixed(1) + "s" : "—"}</td>
        <td>${r.error ? '<span class="error-badge">Error</span>' : '<span class="success-badge">OK</span>'}</td>
      `;
      resultsBody.appendChild(tr);
    });
    resultsSection.style.display = "block";
    appendLog("info", `Run complete — ${msg.results.length} window(s) finished in ${msg.total_elapsed_seconds.toFixed(1)}s`);
  }

  // ── Log ──

  function appendLog(level, text, workerId) {
    const time = new Date().toLocaleTimeString();
    const prefix = workerId !== undefined && workerId !== null ? `[W${workerId}] ` : "";
    const line = document.createElement("div");
    line.className = `log-line log-${level}`;
    line.textContent = `${time} ${prefix}${text}`;
    logBox.appendChild(line);
    logBox.scrollTop = logBox.scrollHeight;
  }

  // ── Status indicator ──

  function setStatus(cls, text) {
    statusDot.className = `dot ${cls}`;
    statusText.textContent = text;
  }

  // ── Controls ──

  startBtn.addEventListener("click", () => {
    const prompt = promptInput.value.trim();
    if (!prompt) {
      promptInput.focus();
      return;
    }

    running = true;
    startBtn.disabled = true;
    stopBtn.disabled = false;
    exportBtn.disabled = true;
    resultsSection.style.display = "none";
    workersContainer.innerHTML = "";
    logBox.innerHTML = "";
    progressBar.style.width = "0%";
    progressText.textContent = "";

    const windowCount = parseInt(windowCountInput.value, 10) || 2;

    // Pre-create worker cards
    for (let i = 0; i < windowCount; i++) ensureWorkerCard(i);

    send({
      type: "start_run",
      prompt: prompt,
      window_count: windowCount,
      submission_gap_seconds: parseFloat(submissionGapInput.value) || null,
      model_left: modelLeftInput.value.trim() || null,
      model_right: modelRightInput.value.trim() || null,
    });

    appendLog("info", `Starting run with ${windowCount} window(s)...`);
  });

  stopBtn.addEventListener("click", () => {
    send({ type: "stop_run" });
    appendLog("warning", "Stop requested...");
  });

  exportBtn.addEventListener("click", () => {
    window.location.href = "/export";
  });

  // ── Helpers ──

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  // ── Init ──
  connect();
})();
