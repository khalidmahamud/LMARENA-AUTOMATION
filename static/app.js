// Arena Runner — WebSocket client & DOM controller

(function () {
  "use strict";

  // ── State ──
  let ws = null;
  let connected = false;
  let running = false;
  let autoScroll = true;
  let runStartTime = null;
  let runResults = null; // last run results for JSON view
  let workerStartTimes = {};
  let workerData = {};   // live data per worker
  let incrementalResults = {}; // worker_id -> result payload (available as each worker completes)

  // File upload state
  let promptMode = "manual"; // "manual" | "file"
  let uploadedRows = null;   // raw parsed rows from backend
  let uploadedPrompts = [];  // combined prompts built per row from selected columns

  // ── DOM refs ──
  const statusBadge       = document.getElementById("status-badge");
  const statusDot         = document.getElementById("status-dot");
  const statusText        = document.getElementById("status-text");
  const windowCountInput  = document.getElementById("window-count");
  const windowSizeInput   = document.getElementById("window-size");
  const submissionGapInput = document.getElementById("submission-gap");
  const arenaUrlInput     = document.getElementById("arena-url");
  const modelAInput       = document.getElementById("model-a");
  const modelBInput       = document.getElementById("model-b");
  const retainOutputInput = document.getElementById("retain-output");
  const zoomInput         = document.getElementById("zoom");
  const clearCookiesInput = document.getElementById("clear-cookies");
  const systemPromptInput = document.getElementById("system-prompt");
  const combineWithFirstInput = document.getElementById("combine-with-first");
  const promptInput       = document.getElementById("prompt");
  const monitorCountInput = document.getElementById("monitor-count");
  const monitorWidthInput = document.getElementById("monitor-width");
  const monitorHeightInput = document.getElementById("monitor-height");
  const taskbarHeightInput = document.getElementById("taskbar-height");
  const tileMarginInput   = document.getElementById("tile-margin");
  const tilePreviewLabel  = document.getElementById("tile-preview-label");
  const startBtn          = document.getElementById("btn-start");
  const stopBtn           = document.getElementById("btn-stop");
  const exportBtn         = document.getElementById("btn-export");
  const pasteBtn          = document.getElementById("btn-paste");
  const clearBtn          = document.getElementById("btn-clear");
  const settingsBtn       = document.getElementById("btn-settings");
  const closeSettingsBtn  = document.getElementById("btn-close-settings");
  const settingsModal     = document.getElementById("settings-modal");
  const workersContainer  = document.getElementById("workers");
  const progressFill      = document.getElementById("progress-fill");
  const progressPct       = document.getElementById("progress-pct");
  const etaText           = document.getElementById("eta-text");
  const logBox            = document.getElementById("log-box");
  const resultsBody       = document.getElementById("results-body");
  const resultsSection    = document.getElementById("results-section");
  const resultsTableWrap  = document.getElementById("results-table-wrap");
  const resultsJsonPre    = document.getElementById("results-json");
  const tabTable          = document.getElementById("tab-table");
  const tabJson           = document.getElementById("tab-json");
  const autoScrollToggle  = document.getElementById("auto-scroll-toggle");

  const toastContainer    = document.getElementById("toast-container");

  // Response modal DOM refs
  const responseModal     = document.getElementById("response-modal");
  const responseModalTitle = document.getElementById("response-modal-title");
  const responseFullText  = document.getElementById("response-full-text");
  const btnCopyResponse   = document.getElementById("btn-copy-response");
  const btnDownloadResponse = document.getElementById("btn-download-response");
  const btnCloseResponse  = document.getElementById("btn-close-response");

  // File upload DOM refs
  const modeManualBtn     = document.getElementById("mode-manual");
  const modeFileBtn       = document.getElementById("mode-file");
  const manualSection     = document.getElementById("manual-prompt-section");
  const fileSection       = document.getElementById("file-prompt-section");
  const uploadArea        = document.getElementById("upload-area");
  const fileInput         = document.getElementById("file-input");
  const fileInfoDiv       = document.getElementById("file-info");
  const fileNameSpan      = document.getElementById("file-name");
  const fileRowCountSpan  = document.getElementById("file-row-count");
  const removeFileBtn     = document.getElementById("btn-remove-file");
  const columnCheckboxes  = document.getElementById("column-checkboxes");
  const rowStartInput     = document.getElementById("row-start");
  const rowEndInput       = document.getElementById("row-end");
  const rowRangeInfo      = document.getElementById("row-range-info");
  const filePreviewDiv    = document.getElementById("file-preview");
  const batchInfoDiv      = document.getElementById("batch-info");

  // Footer stats
  const statRuns        = document.getElementById("stat-runs");
  const statAvgTime     = document.getElementById("stat-avg-time");
  const statSuccessRate = document.getElementById("stat-success-rate");
  const statTokens      = document.getElementById("stat-tokens");

  // ══════════════════════════════════════
  // WebSocket
  // ══════════════════════════════════════

  function connect() {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${protocol}//${location.host}/ws`);

    ws.onopen = () => {
      connected = true;
      setStatus(true);
    };

    ws.onclose = () => {
      connected = false;
      setStatus(false);
      setTimeout(connect, 3000);
    };

    ws.onerror = () => {
      setStatus(false);
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

  // ══════════════════════════════════════
  // Message Handlers
  // ══════════════════════════════════════

  function handleMessage(msg) {
    switch (msg.type) {
      case "worker_update":
        updateWorkerCard(msg);
        updateResultRow(msg);
        break;
      case "worker_result":
        onWorkerResult(msg.result);
        break;
      case "run_progress":
        updateProgress(msg);
        break;
      case "run_complete":
        onRunComplete(msg);
        break;
      case "run_cancelled":
        onRunCancelled();
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
        showToast(msg.message, "error");
        // Reset UI if run hasn't actually started
        if (running) {
          running = false;
          startBtn.disabled = false;
          stopBtn.disabled = true;
        }
        break;
      case "toast":
        showToast(msg.message, msg.level || "success");
        break;
      case "pong":
        break;
    }
  }

  // ══════════════════════════════════════
  // Status Indicator
  // ══════════════════════════════════════

  function setStatus(isConnected) {
    if (isConnected) {
      statusBadge.className = "status-badge";
      statusText.textContent = "Connected";
    } else {
      statusBadge.className = "status-badge disconnected";
      statusText.textContent = "Disconnected";
    }
  }

  // ══════════════════════════════════════
  // Window Cards (Right Column)
  // ══════════════════════════════════════

  function ensureWorkerCard(id) {
    let card = document.getElementById(`worker-${id}`);
    if (!card) {
      card = document.createElement("div");
      card.id = `worker-${id}`;
      card.className = "window-card";
      card.innerHTML = `
        <div class="window-card-header">
          <div class="window-card-left">
            <span class="window-dot"></span>
            <span class="window-title">Window #${id + 1}</span>
          </div>
          <span class="window-card-badge queued">&#9201; Queued</span>
        </div>
        <div class="window-progress">
          <div class="window-progress-fill"></div>
        </div>
        <div class="window-info">Waiting...</div>
      `;
      workersContainer.appendChild(card);
    }
    return card;
  }

  function updateWorkerCard(msg) {
    const card = ensureWorkerCard(msg.worker_id);
    const dot = card.querySelector(".window-dot");
    const badge = card.querySelector(".window-card-badge");
    const fill = card.querySelector(".window-progress-fill");
    const info = card.querySelector(".window-info");

    // Track start times
    if (!workerStartTimes[msg.worker_id] && msg.state !== "idle") {
      workerStartTimes[msg.worker_id] = Date.now();
    }

    // Update progress
    fill.style.width = `${msg.progress_pct}%`;

    // Store worker data
    workerData[msg.worker_id] = {
      state: msg.state,
      progress_pct: msg.progress_pct,
      message: msg.message || "",
      error: msg.error || null
    };

    // State-based styling
    card.className = "window-card";
    fill.className = "window-progress-fill";
    dot.className = "window-dot";
    badge.className = "window-card-badge";

    const elapsed = workerStartTimes[msg.worker_id]
      ? Math.round((Date.now() - workerStartTimes[msg.worker_id]) / 1000)
      : 0;

    if (msg.state === "complete") {
      card.classList.add("state-done");
      dot.classList.add("green");
      fill.classList.add("green");
      badge.classList.add("done");
      badge.innerHTML = "&#10003; Done";
      info.textContent = `${elapsed}s`;
    } else if (msg.state === "error") {
      card.classList.add("state-error");
      dot.classList.add("red");
      fill.classList.add("red");
      badge.classList.add("error");
      badge.innerHTML = "&#10007; Error";
      info.textContent = msg.error || "Failed";
    } else if (msg.state === "polling" || msg.state === "submitting" || msg.state === "pasting") {
      card.classList.add("state-active");
      dot.classList.add("orange");
      fill.classList.add("orange");
      badge.classList.add("active");
      badge.innerHTML = "&#8635; Generating...";
      info.textContent = `${elapsed}s elapsed`;
    } else if (msg.state === "waiting_for_challenge") {
      card.classList.add("state-challenge");
      dot.classList.add("orange");
      fill.classList.add("orange");
      badge.classList.add("challenge");
      badge.innerHTML = "&#9888; Challenge";
      info.textContent = msg.message || "Waiting...";
    } else if (msg.state === "idle") {
      badge.classList.add("queued");
      badge.innerHTML = "&#9201; Queued";
      info.textContent = "Waiting...";
    } else {
      // navigating, launching, selecting_model, ready, etc.
      dot.classList.add("orange");
      fill.classList.add("orange");
      badge.classList.add("active");
      badge.innerHTML = "&#8635; " + capitalize(msg.state.replace(/_/g, " "));
      info.textContent = `${elapsed}s elapsed`;
    }
  }

  function highlightWorker(id, cls) {
    const card = ensureWorkerCard(id);
    card.classList.add(`state-${cls}`);
  }

  // ══════════════════════════════════════
  // Results Table (Live Updates)
  // ══════════════════════════════════════

  function ensureResultRow(id) {
    let row = document.getElementById(`result-row-${id}`);
    if (!row) {
      row = document.createElement("tr");
      row.id = `result-row-${id}`;
      row.innerHTML = `
        <td class="row-num">W${id + 1}</td>
        <td class="col-model-a text-queued">Queued</td>
        <td class="col-model-b text-queued">Queued</td>
        <td class="col-time">&mdash;</td>
        <td class="col-tokens">&mdash;</td>
        <td class="col-status"><span class="badge badge-queued">&#9201; Queued</span></td>
      `;
      resultsBody.appendChild(row);
    }
    return row;
  }

  function updateResultRow(msg) {
    const row = ensureResultRow(msg.worker_id);
    const colA = row.querySelector(".col-model-a");
    const colB = row.querySelector(".col-model-b");
    const colTime = row.querySelector(".col-time");
    const colTokens = row.querySelector(".col-tokens");
    const colStatus = row.querySelector(".col-status");

    const elapsed = workerStartTimes[msg.worker_id]
      ? Math.round((Date.now() - workerStartTimes[msg.worker_id]) / 1000)
      : 0;

    if (msg.state === "complete") {
      colStatus.innerHTML = '<span class="badge badge-done">&#10003; Done</span>';
      colTime.textContent = `${elapsed}s`;
    } else if (msg.state === "error") {
      colStatus.innerHTML = '<span class="badge badge-error">&#10007; Error</span>';
      colA.className = "col-model-a";
      colA.textContent = msg.error || "Error";
      colB.className = "col-model-b";
      colB.textContent = "\u2014";
    } else if (msg.state === "polling" || msg.state === "submitting" || msg.state === "pasting") {
      colStatus.innerHTML = '<span class="badge badge-active">&#8635; Active</span>';
      colA.className = "col-model-a text-generating";
      colA.innerHTML = "&#8226; Generating...";
      colB.className = "col-model-b text-generating";
      colB.innerHTML = "&#8226; Generating...";
      colTime.textContent = `${elapsed}s`;
      colTokens.textContent = "\u2014";
    } else if (msg.state === "idle") {
      // Calculate estimated start time based on gap
      const gap = parseFloat(submissionGapInput.value) || 30;
      const startsIn = gap * msg.worker_id;
      colA.className = "col-model-a text-queued";
      colA.textContent = startsIn > 0 ? `Queued \u2014 starts in ~${startsIn}s` : "Queued";
      colB.className = "col-model-b text-queued";
      colB.textContent = "";
      colStatus.innerHTML = '<span class="badge badge-queued">&#9201; Queued</span>';
    } else {
      // navigating, launching, etc.
      colStatus.innerHTML = '<span class="badge badge-active">&#8635; Active</span>';
      colTime.textContent = elapsed > 0 ? `${elapsed}s` : "\u2014";
    }
  }

  function buildResultCellHTML(modelName, responseText, workerId, side) {
    const name = modelName || "\u2014";
    const text = responseText || "\u2014";
    const hasResponse = responseText && responseText !== "\u2014";
    return `
      <div class="response-cell">
        <span class="response-model-name">${escapeHtml(name)}</span>
        <span class="response-text-preview">${escapeHtml(truncate(text, 80))}</span>
        ${hasResponse ? `<div class="response-actions">
          <button class="btn-view-response" data-worker-id="${workerId}" data-side="${side}">&#128065; View</button>
          <button class="btn-copy-inline" data-worker-id="${workerId}" data-side="${side}">&#128203; Copy</button>
        </div>` : ""}
      </div>
    `;
  }

  function updateResultRowWithData(result) {
    const row = ensureResultRow(result.worker_id);
    const colA = row.querySelector(".col-model-a");
    const colB = row.querySelector(".col-model-b");
    const colTime = row.querySelector(".col-time");
    const colTokens = row.querySelector(".col-tokens");
    const colStatus = row.querySelector(".col-status");

    colA.className = "col-model-a";
    colA.innerHTML = buildResultCellHTML(result.model_a_name, result.model_a_response, result.worker_id, "a");

    colB.className = "col-model-b";
    colB.innerHTML = buildResultCellHTML(result.model_b_name, result.model_b_response, result.worker_id, "b");

    colTime.textContent = result.elapsed_seconds ? result.elapsed_seconds.toFixed(0) + "s" : "\u2014";

    const responseA = result.model_a_response || "";
    const responseB = result.model_b_response || "";
    const tokens = estimateTokens(responseA + responseB);
    colTokens.textContent = tokens > 0 ? formatNumber(tokens) : "\u2014";

    colStatus.innerHTML = result.error
      ? '<span class="badge badge-error">&#10007; Error</span>'
      : '<span class="badge badge-done">&#10003; Done</span>';
  }

  function onWorkerResult(result) {
    incrementalResults[result.worker_id] = result;
    updateResultRowWithData(result);
  }

  function populateFinalResults(results) {
    resultsBody.innerHTML = "";
    results.forEach((r) => {
      incrementalResults[r.worker_id] = r;
      ensureResultRow(r.worker_id);
      updateResultRowWithData(r);
    });
  }

  // ══════════════════════════════════════
  // Progress
  // ══════════════════════════════════════

  function updateProgress(msg) {
    const pct = Math.round(msg.overall_pct);
    progressFill.style.width = `${pct}%`;
    progressPct.textContent = `${pct}%`;

    // Enable export as soon as the first batch is done
    if (msg.phase === "batch_complete" && exportBtn.disabled) {
      exportBtn.disabled = false;
    }

    // Show batch progress in ETA label
    if (msg.batch && msg.total_batches && msg.total_batches > 1) {
      const batchLabel = `Batch ${msg.batch}/${msg.total_batches}`;
      if (msg.phase === "batch_complete" && msg.batch < msg.total_batches) {
        etaText.textContent = `${batchLabel} done`;
        return;
      }
    }

    // ETA calculation
    if (runStartTime && pct > 0 && pct < 100) {
      const elapsed = (Date.now() - runStartTime) / 1000;
      const totalEstimate = (elapsed / pct) * 100;
      const remaining = Math.max(0, totalEstimate - elapsed);
      etaText.textContent = `ETA: ~${formatDuration(remaining)}`;
    }
  }

  // ══════════════════════════════════════
  // Run Complete
  // ══════════════════════════════════════

  function onRunComplete(msg) {
    running = false;
    startBtn.disabled = false;
    stopBtn.disabled = true;
    exportBtn.disabled = false;
    progressFill.style.width = "100%";
    progressPct.textContent = "100%";
    etaText.textContent = "Complete";

    // Store results for JSON view
    runResults = msg.results;

    // Populate final results table
    populateFinalResults(msg.results);

    // Update JSON view
    resultsJsonPre.textContent = JSON.stringify(msg.results, null, 2);

    // Update footer stats
    updateStats(msg);

    appendLog("info", `Run complete \u2014 ${msg.results.length} window(s) finished in ${msg.total_elapsed_seconds.toFixed(1)}s`);
  }

  // ══════════════════════════════════════
  // Run Cancelled
  // ══════════════════════════════════════

  function onRunCancelled() {
    running = false;
    startBtn.disabled = false;
    stopBtn.disabled = true;
    progressFill.style.width = "0%";
    progressPct.textContent = "0%";
    etaText.textContent = "Cancelled";
    appendLog("warning", "Run cancelled \u2014 all windows closed");
  }

  // ══════════════════════════════════════
  // Log
  // ══════════════════════════════════════

  function appendLog(level, text, workerId) {
    const time = new Date().toLocaleTimeString("en-US", { hour12: false });
    const prefix = workerId !== undefined && workerId !== null ? `[W${workerId + 1}] ` : "";
    const line = document.createElement("div");
    line.className = `log-line log-${level}`;
    line.innerHTML = `<span class="log-timestamp">${time}</span>` +
      (prefix ? `<span class="log-worker-id">${prefix}</span>` : "") +
      escapeHtml(text);
    logBox.appendChild(line);

    // Limit log lines
    while (logBox.children.length > 500) {
      logBox.removeChild(logBox.firstChild);
    }

    if (autoScroll) {
      logBox.scrollTop = logBox.scrollHeight;
    }
  }

  // ══════════════════════════════════════
  // Controls
  // ══════════════════════════════════════

  startBtn.addEventListener("click", () => {
    const isFileMode = promptMode === "file";
    let singlePrompt = "";
    let prompts = null;

    if (isFileMode) {
      if (!uploadedPrompts || uploadedPrompts.length === 0) {
        showToast("Upload a file and select a column first", "warning");
        return;
      }
      prompts = uploadedPrompts;
      singlePrompt = prompts[0];
    } else {
      singlePrompt = promptInput.value.trim();
      if (!singlePrompt) {
        promptInput.focus();
        return;
      }
    }

    running = true;
    runStartTime = Date.now();
    workerStartTimes = {};
    workerData = {};
    startBtn.disabled = true;
    stopBtn.disabled = false;
    exportBtn.disabled = true;
    workersContainer.innerHTML = "";
    workersContainer.style.gridTemplateColumns = ""; // reset
    resultsBody.innerHTML = "";
    logBox.innerHTML = "";
    progressFill.style.width = "0%";
    progressPct.textContent = "0%";
    etaText.textContent = "ETA: \u2014";
    runResults = null;
    incrementalResults = {};
    resultsJsonPre.textContent = "";

    const windowCount = parseInt(windowCountInput.value, 10) || 4;

    // Use actual screen dimensions for tiling (from Settings, auto-detected on load)
    const monW = parseInt(monitorWidthInput.value, 10) || screen.availWidth || 1920;
    const monH = parseInt(monitorHeightInput.value, 10) || screen.availHeight || 1080;

    // Set grid layout based on window count
    layoutWindowsGrid(windowCount);

    // Pre-create worker cards and result rows
    for (let i = 0; i < windowCount; i++) {
      ensureWorkerCard(i);
      ensureResultRow(i);
    }

    send({
      type: "start_run",
      prompt: singlePrompt,
      prompts: isFileMode ? prompts : null,
      system_prompt: systemPromptInput.value.trim() || "",
      combine_with_first: combineWithFirstInput.checked,
      window_count: windowCount,
      submission_gap_seconds: parseFloat(submissionGapInput.value) || null,
      model_a: modelAInput.value.trim() || null,
      model_b: modelBInput.value.trim() || null,
      retain_output: retainOutputInput.value,
      clear_cookies: clearCookiesInput.checked,
      zoom_pct: parseInt(zoomInput.value, 10) || 100,
      monitor_count: parseInt(monitorCountInput.value, 10) || 1,
      monitor_width: monW,
      monitor_height: monH,
      taskbar_height: parseInt(taskbarHeightInput.value, 10) || 0,
      margin: parseInt(tileMarginInput.value, 10) || 0,
    });

    if (isFileMode) {
      const batches = Math.ceil(prompts.length / windowCount);
      if (systemPromptInput.value.trim()) {
        if (combineWithFirstInput.checked) {
          appendLog("info", "System prompt will be combined with each prompt as a single message.");
        } else {
          appendLog("info", "System prompt will be sent first before each batch prompt.");
        }
      }
      appendLog("info", `Starting run: ${prompts.length} prompt(s), ${windowCount} window(s), ${batches} batch(es)...`);
    } else {
      if (systemPromptInput.value.trim()) {
        if (combineWithFirstInput.checked) {
          appendLog("info", "System prompt will be combined with the prompt as a single message.");
        } else {
          appendLog("info", "System prompt will be sent first before the manual prompt.");
        }
      }
      appendLog("info", `Starting run with ${windowCount} window(s)...`);
    }
  });

  stopBtn.addEventListener("click", () => {
    send({ type: "stop_run" });
    appendLog("warning", "Stop requested...");
  });

  // Paste button
  pasteBtn.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      promptInput.value = text;
      promptInput.dispatchEvent(new Event("input"));
    } catch {
      appendLog("warning", "Clipboard access denied \u2014 paste manually");
    }
  });

  // Clear button
  clearBtn.addEventListener("click", () => {
    promptInput.value = "";
    promptInput.dispatchEvent(new Event("input"));
    promptInput.focus();
  });

  // Export dropdown
  const exportDropdown = document.getElementById("export-dropdown");

  exportBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    exportDropdown.classList.toggle("hidden");
  });

  // Close dropdown when clicking outside
  document.addEventListener("click", () => {
    exportDropdown.classList.add("hidden");
  });

  exportDropdown.addEventListener("click", () => {
    exportDropdown.classList.add("hidden");
  });

  // ══════════════════════════════════════
  // Prompt Mode Toggle (Manual / File Upload)
  // ══════════════════════════════════════

  function setPromptMode(mode) {
    promptMode = mode;
    modeManualBtn.classList.toggle("active", mode === "manual");
    modeFileBtn.classList.toggle("active", mode === "file");
    manualSection.classList.toggle("hidden", mode !== "manual");
    fileSection.classList.toggle("hidden", mode !== "file");
    // Show/hide paste & clear buttons (only for manual mode)
    pasteBtn.style.display = mode === "manual" ? "" : "none";
    clearBtn.style.display = mode === "manual" ? "" : "none";
    saveSettings();
  }

  modeManualBtn.addEventListener("click", () => setPromptMode("manual"));
  modeFileBtn.addEventListener("click", () => setPromptMode("file"));

  // ══════════════════════════════════════
  // File Upload
  // ══════════════════════════════════════

  uploadArea.addEventListener("click", () => fileInput.click());

  uploadArea.addEventListener("dragover", (e) => {
    e.preventDefault();
    uploadArea.classList.add("dragover");
  });

  uploadArea.addEventListener("dragleave", () => {
    uploadArea.classList.remove("dragover");
  });

  uploadArea.addEventListener("drop", (e) => {
    e.preventDefault();
    uploadArea.classList.remove("dragover");
    const file = e.dataTransfer.files[0];
    if (file) handleFileUpload(file);
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    if (file) handleFileUpload(file);
  });

  removeFileBtn.addEventListener("click", () => {
    clearFileUpload();
  });

  async function handleFileUpload(file) {
    const formData = new FormData();
    formData.append("file", file);

    try {
      const resp = await fetch("/upload-prompts", { method: "POST", body: formData });
      const data = await resp.json();

      if (data.error) {
        appendLog("error", `Upload failed: ${data.error}`);
        showToast(data.error, "error");
        return;
      }

      uploadedRows = data.rows;

      // Show file info
      fileNameSpan.textContent = data.filename;
      fileRowCountSpan.textContent = `${data.row_count} row(s)`;
      uploadArea.classList.add("hidden");
      fileInfoDiv.classList.remove("hidden");

      // Reset row range to full file
      rowStartInput.value = 1;
      rowEndInput.value = "";
      rowEndInput.placeholder = `${data.row_count} (all)`;
      rowRangeInfo.textContent = "";

      // Populate column checkboxes
      columnCheckboxes.innerHTML = "";
      data.columns.forEach((col, idx) => {
        const label = document.createElement("label");
        label.className = "column-chip";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = col;
        cb.addEventListener("change", () => {
          label.classList.toggle("selected", cb.checked);
          onColumnChange();
        });
        label.appendChild(cb);
        label.appendChild(document.createTextNode(col));
        columnCheckboxes.appendChild(label);
      });

      // Auto-select first column
      if (data.columns.length > 0) {
        const firstCb = columnCheckboxes.querySelector("input[type='checkbox']");
        firstCb.checked = true;
        firstCb.closest(".column-chip").classList.add("selected");
        onColumnChange();
      }

      appendLog("info", `File loaded: ${data.filename} (${data.row_count} rows, ${data.columns.length} columns)`);
    } catch (err) {
      appendLog("error", `Upload error: ${err.message}`);
      showToast("Failed to upload file", "error");
    }
  }

  function clearFileUpload() {
    uploadedRows = null;
    uploadedPrompts = [];
    fileInput.value = "";
    uploadArea.classList.remove("hidden");
    fileInfoDiv.classList.add("hidden");
    columnCheckboxes.innerHTML = "";
    rowStartInput.value = 1;
    rowEndInput.value = "";
    rowEndInput.placeholder = "End (all)";
    rowRangeInfo.textContent = "";
    filePreviewDiv.innerHTML = "";
    batchInfoDiv.textContent = "";
  }

  function getSelectedColumns() {
    return Array.from(columnCheckboxes.querySelectorAll("input[type='checkbox']:checked"))
      .map((cb) => cb.value);
  }

  function onColumnChange() {
    const cols = getSelectedColumns();
    if (!uploadedRows || cols.length === 0) {
      uploadedPrompts = [];
      filePreviewDiv.innerHTML = "";
      rowRangeInfo.textContent = "";
      updateBatchInfo();
      return;
    }

    // Combine selected columns into a single prompt per row.
    const allPrompts = uploadedRows
      .map((row) => cols.map((c) => (row[c] || "").trim()).filter((v) => v).join("\n"))
      .filter((v) => v.length > 0);

    // Apply row range (1-indexed for user)
    const start = Math.max(1, parseInt(rowStartInput.value, 10) || 1);
    const end = parseInt(rowEndInput.value, 10) || allPrompts.length;
    const clampedEnd = Math.min(end, allPrompts.length);
    uploadedPrompts = allPrompts.slice(start - 1, clampedEnd);

    // Update range info
    rowRangeInfo.textContent = `(using rows ${start}\u2013${clampedEnd} of ${allPrompts.length})`;

    // Render preview (first 5 of the sliced range)
    const preview = uploadedPrompts.slice(0, 5);
    const colLabel = cols.length === 1 ? cols[0] : cols.join(" + ");
    let html = `<table><thead><tr><th>#</th><th>${escapeHtml(colLabel)}</th></tr></thead><tbody>`;
    preview.forEach((p, i) => {
      html += `<tr><td>${start + i}</td><td>${escapeHtml(truncate(p, 120))}</td></tr>`;
    });
    if (uploadedPrompts.length > 5) {
      html += `<tr><td colspan="2" style="color:var(--text-dim)">... and ${uploadedPrompts.length - 5} more</td></tr>`;
    }
    html += "</tbody></table>";
    filePreviewDiv.innerHTML = html;

    updateBatchInfo();
  }

  // Re-extract prompts when row range changes
  rowStartInput.addEventListener("input", onColumnChange);
  rowEndInput.addEventListener("input", onColumnChange);

  function updateBatchInfo() {
    const wc = parseInt(windowCountInput.value, 10) || 4;
    const total = uploadedPrompts.length;
    if (total === 0) {
      batchInfoDiv.textContent = "";
      return;
    }
    const batches = Math.ceil(total / wc);
    batchInfoDiv.textContent = `${total} prompt(s) \u2192 ${batches} batch(es) of ${wc} window(s)`;
  }

  // Update batch info when window count changes
  windowCountInput.addEventListener("input", updateBatchInfo);

  // ══════════════════════════════════════
  // Settings Modal
  // ══════════════════════════════════════

  settingsBtn.addEventListener("click", () => {
    settingsModal.classList.remove("hidden");
  });

  closeSettingsBtn.addEventListener("click", () => {
    settingsModal.classList.add("hidden");
  });

  settingsModal.addEventListener("click", (e) => {
    if (e.target === settingsModal) {
      settingsModal.classList.add("hidden");
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!responseModal.classList.contains("hidden")) {
        responseModal.classList.add("hidden");
      } else if (!settingsModal.classList.contains("hidden")) {
        settingsModal.classList.add("hidden");
      }
    }
  });

  // ══════════════════════════════════════
  // Response Viewer Modal
  // ══════════════════════════════════════

  let currentResponseText = "";
  let currentResponseModelName = "";

  function showResponseModal(title, text) {
    currentResponseText = text;
    currentResponseModelName = title;
    responseModalTitle.textContent = title;
    responseFullText.textContent = text;
    responseModal.classList.remove("hidden");
  }

  btnCloseResponse.addEventListener("click", () => {
    responseModal.classList.add("hidden");
  });

  responseModal.addEventListener("click", (e) => {
    if (e.target === responseModal) {
      responseModal.classList.add("hidden");
    }
  });

  btnCopyResponse.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(currentResponseText);
      showToast("Copied to clipboard", "success");
    } catch {
      showToast("Clipboard access denied", "warning");
    }
  });

  btnDownloadResponse.addEventListener("click", () => {
    const safeName = currentResponseModelName.replace(/[^a-zA-Z0-9_\-. ]/g, "_").substring(0, 60);
    const blob = new Blob([currentResponseText], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${safeName}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  });

  // Event delegation for View/Copy buttons inside result table rows
  resultsBody.addEventListener("click", async (e) => {
    const viewBtn = e.target.closest(".btn-view-response");
    if (viewBtn) {
      const workerId = parseInt(viewBtn.dataset.workerId, 10);
      const side = viewBtn.dataset.side;
      const result = incrementalResults[workerId] || (runResults && runResults.find(r => r.worker_id === workerId));
      if (result) {
        const name = side === "a" ? result.model_a_name : result.model_b_name;
        const text = side === "a" ? result.model_a_response : result.model_b_response;
        showResponseModal(`${name || "Unknown"}`, text || "No response available");
      }
      return;
    }

    const copyBtn = e.target.closest(".btn-copy-inline");
    if (copyBtn) {
      const workerId = parseInt(copyBtn.dataset.workerId, 10);
      const side = copyBtn.dataset.side;
      const result = incrementalResults[workerId] || (runResults && runResults.find(r => r.worker_id === workerId));
      if (result) {
        const text = side === "a" ? result.model_a_response : result.model_b_response;
        try {
          await navigator.clipboard.writeText(text || "");
          showToast("Copied to clipboard", "success");
        } catch {
          showToast("Clipboard access denied", "warning");
        }
      }
      return;
    }
  });

  // ══════════════════════════════════════
  // Tabs (Table View / JSON)
  // ══════════════════════════════════════

  tabTable.addEventListener("click", () => {
    tabTable.classList.add("active");
    tabJson.classList.remove("active");
    resultsTableWrap.classList.remove("hidden");
    resultsJsonPre.classList.add("hidden");
    exportBtn.innerHTML = "&#128196; Export .xlsx";
  });

  tabJson.addEventListener("click", () => {
    tabJson.classList.add("active");
    tabTable.classList.remove("active");
    resultsJsonPre.classList.remove("hidden");
    resultsTableWrap.classList.add("hidden");
    exportBtn.innerHTML = "&#128196; Export .json";
  });

  // ══════════════════════════════════════
  // Auto-scroll Toggle
  // ══════════════════════════════════════

  autoScrollToggle.classList.add("active");
  autoScrollToggle.addEventListener("click", () => {
    autoScroll = !autoScroll;
    autoScrollToggle.classList.toggle("active", autoScroll);
  });

  // ══════════════════════════════════════
  // Helpers
  // ══════════════════════════════════════

  function showToast(message, level) {
    const icons = { success: "\u2713", info: "\u24D8", warning: "\u26A0", error: "\u2717" };
    const toast = document.createElement("div");
    toast.className = `toast toast-${level || "success"}`;
    toast.innerHTML =
      `<span class="toast-icon">${icons[level] || icons.success}</span>` +
      `<span>${escapeHtml(message)}</span>`;
    toastContainer.appendChild(toast);

    setTimeout(() => {
      toast.classList.add("toast-out");
      toast.addEventListener("animationend", () => toast.remove());
    }, 4000);
  }

  function layoutWindowsGrid(count) {
    // 1-2: single column, 3-4: 2 columns, 5-6: 2 columns, 7-9: 3 columns, 10+: 3-4 columns
    let cols;
    if (count <= 2) cols = 1;
    else if (count <= 6) cols = 2;
    else if (count <= 9) cols = 3;
    else cols = 4;

    workersContainer.style.display = "grid";
    workersContainer.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
    workersContainer.style.alignContent = "start";
  }

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  function escapeAttr(str) {
    return str.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function capitalize(str) {
    return str.charAt(0).toUpperCase() + str.slice(1);
  }

  function truncate(str, len) {
    if (str.length <= len) return str;
    return str.substring(0, len) + "\u2026";
  }

  function formatNumber(n) {
    return n.toLocaleString("en-US");
  }

  function formatDuration(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${s}s`;
  }

  function estimateTokens(text) {
    // Rough estimation: ~4 chars per token for English
    return Math.round(text.length / 4);
  }

  // ══════════════════════════════════════
  // Footer Stats (persisted in localStorage)
  // ══════════════════════════════════════

  const STATS_KEY = "lmarena_stats";

  function loadStats() {
    try {
      const raw = localStorage.getItem(STATS_KEY);
      if (!raw) return { totalRuns: 0, totalTime: 0, totalSuccess: 0, totalWindows: 0, totalTokens: 0 };
      return JSON.parse(raw);
    } catch {
      return { totalRuns: 0, totalTime: 0, totalSuccess: 0, totalWindows: 0, totalTokens: 0 };
    }
  }

  function saveStats(stats) {
    try { localStorage.setItem(STATS_KEY, JSON.stringify(stats)); } catch {}
  }

  function renderStats() {
    const s = loadStats();
    statRuns.textContent = s.totalRuns;
    if (s.totalRuns > 0) {
      const avgTime = Math.round(s.totalTime / s.totalRuns);
      statAvgTime.textContent = `${avgTime}s`;
      const rate = s.totalWindows > 0 ? ((s.totalSuccess / s.totalWindows) * 100).toFixed(1) : 0;
      statSuccessRate.textContent = `${rate}%`;
      statSuccessRate.className = parseFloat(rate) >= 90 ? "highlight" : "";
    } else {
      statAvgTime.textContent = "\u2014";
      statSuccessRate.textContent = "\u2014";
    }
    statTokens.textContent = formatNumber(s.totalTokens);
  }

  function updateStats(msg) {
    const s = loadStats();
    s.totalRuns += 1;
    s.totalTime += msg.total_elapsed_seconds || 0;

    let runTokens = 0;
    let runSuccess = 0;
    (msg.results || []).forEach((r) => {
      if (!r.error) runSuccess++;
      const text = (r.model_a_response || "") + (r.model_b_response || "");
      runTokens += estimateTokens(text);
    });
    s.totalSuccess += runSuccess;
    s.totalWindows += (msg.results || []).length;
    s.totalTokens += runTokens;

    saveStats(s);
    renderStats();
  }

  renderStats();

  // ══════════════════════════════════════
  // Settings Persistence (localStorage)
  // ══════════════════════════════════════

  const STORAGE_KEY = "lmarena_settings";

  const settingsFields = [
    { el: windowCountInput,   key: "window_count" },
    { el: submissionGapInput, key: "submission_gap" },
    { el: arenaUrlInput,      key: "arena_url" },
    { el: modelAInput,        key: "model_a" },
    { el: modelBInput,        key: "model_b" },
    { el: retainOutputInput,  key: "retain_output" },
    { el: zoomInput,          key: "zoom" },
    { el: clearCookiesInput,  key: "clear_cookies", checkbox: true },
    { el: monitorCountInput,  key: "monitor_count" },
    { el: monitorWidthInput,  key: "monitor_width" },
    { el: monitorHeightInput, key: "monitor_height" },
    { el: taskbarHeightInput, key: "taskbar_height" },
    { el: tileMarginInput,    key: "tile_margin" },
    { el: systemPromptInput,  key: "system_prompt" },
    { el: promptInput,        key: "prompt" },
    // prompt_mode handled separately (not an input element)
  ];

  function saveSettings() {
    const obj = {};
    settingsFields.forEach((f) => {
      obj[f.key] = f.checkbox ? f.el.checked : f.el.value;
    });
    obj.prompt_mode = promptMode;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(obj)); } catch {}
  }

  function loadSettings() {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      const obj = JSON.parse(raw);
      settingsFields.forEach((f) => {
        if (obj[f.key] !== undefined) {
          if (f.checkbox) f.el.checked = obj[f.key];
          else f.el.value = obj[f.key];
        }
      });
      if (obj.prompt_mode) {
        setPromptMode(obj.prompt_mode);
      }
    } catch {}
  }

  loadSettings();

  settingsFields.forEach((f) => {
    const evt = f.checkbox ? "change" : "input";
    f.el.addEventListener(evt, saveSettings);
  });

  // ══════════════════════════════════════
  // Tile Preview (inside Settings modal)
  // ══════════════════════════════════════

  function updateTilePreview() {
    const count = parseInt(windowCountInput.value, 10) || 4;
    const monitors = parseInt(monitorCountInput.value, 10) || 1;
    const mw = parseInt(monitorWidthInput.value, 10) || screen.availWidth || 1920;
    const mh = parseInt(monitorHeightInput.value, 10) || screen.availHeight || 1080;
    const tb = parseInt(taskbarHeightInput.value, 10) || 0;
    const mg = parseInt(tileMarginInput.value, 10) || 0;

    const totalW = monitors * mw;
    const totalH = mh - tb;
    const cols = Math.ceil(Math.sqrt(count));
    const rows = Math.ceil(count / cols);
    const winW = Math.floor((totalW - (cols + 1) * mg) / cols);
    const winH = Math.floor((totalH - (rows + 1) * mg) / rows);

    // Update the computed WINDOW SIZE field in config bar
    windowSizeInput.value = `${winW} \u00d7 ${winH}`;

    tilePreviewLabel.textContent =
      `${cols}\u00d7${rows} grid \u2014 each window ${winW}\u00d7${winH}px` +
      (monitors > 1 ? ` across ${totalW}\u00d7${totalH} total` : "");
  }

  [windowCountInput, monitorCountInput, monitorWidthInput,
   monitorHeightInput, taskbarHeightInput, tileMarginInput
  ].forEach((el) => el.addEventListener("input", updateTilePreview));

  // ══════════════════════════════════════
  // Init
  // ══════════════════════════════════════

  // Auto-detect screen dimensions if not already saved
  function initScreenDefaults() {
    const sw = screen.availWidth || 1920;
    const sh = screen.availHeight || 1080;
    // Only set if the fields still have generic defaults
    if (!localStorage.getItem(STORAGE_KEY) ||
        monitorWidthInput.value === "1920" && monitorHeightInput.value === "1080") {
      monitorWidthInput.value = sw;
      monitorHeightInput.value = sh;
      saveSettings();
    }
  }

  initScreenDefaults();
  updateTilePreview();

  // Auto-expand system prompt if it has saved content
  if (systemPromptInput.value.trim()) {
    document.getElementById("system-prompt-details").open = true;
  }

  connect();
})();
