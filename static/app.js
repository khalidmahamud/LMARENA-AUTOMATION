// Arena Runner — WebSocket client & DOM controller

(function () {
  "use strict";

  // ── State ──
  let ws = null;
  let connected = false;
  let running = false;
  let paused = false;
  let pauseTransitionPending = false;
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

  // Image upload state
  let uploadedImages = [];   // Array of { data: base64, mime_type, filename, objectUrl }

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
  const incognitoModeInput = document.getElementById("incognito-mode");
  const simultaneousStartInput = document.getElementById("simultaneous-start");
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
  const tabHtml           = document.getElementById("tab-html");
  const resultsHtmlWrap   = document.getElementById("results-html-wrap");
  const htmlResultsList   = document.getElementById("html-results-list");
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

  // Image upload DOM refs
  const imageUploadArea  = document.getElementById("image-upload-area");
  const imageFileInput   = document.getElementById("image-file-input");
  const imageThumbnails  = document.getElementById("image-thumbnails");

  // Resume banner DOM refs
  const resumeBanner    = document.getElementById("resume-banner");
  const resumeList      = document.getElementById("resume-list");
  const dismissResumeBtn = document.getElementById("dismiss-resume");

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
      fetchCheckpoints();
      syncRunState();
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
      case "run_paused":
        onRunPaused();
        break;
      case "run_resumed":
        onRunResumed();
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
          resetRunControlState();
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

  function updateStartButton() {
    if (!running) {
      startBtn.innerHTML = "&#8853; Start Run";
      startBtn.disabled = false;
      return;
    }

    startBtn.disabled = pauseTransitionPending;
    startBtn.innerHTML = paused ? "&#9654; Resume" : "&#9208; Pause";
  }

  function resetRunControlState() {
    running = false;
    paused = false;
    pauseTransitionPending = false;
    updateStartButton();
    stopBtn.disabled = true;
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
    } else if (msg.state === "prepared") {
      dot.classList.add("orange");
      fill.classList.add("orange");
      badge.classList.add("active");
      badge.innerHTML = "&#9201; Prepared";
      info.textContent = "Waiting to submit";
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
    } else if (msg.state === "prepared") {
      colStatus.innerHTML = '<span class="badge badge-active">&#9201; Prepared</span>';
      colA.className = "col-model-a text-queued";
      colA.textContent = "Prepared \u2014 waiting to submit";
      colB.className = "col-model-b text-queued";
      colB.textContent = "";
      colTime.textContent = "\u2014";
      colTokens.textContent = "\u2014";
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
      colA.className = "col-model-a text-queued";
      if (simultaneousStartInput.checked) {
        colA.textContent = "Queued \u2014 prepares with the batch";
      } else {
        const startsIn = gap * msg.worker_id;
        colA.textContent = startsIn > 0 ? `Queued \u2014 starts in ~${startsIn}s` : "Queued";
      }
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
    if (tabHtml.classList.contains("active")) renderHtmlPreviews();
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

    if (paused) {
      etaText.textContent = "Paused";
      return;
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
    resetRunControlState();
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

    // Refresh HTML previews if HTML tab is active
    if (tabHtml.classList.contains("active")) renderHtmlPreviews();

    appendLog("info", `Run complete \u2014 ${msg.results.length} window(s) finished in ${msg.total_elapsed_seconds.toFixed(1)}s`);
  }

  // ══════════════════════════════════════
  // Run Cancelled
  // ══════════════════════════════════════

  function onRunCancelled() {
    resetRunControlState();
    progressFill.style.width = "0%";
    progressPct.textContent = "0%";
    etaText.textContent = "Cancelled";
    appendLog("warning", "Run cancelled \u2014 all windows closed");
  }

  function onRunPaused() {
    if (!running) return;
    paused = true;
    pauseTransitionPending = false;
    updateStartButton();
    etaText.textContent = "Paused";
  }

  function onRunResumed() {
    if (!running) return;
    paused = false;
    pauseTransitionPending = false;
    updateStartButton();
    etaText.textContent = "Resuming...";
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
    if (running) {
      if (pauseTransitionPending) return;
      pauseTransitionPending = true;
      updateStartButton();
      etaText.textContent = paused ? "Resuming..." : "Pausing...";
      send({ type: paused ? "resume_run" : "pause_run" });
      return;
    }

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
    paused = false;
    pauseTransitionPending = false;
    runStartTime = Date.now();
    workerStartTimes = {};
    workerData = {};
    updateStartButton();
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
      incognito: incognitoModeInput.checked,
      images: (!isFileMode && uploadedImages.length > 0)
        ? uploadedImages.map((img) => ({
            data: img.data,
            mime_type: img.mime_type,
            filename: img.filename,
          }))
        : null,
      simultaneous_start: simultaneousStartInput.checked,
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
      if (simultaneousStartInput.checked) {
        appendLog("info", "All windows will prepare in parallel, then submit one by one using the configured gap.");
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
      if (simultaneousStartInput.checked) {
        appendLog("info", "All windows will prepare in parallel, then submit one by one using the configured gap.");
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
    clearImages();
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
      saveFileState();
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
    clearSavedFileState();
  }

  // ══════════════════════════════════════
  // Image Upload (Manual Mode)
  // ══════════════════════════════════════

  const MAX_IMAGE_SIZE = 5 * 1024 * 1024; // 5 MB
  const MAX_IMAGES = 10;
  const MAX_IMAGE_DIM = 2048;

  imageUploadArea.addEventListener("click", () => imageFileInput.click());

  imageUploadArea.addEventListener("dragover", (e) => {
    e.preventDefault();
    imageUploadArea.classList.add("dragover");
  });

  imageUploadArea.addEventListener("dragleave", () => {
    imageUploadArea.classList.remove("dragover");
  });

  imageUploadArea.addEventListener("drop", (e) => {
    e.preventDefault();
    imageUploadArea.classList.remove("dragover");
    handleImageFiles(e.dataTransfer.files);
  });

  imageFileInput.addEventListener("change", () => {
    handleImageFiles(imageFileInput.files);
    imageFileInput.value = "";
  });

  function handleImageFiles(fileList) {
    const files = Array.from(fileList);
    const remaining = MAX_IMAGES - uploadedImages.length;
    if (remaining <= 0) {
      showToast(`Maximum ${MAX_IMAGES} images allowed`, "warning");
      return;
    }
    const toProcess = files.slice(0, remaining);

    toProcess.forEach((file) => {
      if (!file.type.match(/^image\/(png|jpeg|webp|gif)$/)) {
        showToast(`Unsupported format: ${file.name}`, "warning");
        return;
      }
      if (file.size > MAX_IMAGE_SIZE) {
        showToast(`${file.name} exceeds 5 MB limit`, "warning");
        return;
      }
      processImage(file);
    });
  }

  function processImage(file) {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        let { width, height } = img;

        // Resize if too large
        if (width > MAX_IMAGE_DIM || height > MAX_IMAGE_DIM) {
          const scale = MAX_IMAGE_DIM / Math.max(width, height);
          width = Math.round(width * scale);
          height = Math.round(height * scale);
        }

        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, width, height);

        // Determine output format
        const outputType = file.type === "image/png" ? "image/png" : "image/jpeg";
        const quality = outputType === "image/jpeg" ? 0.85 : undefined;
        const dataUrl = canvas.toDataURL(outputType, quality);
        const base64 = dataUrl.split(",")[1];

        const objectUrl = URL.createObjectURL(file);
        uploadedImages.push({
          data: base64,
          mime_type: outputType,
          filename: file.name,
          objectUrl: objectUrl,
        });
        renderImageThumbnails();
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  }

  function renderImageThumbnails() {
    imageThumbnails.innerHTML = "";
    uploadedImages.forEach((img, idx) => {
      const thumb = document.createElement("div");
      thumb.className = "image-thumb";
      thumb.innerHTML = `
        <img src="${img.objectUrl}" alt="${escapeAttr(img.filename)}" />
        <button class="image-thumb-remove" data-idx="${idx}">&times;</button>
        <span class="image-thumb-name">${escapeHtml(truncate(img.filename, 12))}</span>
      `;
      imageThumbnails.appendChild(thumb);
    });

    // Bind remove buttons
    imageThumbnails.querySelectorAll(".image-thumb-remove").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const idx = parseInt(btn.dataset.idx, 10);
        removeImage(idx);
      });
    });
  }

  function removeImage(idx) {
    if (uploadedImages[idx] && uploadedImages[idx].objectUrl) {
      URL.revokeObjectURL(uploadedImages[idx].objectUrl);
    }
    uploadedImages.splice(idx, 1);
    renderImageThumbnails();
  }

  function clearImages() {
    uploadedImages.forEach((img) => {
      if (img.objectUrl) URL.revokeObjectURL(img.objectUrl);
    });
    uploadedImages = [];
    renderImageThumbnails();
  }

  // ── File state persistence ──

  const FILE_STATE_KEY = "lmarena_file_state";

  function saveFileState() {
    if (!uploadedRows || uploadedRows.length === 0) return;
    try {
      const cols = getSelectedColumns();
      const state = {
        rows: uploadedRows,
        filename: fileNameSpan.textContent,
        rowCount: uploadedRows.length,
        columns: Array.from(columnCheckboxes.querySelectorAll("input[type='checkbox']"))
          .map(cb => cb.value),
        selectedColumns: cols,
        rowStart: rowStartInput.value,
        rowEnd: rowEndInput.value,
      };
      localStorage.setItem(FILE_STATE_KEY, JSON.stringify(state));
    } catch {}
  }

  function clearSavedFileState() {
    try { localStorage.removeItem(FILE_STATE_KEY); } catch {}
  }

  function restoreFileState() {
    try {
      const raw = localStorage.getItem(FILE_STATE_KEY);
      if (!raw) return;
      const state = JSON.parse(raw);
      if (!state.rows || state.rows.length === 0) return;

      uploadedRows = state.rows;

      // Restore file info display
      fileNameSpan.textContent = state.filename || "Restored file";
      fileRowCountSpan.textContent = `${state.rowCount || state.rows.length} row(s)`;
      uploadArea.classList.add("hidden");
      fileInfoDiv.classList.remove("hidden");

      // Restore row range
      rowStartInput.value = state.rowStart || 1;
      rowEndInput.value = state.rowEnd || "";
      rowEndInput.placeholder = `${state.rowCount || state.rows.length} (all)`;

      // Restore column checkboxes
      columnCheckboxes.innerHTML = "";
      (state.columns || []).forEach(col => {
        const label = document.createElement("label");
        label.className = "column-chip";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = col;
        const isSelected = (state.selectedColumns || []).includes(col);
        cb.checked = isSelected;
        if (isSelected) label.classList.add("selected");
        cb.addEventListener("change", () => {
          label.classList.toggle("selected", cb.checked);
          onColumnChange();
          saveFileState();
        });
        label.appendChild(cb);
        label.appendChild(document.createTextNode(col));
        columnCheckboxes.appendChild(label);
      });

      // Rebuild prompts
      onColumnChange();
    } catch {}
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
    saveFileState();
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
  // Tabs (Table View / JSON / HTML)
  // ══════════════════════════════════════

  function setResultsTab(active) {
    tabTable.classList.toggle("active", active === "table");
    tabJson.classList.toggle("active", active === "json");
    tabHtml.classList.toggle("active", active === "html");
    resultsTableWrap.classList.toggle("hidden", active !== "table");
    resultsJsonPre.classList.toggle("hidden", active !== "json");
    resultsHtmlWrap.classList.toggle("hidden", active !== "html");
  }

  tabTable.addEventListener("click", () => {
    setResultsTab("table");
    exportBtn.innerHTML = "&#128196; Export .xlsx";
  });

  tabJson.addEventListener("click", () => {
    setResultsTab("json");
    exportBtn.innerHTML = "&#128196; Export .json";
  });

  tabHtml.addEventListener("click", () => {
    setResultsTab("html");
    renderHtmlPreviews();
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
  // HTML Preview (extract code blocks & render)
  // ══════════════════════════════════════

  function extractHtmlBlocks(responseHtml) {
    if (!responseHtml) return [];
    const container = document.createElement("div");
    container.innerHTML = responseHtml;
    const blocks = [];
    // Look for <pre><code> blocks that contain HTML
    container.querySelectorAll("pre code").forEach((codeEl) => {
      // Strip leading language label (e.g. "HTML", "html", "css") injected by code block renderers
      const text = (codeEl.textContent || "")
        .replace(/^(html|css|javascript|js|typescript|ts|xml|json|jsx|tsx)\s*(?=<)/i, "")
        .trim();
      // Check if it looks like HTML
      if (/<(!DOCTYPE|html|head|body|div|span|p |h[1-6]|section|article|nav|main|form|table|ul|ol|li|a |img |style|script|link|meta|button|input|header|footer)/i.test(text)) {
        blocks.push(text);
      }
    });
    // Also check standalone <code> blocks (not inside <pre>) in case of inline code fences
    if (blocks.length === 0) {
      container.querySelectorAll("code").forEach((codeEl) => {
        const text = (codeEl.textContent || "")
          .replace(/^(html|css|javascript|js|typescript|ts|xml|json)\s*(?=<)/i, "")
          .trim();
        if (text.length > 50 && /<(!DOCTYPE|html|head|body|div)/i.test(text)) {
          blocks.push(text);
        }
      });
    }
    return blocks;
  }

  function renderHtmlPreviews() {
    htmlResultsList.innerHTML = "";
    const results = runResults || Object.values(incrementalResults);
    if (!results || results.length === 0) {
      htmlResultsList.innerHTML = '<div class="html-empty">No results yet</div>';
      return;
    }

    let hasAnyHtml = false;
    results.forEach((r) => {
      const blocksA = extractHtmlBlocks(r.model_a_response_html);
      const blocksB = extractHtmlBlocks(r.model_b_response_html);
      if (blocksA.length === 0 && blocksB.length === 0) return;
      hasAnyHtml = true;

      const card = document.createElement("div");
      card.className = "html-result-card";

      const header = document.createElement("div");
      header.className = "html-result-header";
      header.textContent = `Window #${r.worker_id + 1}`;
      card.appendChild(header);

      const panels = document.createElement("div");
      panels.className = "html-result-panels";

      // Model A
      const panelA = buildHtmlPanel(r.model_a_name || "Model A", blocksA);
      panels.appendChild(panelA);

      // Model B
      const panelB = buildHtmlPanel(r.model_b_name || "Model B", blocksB);
      panels.appendChild(panelB);

      card.appendChild(panels);
      htmlResultsList.appendChild(card);
    });

    if (!hasAnyHtml) {
      htmlResultsList.innerHTML = '<div class="html-empty">No HTML code blocks found in responses</div>';
    }
  }

  function buildHtmlPanel(modelName, blocks) {
    const panel = document.createElement("div");
    panel.className = "html-result-panel";

    const panelHeader = document.createElement("div");
    panelHeader.className = "html-panel-header";
    panelHeader.textContent = modelName;
    panel.appendChild(panelHeader);

    if (blocks.length === 0) {
      const empty = document.createElement("div");
      empty.className = "html-panel-empty";
      empty.textContent = "No HTML code blocks";
      panel.appendChild(empty);
    } else {
      blocks.forEach((html, idx) => {
        const wrap = document.createElement("div");
        wrap.className = "html-iframe-wrap";
        if (blocks.length > 1) {
          const label = document.createElement("div");
          label.className = "html-block-label";
          label.textContent = `Block ${idx + 1}`;
          wrap.appendChild(label);
        }
        const iframe = document.createElement("iframe");
        iframe.className = "html-preview-iframe";
        iframe.sandbox = "allow-scripts";
        iframe.srcdoc = html;
        wrap.appendChild(iframe);
        panel.appendChild(wrap);
      });
    }
    return panel;
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

  document.getElementById("reset-stats-btn").addEventListener("click", () => {
    localStorage.removeItem(STATS_KEY);
    renderStats();
  });

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
    { el: incognitoModeInput, key: "incognito_mode", checkbox: true },
    { el: simultaneousStartInput, key: "simultaneous_start", checkbox: true },
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
  // Checkpoint / Resume
  // ══════════════════════════════════════

  function fetchCheckpoints() {
    fetch("/api/checkpoints")
      .then(res => res.json())
      .then(data => {
        if (!Array.isArray(data) || data.length === 0) {
          resumeBanner.classList.add("hidden");
          return;
        }
        renderCheckpoints(data);
        resumeBanner.classList.remove("hidden");
      })
      .catch(() => {
        resumeBanner.classList.add("hidden");
      });
  }

  function renderCheckpoints(checkpoints) {
    resumeList.innerHTML = "";
    checkpoints.forEach(cp => {
      const div = document.createElement("div");
      div.className = "resume-item";
      div.dataset.runId = cp.run_id;

      const lastTime = cp.last_checkpoint_at
        ? new Date(cp.last_checkpoint_at).toLocaleString()
        : "unknown";

      div.innerHTML = `
        <div class="resume-info">
          <div class="resume-info-title">Run ${cp.run_id}</div>
          <div class="resume-info-detail">
            ${cp.completed_prompts}/${cp.total_prompts} prompts done
            &middot; batch ${cp.next_batch}/${cp.total_batches}
            &middot; ${lastTime}
          </div>
        </div>
        <div class="resume-actions">
          <button class="btn-resume" data-run-id="${cp.run_id}">&#9654; Resume</button>
          <button class="btn-discard" data-run-id="${cp.run_id}">&#10005; Discard</button>
        </div>
      `;
      resumeList.appendChild(div);
    });
  }

  resumeList.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-run-id]");
    if (!btn) return;

    const runId = btn.dataset.runId;

    if (btn.classList.contains("btn-resume")) {
      if (running) {
        showToast("A run is already in progress", "warning");
        return;
      }
      // Transition UI to running state
      running = true;
      paused = false;
      pauseTransitionPending = false;
      runStartTime = Date.now();
      workerStartTimes = {};
      workerData = {};
      updateStartButton();
      stopBtn.disabled = false;
      exportBtn.disabled = true;
      workersContainer.innerHTML = "";
      workersContainer.style.gridTemplateColumns = "";
      resultsBody.innerHTML = "";
      logBox.innerHTML = "";
      progressFill.style.width = "0%";
      progressPct.textContent = "0%";
      etaText.textContent = "Resuming...";
      runResults = null;
      incrementalResults = {};
      resultsJsonPre.textContent = "";

      resumeBanner.classList.add("hidden");

      send({ type: "resume_from_checkpoint", run_id: runId });
      appendLog("info", `Resuming run ${runId} from checkpoint`);
    }

    if (btn.classList.contains("btn-discard")) {
      fetch(`/api/checkpoints/${runId}`, { method: "DELETE" })
        .then(() => {
          const item = resumeList.querySelector(`[data-run-id="${runId}"]`);
          if (item && item.classList.contains("resume-item")) {
            item.remove();
          }
          if (resumeList.children.length === 0) {
            resumeBanner.classList.add("hidden");
          }
          showToast(`Checkpoint ${runId} discarded`, "info");
        })
        .catch(() => {
          showToast("Failed to discard checkpoint", "error");
        });
    }
  });

  dismissResumeBtn.addEventListener("click", () => {
    resumeBanner.classList.add("hidden");
  });

  // ══════════════════════════════════════
  // Run State Sync (reconnect recovery)
  // ══════════════════════════════════════

  function syncRunState() {
    fetch("/api/run-state")
      .then(res => res.json())
      .then(state => {
        if (!state || !state.running) return;

        // Restore running state
        running = true;
        paused = state.paused || false;
        runStartTime = runStartTime || Date.now();
        updateStartButton();
        stopBtn.disabled = false;

        // Hide resume banner while a run is active
        resumeBanner.classList.add("hidden");

        // Restore worker cards
        const workerCount = state.workers ? state.workers.length : 0;
        if (workerCount > 0) {
          workersContainer.innerHTML = "";
          layoutWindowsGrid(workerCount);
          state.workers.forEach(w => {
            ensureWorkerCard(w.worker_id);
            updateWorkerCard({
              worker_id: w.worker_id,
              state: w.state,
              progress_pct: w.progress_pct,
              message: "",
              error: null,
            });
          });
        }

        // Restore result rows
        if (state.results && state.results.length > 0) {
          state.results.forEach(r => {
            ensureResultRow(r.worker_id);
            updateResultRowWithData(r);
            incrementalResults[r.worker_id] = r;
          });
          exportBtn.disabled = false;
        }

        // Restore progress bar
        const total = state.total_prompts || 1;
        const done = state.completed_prompts || 0;
        const pct = Math.round((done / total) * 100);
        progressFill.style.width = `${pct}%`;
        progressPct.textContent = `${pct}%`;

        if (paused) {
          etaText.textContent = "Paused";
        } else {
          etaText.textContent = `${done}/${total} prompts done`;
        }

        appendLog("info", "Reconnected to active run");
      })
      .catch(() => {});
  }

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
  updateStartButton();

  // Auto-expand system prompt if it has saved content
  if (systemPromptInput.value.trim()) {
    document.getElementById("system-prompt-details").open = true;
  }

  // Restore file upload state if present
  restoreFileState();

  connect();
})();
