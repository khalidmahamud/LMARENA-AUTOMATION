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
  let activeRunId = null;
  let runResults = null; // last run results for JSON view
  let workerStartTimes = {};
  let workerData = {};   // live data per worker
  let incrementalResults = {}; // worker_id -> result payload (available as each worker completes)

  // Multi-prompt card state
  let promptCards = {};     // cardId -> card state object
  let nextCardIndex = 1;    // counter for card numbering

  function generateCardId() {
    return "card_" + Date.now() + "_" + (nextCardIndex++);
  }

  function generateRunId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID().replace(/-/g, "").slice(0, 8);
    }
    return (Date.now().toString(36) + Math.random().toString(36).slice(2, 6)).slice(0, 8);
  }

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
  const promptsPerSessionInput = document.getElementById("prompts-per-session");
  const arenaUrlInput     = document.getElementById("arena-url");
  const modelAInput       = document.getElementById("model-a");
  const modelBInput       = document.getElementById("model-b");
  const retainOutputInput = document.getElementById("retain-output");
  const zoomInput         = document.getElementById("zoom");
  const clearCookiesInput = document.getElementById("clear-cookies");
  const incognitoModeInput = document.getElementById("incognito-mode");
  const minimizedModeInput = document.getElementById("minimized-mode");
  const simultaneousStartInput = document.getElementById("simultaneous-start");
  const proxyListInput    = document.getElementById("proxy-list");
  const proxyProtocolInput = document.getElementById("proxy-protocol");
  const proxyLimitInput   = document.getElementById("proxy-limit");
  const fetchProxiesBtn   = document.getElementById("fetch-proxies-btn");
  const proxyTestInput    = document.getElementById("proxy-test");
  const proxyFetchStatus  = document.getElementById("proxy-fetch-status");
  const proxyOnChallengeInput = document.getElementById("proxy-on-challenge");
  const windowsPerProxyInput  = document.getElementById("windows-per-proxy");
  const checkPoolBtn        = document.getElementById("check-pool-btn");
  const autoRefreshToggle   = document.getElementById("auto-refresh-toggle");
  const proxyPoolCounts     = document.getElementById("proxy-pool-counts");
  const proxyPoolStatusEl   = document.getElementById("proxy-pool-status");
  const poolTrackerCounts   = document.getElementById("pool-tracker-counts");
  const poolBarFill         = document.getElementById("pool-bar-fill");
  const poolRefreshDot      = document.getElementById("pool-refresh-dot");
  const poolMaxSizeInput    = document.getElementById("pool-max-size");
  const poolMaxLatencyInput = document.getElementById("pool-max-latency");
  const poolRefreshIntervalInput = document.getElementById("pool-refresh-interval");
  // Preview DOM refs
  const previewGridWrap   = document.getElementById("preview-grid-wrap");
  const previewGrid       = document.getElementById("preview-grid");
  const previewStatus     = document.getElementById("preview-status");
  const headlessModeInput = document.getElementById("headless-mode");

  // Preview state
  let previewSubscribed = false;

  // Log tabs
  const logTabProcessing  = document.getElementById("log-tab-processing");
  const logTabProxy       = document.getElementById("log-tab-proxy");
  const proxyLogBox       = document.getElementById("proxy-log-box");
  const proxyLogEntries   = document.getElementById("proxy-log-entries");
  const plogPoolCount     = document.getElementById("plog-pool-count");
  const plogAvgLatency    = document.getElementById("plog-avg-latency");
  const plogThreshold     = document.getElementById("plog-threshold");
  const plogAutoRefresh   = document.getElementById("plog-auto-refresh");
  const plogLatencyFill   = document.getElementById("plog-latency-fill");
  const plogLatencyMarker = document.getElementById("plog-latency-marker");
  const plogLatencyMax    = document.getElementById("plog-latency-max");
  const proxyListEl       = document.getElementById("proxy-list");
  const systemPromptInput = document.getElementById("system-prompt");
  const combineWithFirstInput = document.getElementById("combine-with-first");
  const promptInput       = document.getElementById("prompt");
  const startMonitorInput = document.getElementById("start-monitor");
  const monitorCountInput = document.getElementById("monitor-count");
  const monitorWidthInput = document.getElementById("monitor-width");
  const monitorHeightInput = document.getElementById("monitor-height");
  const taskbarHeightInput = document.getElementById("taskbar-height");
  const tileMarginInput   = document.getElementById("tile-margin");
  const tilePreviewLabel  = document.getElementById("tile-preview-label");
  const startBtn          = document.getElementById("btn-start");
  const stopBtn           = document.getElementById("btn-stop");
  const closeAllBtn       = document.getElementById("btn-close-all");
  const clearAllBtn       = document.getElementById("btn-clear-all");
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
  const batchAggSizeInput = document.getElementById("batch-aggregate-size");
  const batchAggInfo      = document.getElementById("batch-aggregate-info");

  // Image upload DOM refs
  const imageUploadArea  = document.getElementById("image-upload-area");
  const imageFileInput   = document.getElementById("image-file-input");
  const imageThumbnails  = document.getElementById("image-thumbnails");

  // Instruction Load mode DOM refs
  const modeInstructionBtn     = document.getElementById("mode-instruction");
  const instructionSection     = document.getElementById("instruction-load-section");
  const instructionUploadArea  = document.getElementById("instruction-upload-area");
  const instructionFileInput   = document.getElementById("instruction-file-input");
  const instructionInfoDiv     = document.getElementById("instruction-info");
  const instructionFileName    = document.getElementById("instruction-file-name");
  const instructionCountSpan   = document.getElementById("instruction-count");
  const removeInstructionsBtn  = document.getElementById("btn-remove-instructions");
  const instructionCardsContainer = document.getElementById("instruction-cards-container");
  const instructionRunBtn      = document.getElementById("btn-instruction-run");
  const instructionStopBtn     = document.getElementById("btn-instruction-stop");
  const instructionEta         = document.getElementById("instruction-eta");
  const instructionProgressFill = document.getElementById("instruction-progress-fill");
  const instructionProgressPct = document.getElementById("instruction-progress-pct");

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
      resubscribePreview();
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

  function parseProxyList(text) {
    if (!text || !text.trim()) return null;
    return text.trim().split("\n").filter(function (l) { return l.trim(); }).map(function (line) {
      var parts = line.trim().split(",");
      var entry = { server: parts[0].trim() };
      if (parts[1]) entry.username = parts[1].trim();
      if (parts[2]) entry.password = parts[2].trim();
      return entry;
    });
  }

  if (fetchProxiesBtn) {
    fetchProxiesBtn.addEventListener("click", function () {
      var protocol = proxyProtocolInput.value;
      var limit = parseInt(proxyLimitInput.value, 10) || 10;
      var test = proxyTestInput.checked;
      proxyFetchStatus.style.color = "var(--text-dim)";
      proxyFetchStatus.textContent = test ? "Loading & testing XLSX proxies..." : "Loading XLSX proxies...";
      fetchProxiesBtn.disabled = true;

      var qs = "protocol=" + encodeURIComponent(protocol) + "&limit=" + limit;
      if (test) qs += "&test=true";

      fetch("/api/free-proxies?" + qs)
        .then(function (res) { return res.json(); })
        .then(function (data) {
          if (data.error) {
            proxyFetchStatus.style.color = "var(--red)";
            proxyFetchStatus.textContent = data.error;
            return;
          }
          if (!data.proxies || !data.proxies.length) {
            proxyFetchStatus.style.color = "var(--orange)";
            proxyFetchStatus.textContent = data.tested
              ? "0 alive out of " + (data.total_tested || 0) + " tested"
              : "No proxies found in XLSX";
            return;
          }
          var lines = data.proxies.map(function (p) { return p.server; });
          proxyListInput.value = lines.join("\n");
          proxyFetchStatus.style.color = "var(--green)";
          proxyFetchStatus.textContent = data.tested
            ? data.count + " alive out of " + data.total_tested + " tested"
            : data.count + " proxies loaded from XLSX";
        })
        .catch(function (err) {
          proxyFetchStatus.style.color = "var(--red)";
          proxyFetchStatus.textContent = "Failed: " + err.message;
        })
        .finally(function () {
          fetchProxiesBtn.disabled = false;
          refreshPoolStatus();
        });
    });
  }

  // ── Log Tab Switching ──

  var activeLogTab = "processing";

  if (logTabProcessing) {
    logTabProcessing.addEventListener("click", function () {
      activeLogTab = "processing";
      logTabProcessing.classList.add("active");
      logTabProxy.classList.remove("active");
      logBox.classList.remove("hidden");
      proxyLogBox.classList.add("hidden");
    });
  }

  if (logTabProxy) {
    logTabProxy.addEventListener("click", function () {
      activeLogTab = "proxy";
      logTabProxy.classList.add("active");
      logTabProcessing.classList.remove("active");
      proxyLogBox.classList.remove("hidden");
      logBox.classList.add("hidden");
    });
  }

  function appendProxyLog(level, text) {
    if (!proxyLogEntries) return;
    var time = new Date().toLocaleTimeString("en-US", { hour12: false });
    var line = document.createElement("div");
    line.className = "log-line " + level;
    line.textContent = time + "  " + text;
    line.style.animation = "fade-in-up 0.15s ease-out";
    proxyLogEntries.appendChild(line);
    while (proxyLogEntries.children.length > 200) {
      proxyLogEntries.removeChild(proxyLogEntries.firstChild);
    }
    if (autoScroll && activeLogTab === "proxy") {
      proxyLogBox.scrollTo({ top: proxyLogBox.scrollHeight, behavior: "smooth" });
    }
  }

  var _lastPoolHealthy = null;
  var _lastPoolTotal = null;

  // ── Proxy Pool Controls ──

  function refreshPoolStatus() {
    fetch("/api/proxy-pool/status")
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function (data) {
        var h = typeof data.healthy === "number" ? data.healthy : 0;
        var d = typeof data.degraded === "number" ? data.degraded : 0;
        var t = typeof data.total === "number" ? data.total : (data.proxies ? data.proxies.length : 0);
        var m = typeof data.max_healthy === "number" ? data.max_healthy : 50;
        var maxLat = typeof data.max_latency_ms === "number" ? data.max_latency_ms : 5000;
        var avgLat = data.avg_latency_ms;
        var isEnabled = data.auto_refresh_enabled || false;
        var isActive = data.auto_refresh_active || false;

        // Update settings modal
        proxyPoolCounts.textContent = h + " healthy / " + t + " total";
        proxyPoolCounts.style.color = h > 0 ? "var(--green)" : d > 0 ? "var(--orange)" : "var(--text-dim)";
        if (autoRefreshToggle) {
          autoRefreshToggle.checked = isEnabled;
        }
        if (poolMaxSizeInput && poolMaxSizeInput !== document.activeElement) {
          poolMaxSizeInput.value = m;
        }
        if (poolMaxLatencyInput && poolMaxLatencyInput !== document.activeElement) {
          poolMaxLatencyInput.value = maxLat;
        }
        if (poolRefreshIntervalInput && poolRefreshIntervalInput !== document.activeElement) {
          var intervalSec = typeof data.auto_refresh_interval === "number" ? data.auto_refresh_interval : 300;
          poolRefreshIntervalInput.value = Math.round(intervalSec / 60);
        }

        // Update sidebar tracker — show healthy / max
        if (poolTrackerCounts) {
          poolTrackerCounts.textContent = h + " / " + m;
        }
        if (poolBarFill) {
          var pct = m > 0 ? Math.round((h / m) * 100) : 0;
          poolBarFill.style.width = Math.min(pct, 100) + "%";
          poolBarFill.className = "pool-tracker-bar-fill" +
            (t === 0 ? " empty" : pct < 50 ? " warning" : "");
        }
        if (poolRefreshDot) {
          poolRefreshDot.className = "pool-tracker-refresh-dot" + (isActive ? " active" : "");
          poolRefreshDot.title = isActive ? "Auto-refresh active" : "Auto-refresh inactive";
        }

        // Update proxy log tab stats
        if (plogPoolCount) plogPoolCount.textContent = h + " healthy / " + t + " total";
        if (plogAvgLatency) {
          plogAvgLatency.textContent = avgLat !== null ? Math.round(avgLat) + "ms" : "--";
          plogAvgLatency.style.color = avgLat !== null && avgLat < maxLat * 0.5 ? "var(--green)" : avgLat !== null && avgLat < maxLat ? "var(--orange)" : "var(--text-dim)";
        }
        if (plogThreshold) plogThreshold.textContent = "< " + maxLat + "ms";
        if (plogAutoRefresh) {
          plogAutoRefresh.textContent = isActive ? "active" : "off";
          plogAutoRefresh.style.color = isActive ? "var(--green)" : "var(--text-dim)";
        }
        // Latency meter
        if (plogLatencyFill && avgLat !== null) {
          var latPct = Math.min(100, Math.round((avgLat / maxLat) * 100));
          plogLatencyFill.style.width = latPct + "%";
        } else if (plogLatencyFill) {
          plogLatencyFill.style.width = "0%";
        }
        if (plogLatencyMarker && avgLat !== null) {
          var markerPos = Math.min(98, Math.round((avgLat / maxLat) * 100));
          plogLatencyMarker.style.left = markerPos + "%";
          plogLatencyMarker.classList.remove("hidden");
        } else if (plogLatencyMarker) {
          plogLatencyMarker.classList.add("hidden");
        }
        if (plogLatencyMax) plogLatencyMax.textContent = maxLat + "ms";

        // Render per-proxy list
        if (proxyListEl && data.proxies) {
          var proxies = data.proxies.slice().sort(function (a, b) {
            var rankA = a.healthy ? 0 : a.degraded ? 1 : 2;
            var rankB = b.healthy ? 0 : b.degraded ? 1 : 2;
            if (rankA !== rankB) return rankA - rankB;
            var la = a.latency_ms != null ? a.latency_ms : 99999;
            var lb = b.latency_ms != null ? b.latency_ms : 99999;
            return la - lb;
          });
          var html = "";
          for (var i = 0; i < proxies.length; i++) {
            var p = proxies[i];
            var addr = p.server.replace(/^https?:\/\//, "").replace(/^socks[45]:\/\//, "");
            var lat = p.healthy && p.latency_ms != null ? Math.round(p.latency_ms) : null;
            var barPct = lat != null && maxLat > 0 ? Math.min(100, Math.round((lat / maxLat) * 100)) : 0;
            var barColor = p.degraded ? "var(--orange)" :
              !p.healthy ? "var(--red)" :
              lat != null && lat < maxLat * 0.3 ? "var(--green)" :
              lat != null && lat < maxLat * 0.6 ? "#a3d977" :
              lat != null && lat < maxLat * 0.8 ? "var(--orange)" : "var(--red)";
            var statusDot = p.healthy ? "green" : p.degraded ? "amber" : "red";
            var latText = lat != null ? lat + "ms" : p.degraded ? "retry" : "--";
            html += '<div class="proxy-row">' +
              '<span class="proxy-row-dot ' + statusDot + '"></span>' +
              '<span class="proxy-row-addr" title="' + p.server + '">' + addr + '</span>' +
              '<div class="proxy-row-bar"><div class="proxy-row-bar-fill" style="width:' + barPct + '%;background:' + barColor + '"></div></div>' +
              '<span class="proxy-row-lat">' + latText + '</span>' +
              '</div>';
          }
          proxyListEl.innerHTML = html;
        }

        // Log pool changes
        if (_lastPoolHealthy !== null && (h !== _lastPoolHealthy || t !== _lastPoolTotal)) {
          var healthyDiff = h - _lastPoolHealthy;
          var totalDiff = t - _lastPoolTotal;
          var diffParts = [];
          if (healthyDiff !== 0) diffParts.push("healthy " + (healthyDiff > 0 ? "+" : "") + healthyDiff);
          if (totalDiff !== 0) diffParts.push("total " + (totalDiff > 0 ? "+" : "") + totalDiff);
          appendProxyLog(healthyDiff >= 0 ? "info" : "warning",
            "Pool: " + h + " healthy / " + t + " total" +
            (diffParts.length ? " (" + diffParts.join(", ") + ")" : "") +
            " | target " + m +
            (avgLat !== null ? " | avg " + Math.round(avgLat) + "ms" : ""));
        }
        _lastPoolHealthy = h;
        _lastPoolTotal = t;
      })
      .catch(function () {
        proxyPoolCounts.textContent = "-- / --";
        proxyPoolCounts.style.color = "var(--text-dim)";
        if (poolTrackerCounts) poolTrackerCounts.textContent = "-- / --";
        if (poolBarFill) {
          poolBarFill.style.width = "0%";
          poolBarFill.className = "pool-tracker-bar-fill empty";
        }
      });
  }

  // Poll pool status every 30 seconds
  setInterval(refreshPoolStatus, 30000);
  refreshPoolStatus();

  if (checkPoolBtn) {
    checkPoolBtn.addEventListener("click", function () {
      proxyPoolStatusEl.textContent = "Checking...";
      proxyPoolStatusEl.style.color = "var(--text-dim)";
      checkPoolBtn.disabled = true;
      fetch("/api/proxy-pool/health-check", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var msg = data.healthy + " healthy, " + data.unhealthy + " unhealthy, " + data.recovered + " recovered";
          if (data.avg_latency_ms) msg += " | avg " + Math.round(data.avg_latency_ms) + "ms";
          proxyPoolStatusEl.textContent = msg;
          proxyPoolStatusEl.style.color = data.healthy > 0 ? "var(--green)" : "var(--orange)";
          appendProxyLog("info", "Health check: " + msg);
          refreshPoolStatus();
        })
        .catch(function (err) {
          proxyPoolStatusEl.textContent = "Check failed: " + err.message;
          proxyPoolStatusEl.style.color = "var(--red)";
        })
        .finally(function () {
          checkPoolBtn.disabled = false;
        });
    });
  }

  if (autoRefreshToggle) {
    autoRefreshToggle.addEventListener("change", function () {
      var endpoint = this.checked ? "start" : "stop";
      var protocol = proxyProtocolInput.value;
      var intervalMin = parseInt(poolRefreshIntervalInput ? poolRefreshIntervalInput.value : "5", 10) || 5;
      var intervalSec = Math.max(60, Math.min(3600, intervalMin * 60));
      var qs = this.checked
        ? "?protocol=" + encodeURIComponent(protocol) + "&limit=20&interval=" + intervalSec
        : "";
      fetch("/api/proxy-pool/auto-refresh/" + endpoint + qs, { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function () {
          proxyPoolStatusEl.textContent = autoRefreshToggle.checked
            ? "Auto-refresh started (" + intervalMin + "min)"
            : "Auto-refresh stopped";
          proxyPoolStatusEl.style.color = "var(--green)";
          appendProxyLog("info", autoRefreshToggle.checked
            ? "Auto-refresh started (" + intervalMin + "min interval)"
            : "Auto-refresh stopped");
          refreshPoolStatus();
        })
        .catch(function (err) {
          proxyPoolStatusEl.textContent = "Error: " + err.message;
          proxyPoolStatusEl.style.color = "var(--red)";
        });
    });
  }

  if (poolRefreshIntervalInput) {
    var _intervalTimer = null;
    poolRefreshIntervalInput.addEventListener("input", function () {
      var el = this;
      clearTimeout(_intervalTimer);
      _intervalTimer = setTimeout(function () {
        var intervalMin = parseInt(el.value, 10);
        if (!intervalMin || intervalMin < 1) return;
        intervalMin = Math.min(60, Math.max(1, intervalMin));
        el.value = intervalMin;
        // Only restart if auto-refresh is currently active
        if (autoRefreshToggle && autoRefreshToggle.checked) {
          var intervalSec = intervalMin * 60;
          var protocol = proxyProtocolInput.value;
          fetch("/api/proxy-pool/auto-refresh/start?protocol=" + encodeURIComponent(protocol) + "&limit=20&interval=" + intervalSec, { method: "POST" })
            .then(function (r) { return r.json(); })
            .then(function () {
              proxyPoolStatusEl.textContent = "Auto-refresh interval updated (" + intervalMin + "min)";
              proxyPoolStatusEl.style.color = "var(--green)";
              appendProxyLog("info", "Auto-refresh interval updated to " + intervalMin + "min");
              refreshPoolStatus();
            })
            .catch(function (err) {
              proxyPoolStatusEl.textContent = "Error: " + err.message;
              proxyPoolStatusEl.style.color = "var(--red)";
            });
        }
      }, 800);
    });
  }

  if (poolMaxSizeInput) {
    var _maxSizeTimer = null;
    poolMaxSizeInput.addEventListener("input", function () {
      var el = this;
      clearTimeout(_maxSizeTimer);
      _maxSizeTimer = setTimeout(function () {
        var limit = parseInt(el.value, 10);
        if (!limit || limit < 1) return;
        limit = Math.min(500, limit);
        el.value = limit;
        fetch("/api/proxy-pool/max-size?limit=" + limit, { method: "POST" })
          .then(function (r) {
            if (!r.ok) throw new Error("status " + r.status);
            return r.json();
          })
          .then(function () {
            proxyPoolStatusEl.textContent = "Max pool size set to " + limit;
            proxyPoolStatusEl.style.color = "var(--green)";
            refreshPoolStatus();
          })
          .catch(function (err) {
            proxyPoolStatusEl.textContent = "Error: " + err.message;
            proxyPoolStatusEl.style.color = "var(--red)";
          });
      }, 600);
    });
  }

  if (poolMaxLatencyInput) {
    var _maxLatencyTimer = null;
    poolMaxLatencyInput.addEventListener("input", function () {
      var el = this;
      clearTimeout(_maxLatencyTimer);
      _maxLatencyTimer = setTimeout(function () {
        var ms = parseInt(el.value, 10);
        if (!ms || ms < 500) return;
        ms = Math.min(30000, ms);
        el.value = ms;
        fetch("/api/proxy-pool/max-latency?ms=" + ms, { method: "POST" })
          .then(function (r) {
            if (!r.ok) throw new Error("status " + r.status);
            return r.json();
          })
          .then(function () {
            proxyPoolStatusEl.textContent = "Max latency set to " + ms + "ms";
            proxyPoolStatusEl.style.color = "var(--green)";
            appendProxyLog("info", "Latency threshold set to " + ms + "ms");
            refreshPoolStatus();
          })
          .catch(function (err) {
            proxyPoolStatusEl.textContent = "Error: " + err.message;
            proxyPoolStatusEl.style.color = "var(--red)";
          });
      }, 600);
    });
  }

  // ══════════════════════════════════════
  // Message Handlers
  // ══════════════════════════════════════

  function handleMessage(msg) {
    var runId = msg.run_id;

    // Route to instruction mode handlers if active
    if (runId && instructionRunning && promptCards[runId]) {
      switch (msg.type) {
        case "worker_update":
          updateCardWorker(runId, msg);
          break;
        case "worker_result":
          onCardWorkerResult(runId, msg.result);
          break;
        case "worker_partial_result":
          onCardPartialResult(runId, msg.result);
          break;
        case "run_progress":
          updateCardProgress(runId, msg);
          break;
        case "run_complete":
          onCardRunComplete(runId, msg);
          runNextInstruction();
          break;
        case "run_cancelled":
          onCardRunCancelled(runId);
          runNextInstruction();
          break;
        case "run_paused":
          onCardRunPaused(runId);
          break;
        case "run_resumed":
          onCardRunResumed(runId);
          break;
        case "log":
          appendLog(msg.level, msg.text, msg.worker_id);
          break;
      }
      return;
    }

    // Original message handling (manual + file mode)
    switch (msg.type) {
      case "worker_update":
        updateWorkerCard(msg);
        updateResultRow(msg);
        break;
      case "worker_result":
        onWorkerResult({ ...msg.result, run_id: msg.run_id || null });
        break;
      case "worker_partial_result":
        onWorkerPartialResult({ ...msg.result, run_id: msg.run_id || null });
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
        highlightWorker(msg.worker_id, "challenge", msg.run_id);
        break;
      case "log":
        appendLog(msg.level, msg.text, msg.worker_id);
        break;
      case "error":
        appendLog("error", msg.message);
        showToast(msg.message, "error");
        if (running) {
          resetRunControlState();
        }
        break;
      case "toast":
        showToast(msg.message, msg.level || "success");
        break;
      case "preview_screenshots":
        onPreviewScreenshots(msg.screenshots || []);
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
    activeRunId = null;
    updateStartButton();
    stopBtn.disabled = true;
  }

  function resetPreviewGrid() {
    previewGrid.innerHTML =
      '<div class="preview-empty">' +
      '<div class="preview-empty-icon">&#128247;</div>' +
      '<span>No windows active</span>' +
      '<span>Start a run to see live previews</span>' +
      '</div>';
    previewStatus.textContent = "No windows active";
  }

  function clearDashboardState() {
    resetRunControlState();
    workerStartTimes = {};
    workerData = {};
    incrementalResults = {};
    runResults = null;
    htmlBlocksCache = {};
    previewEntryKeys = [];
    previewIndex = 0;
    currentResponseText = "";
    currentResponseModelName = "";

    workersContainer.innerHTML = "";
    resultsBody.innerHTML = "";
    resultsJsonPre.textContent = "";
    htmlResultsList.innerHTML = '<div class="html-empty">No results yet</div>';
    logBox.innerHTML = "";
    progressFill.style.width = "0%";
    progressPct.textContent = "0%";
    etaText.textContent = "ETA: \u2014";
    exportBtn.disabled = true;
    responseModal.classList.add("hidden");
    htmlPreviewModal.classList.add("hidden");
    resetPreviewGrid();
  }

  // ══════════════════════════════════════
  // Window Cards (Right Column)
  // ══════════════════════════════════════

  function getWorkerKey(workerId, runId) {
    return (runId || "default") + "::" + workerId;
  }

  function getWorkerDomId(workerId, runId) {
    return "worker-" + getWorkerKey(workerId, runId).replace(/[^\w-]/g, "_");
  }

  function getRunLabel(runId) {
    if (!runId) return "";
    const card = promptCards[runId];
    if (card && card.index) return "P#" + card.index;
    if (runId.startsWith("card_")) return "P#?";
    return runId.slice(0, 6);
  }

  function getResultKey(workerId, runId) {
    return getWorkerKey(workerId, runId);
  }

  function getResultDomId(workerId, runId) {
    return "result-row-" + getResultKey(workerId, runId).replace(/[^\w-]/g, "_");
  }

  function ensureWorkerCard(id, runId) {
    const cardId = getWorkerDomId(id, runId);
    const runLabel = getRunLabel(runId);
    let card = document.getElementById(cardId);
    if (!card && runId) {
      const legacyCard = document.getElementById(getWorkerDomId(id, null));
      if (legacyCard && (!legacyCard.dataset.runId || legacyCard.dataset.runId === "")) {
        legacyCard.id = cardId;
        legacyCard.dataset.workerKey = getWorkerKey(id, runId);
        legacyCard.dataset.runId = runId;
        card = legacyCard;
      }
    }
    if (!card) {
      card = document.createElement("div");
      card.id = cardId;
      card.dataset.workerKey = getWorkerKey(id, runId);
      card.dataset.runId = runId || "";
      card.className = "window-card";
      card.innerHTML = `
        <div class="window-card-header">
          <div class="window-card-left">
            <span class="window-dot"></span>
            <span class="window-title">Window #${id + 1}</span>
            ${runLabel ? `<span class="window-run-badge">${runLabel}</span>` : ""}
            <span class="window-proxy-badge hidden"></span>
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
    const runId = msg.run_id || null;
    const workerKey = getWorkerKey(msg.worker_id, runId);
    const card = ensureWorkerCard(msg.worker_id, runId);
    const dot = card.querySelector(".window-dot");
    const badge = card.querySelector(".window-card-badge");
    const fill = card.querySelector(".window-progress-fill");
    const info = card.querySelector(".window-info");
    const proxyBadge = card.querySelector(".window-proxy-badge");

    // Update proxy badge
    if (msg.proxy) {
      proxyBadge.textContent = msg.proxy.replace(/^https?:\/\//, "").replace(/^socks[45]:\/\//, "");
      proxyBadge.classList.remove("hidden");
    }

    // Track start times
    if (!workerStartTimes[workerKey] && msg.state !== "idle") {
      workerStartTimes[workerKey] = Date.now();
    }

    // Update progress
    fill.style.width = `${msg.progress_pct}%`;

    // Store worker data
    workerData[workerKey] = {
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

    const elapsed = workerStartTimes[workerKey]
      ? Math.round((Date.now() - workerStartTimes[workerKey]) / 1000)
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

  function highlightWorker(id, cls, runId) {
    const card = ensureWorkerCard(id, runId);
    card.classList.add(`state-${cls}`);
  }

  // ══════════════════════════════════════
  // Results Table (Live Updates)
  // ══════════════════════════════════════

  function ensureResultRow(id, runId) {
    const resultId = getResultDomId(id, runId);
    const runLabel = getRunLabel(runId);
    let row = document.getElementById(resultId);
    if (!row && runId) {
      const legacyRow = document.getElementById(getResultDomId(id, null));
      if (legacyRow) {
        legacyRow.id = resultId;
        row = legacyRow;
      }
    }
    if (!row) {
      row = document.createElement("tr");
      row.id = resultId;
      row.innerHTML = `
        <td class="row-num">${runLabel ? runLabel + "-" : ""}W${id + 1}</td>
        <td class="col-model-a text-queued">Queued</td>
        <td class="col-model-b text-queued">Queued</td>
        <td class="col-time">&mdash;</td>
        <td class="col-tokens">&mdash;</td>
        <td class="col-status"><span class="badge badge-queued">&#9201; Queued</span></td>
      `;
      resultsBody.appendChild(row);
      if (autoScroll) {
        row.scrollIntoView({ behavior: "smooth", block: "end" });
      }
    }
    return row;
  }

  function updateResultRow(msg) {
    const runId = msg.run_id || null;
    const workerKey = getWorkerKey(msg.worker_id, runId);
    const row = ensureResultRow(msg.worker_id, runId);
    const colA = row.querySelector(".col-model-a");
    const colB = row.querySelector(".col-model-b");
    const colTime = row.querySelector(".col-time");
    const colTokens = row.querySelector(".col-tokens");
    const colStatus = row.querySelector(".col-status");

    const elapsed = workerStartTimes[workerKey]
      ? Math.round((Date.now() - workerStartTimes[workerKey]) / 1000)
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

  function buildResultCellHTML(modelName, responseText, workerId, side, runId) {
    const name = modelName || "\u2014";
    const text = responseText || "\u2014";
    const hasResponse = responseText && responseText !== "\u2014";
    return `
      <div class="response-cell">
        <span class="response-model-name">${escapeHtml(name)}</span>
        <span class="response-text-preview">${escapeHtml(truncate(text, 80))}</span>
        ${hasResponse ? `<div class="response-actions">
          <button class="btn-view-response" data-worker-id="${workerId}" data-run-id="${escapeAttr(runId || "")}" data-side="${side}">&#128065; View</button>
          <button class="btn-copy-inline" data-worker-id="${workerId}" data-run-id="${escapeAttr(runId || "")}" data-side="${side}">&#128203; Copy</button>
        </div>` : ""}
      </div>
    `;
  }

  function updateResultRowWithData(result) {
    const row = ensureResultRow(result.worker_id, result.run_id || null);
    const colA = row.querySelector(".col-model-a");
    const colB = row.querySelector(".col-model-b");
    const colTime = row.querySelector(".col-time");
    const colTokens = row.querySelector(".col-tokens");
    const colStatus = row.querySelector(".col-status");

    colA.className = "col-model-a";
    colA.innerHTML = buildResultCellHTML(result.model_a_name, result.model_a_response, result.worker_id, "a", result.run_id || null);

    colB.className = "col-model-b";
    colB.innerHTML = buildResultCellHTML(result.model_b_name, result.model_b_response, result.worker_id, "b", result.run_id || null);

    colTime.textContent = result.elapsed_seconds ? result.elapsed_seconds.toFixed(0) + "s" : "\u2014";

    const responseA = result.model_a_response || "";
    const responseB = result.model_b_response || "";
    const tokens = estimateTokens(responseA + responseB);
    colTokens.textContent = tokens > 0 ? formatNumber(tokens) : "\u2014";

    colStatus.innerHTML = result.error
      ? '<span class="badge badge-error">&#10007; Error</span>'
      : '<span class="badge badge-done">&#10003; Done</span>';

    if (autoScroll) {
      row.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }

  function onWorkerResult(result) {
    const key = getResultKey(result.worker_id, result.run_id || null);
    incrementalResults[key] = result;
    updateResultRowWithData(result);
    if (tabHtml.classList.contains("active")) renderHtmlPreviews();
  }

  function onWorkerPartialResult(partial) {
    const runId = partial.run_id || null;
    const key = getResultKey(partial.worker_id, runId);
    var row = ensureResultRow(partial.worker_id, runId);
    var side = partial.slide;
    var col = row.querySelector(side === "a" ? ".col-model-a" : ".col-model-b");
    var colStatus = row.querySelector(".col-status");

    col.className = side === "a" ? "col-model-a" : "col-model-b";
    col.innerHTML = buildResultCellHTML(
      partial.model_name,
      partial.response,
      partial.worker_id,
      side,
      runId
    );

    if (!colStatus.querySelector(".badge-done")) {
      colStatus.innerHTML = '<span class="badge badge-partial">&#189; Partial</span>';
    }

    if (!incrementalResults[key]) {
      incrementalResults[key] = { worker_id: partial.worker_id, run_id: runId };
    }
    var ir = incrementalResults[key];
    if (side === "a") {
      ir.model_a_name = partial.model_name;
      ir.model_a_response = partial.response;
      ir.model_a_response_html = partial.response_html;
    } else {
      ir.model_b_name = partial.model_name;
      ir.model_b_response = partial.response;
      ir.model_b_response_html = partial.response_html;
    }

    if (autoScroll) {
      row.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }

  function populateFinalResults(results, append) {
    if (!append) {
      resultsBody.innerHTML = "";
    }
    results.forEach((r) => {
      const key = getResultKey(r.worker_id, r.run_id || null);
      incrementalResults[key] = r;
      ensureResultRow(r.worker_id, r.run_id || null);
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

  function onRunComplete(msg, options) {
    const append = !!(options && options.append);
    if (!append) {
      resetRunControlState();
    }
    exportBtn.disabled = false;
    if (!append) {
      progressFill.style.width = "100%";
      progressPct.textContent = "100%";
      etaText.textContent = "Complete";
    }

    // Store results for JSON view
    if (!append) {
      runResults = msg.results;
    } else {
      runResults = (runResults || []).concat(msg.results || []);
    }

    const enrichedResults = (msg.results || []).map((r) => ({
      ...r,
      run_id: r.run_id || msg.run_id || null,
    }));

    // Populate final results table
    populateFinalResults(enrichedResults, append);

    // Reconcile worker cards with final results in case a worker missed a
    // terminal state event during orchestrator-side recovery handling.
    enrichedResults.forEach((r) => {
      updateWorkerCard({
        worker_id: r.worker_id,
        run_id: r.run_id || msg.run_id || null,
        state: r.error ? "error" : "complete",
        progress_pct: 100,
        message: r.error || "",
        error: r.error || null,
      });
    });

    // Update JSON view
    resultsJsonPre.textContent = JSON.stringify(runResults, null, 2);

    // Update footer stats
    updateStats(msg);

    // Refresh HTML previews if HTML tab is active
    if (tabHtml.classList.contains("active")) renderHtmlPreviews();

    const failed = enrichedResults.filter((r) => !!r.error).length;
    const succeeded = enrichedResults.length - failed;
    const runPrefix = msg.run_id ? `[${getRunLabel(msg.run_id) || msg.run_id}] ` : "";
    const hasTurns = enrichedResults.some((r) => (r.turn_index || 0) > 0);
    const unitLabel = hasTurns ? "result(s)" : "window(s)";
    appendLog(
      failed > 0 ? "warning" : "info",
      failed > 0
        ? `${runPrefix}Run complete \u2014 ${succeeded} succeeded, ${failed} failed in ${msg.total_elapsed_seconds.toFixed(1)}s`
        : `${runPrefix}Run complete \u2014 ${enrichedResults.length} ${unitLabel} finished in ${msg.total_elapsed_seconds.toFixed(1)}s`
    );
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
    line.style.animation = "fade-in-up 0.15s ease-out";
    logBox.appendChild(line);

    // Limit log lines
    while (logBox.children.length > 500) {
      logBox.removeChild(logBox.firstChild);
    }

    if (autoScroll) {
      logBox.scrollTo({ top: logBox.scrollHeight, behavior: "smooth" });
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
      const controlMsg = { type: paused ? "resume_run" : "pause_run" };
      if (activeRunId) controlMsg.run_id = activeRunId;
      send(controlMsg);
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
    activeRunId = generateRunId();
    runStartTime = Date.now();
    workerStartTimes = {};
    workerData = {};
    updateStartButton();
    stopBtn.disabled = false;
    exportBtn.disabled = true;
    workersContainer.innerHTML = "";
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
      ensureWorkerCard(i, activeRunId);
      ensureResultRow(i, activeRunId);
    }

    send({
      type: "start_run",
      run_id: activeRunId,
      prompt: singlePrompt,
      prompts: isFileMode ? prompts : null,
      system_prompt: systemPromptInput.value.trim() || "",
      combine_with_first: combineWithFirstInput.checked,
      window_count: windowCount,
      submission_gap_seconds: parseFloat(submissionGapInput.value) || null,
      prompts_per_session: parseInt(promptsPerSessionInput.value, 10) || 1,
      model_a: modelAInput.value.trim() || null,
      model_b: modelBInput.value.trim() || null,
      retain_output: retainOutputInput.value,
      clear_cookies: clearCookiesInput.checked,
      incognito: incognitoModeInput.checked,
      minimized: minimizedModeInput.checked,
      headless: headlessModeInput.checked,
      images: (!isFileMode && uploadedImages.length > 0)
        ? uploadedImages.map((img) => ({
            data: img.data,
            mime_type: img.mime_type,
            filename: img.filename,
          }))
        : null,
      simultaneous_start: simultaneousStartInput.checked,
      zoom_pct: parseInt(zoomInput.value, 10) || 100,
      start_monitor: parseInt(startMonitorInput.value, 10) || 1,
      monitor_count: parseInt(monitorCountInput.value, 10) || 1,
      monitor_width: monW,
      monitor_height: monH,
      taskbar_height: parseInt(taskbarHeightInput.value, 10) || 0,
      margin: parseInt(tileMarginInput.value, 10) || 0,
      proxies: parseProxyList(proxyListInput.value),
      proxy_on_challenge: proxyOnChallengeInput.checked,
      windows_per_proxy: parseInt(windowsPerProxyInput.value, 10) || 4,
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
    const stopMsg = { type: "stop_run" };
    if (activeRunId) stopMsg.run_id = activeRunId;
    send(stopMsg);
    appendLog("warning", "Stop requested...");
  });

  closeAllBtn.addEventListener("click", () => {
    closeAllBtn.disabled = true;
    fetch("/api/close-all-windows", { method: "POST" })
      .then(function (res) {
        if (!res.ok) throw new Error("status " + res.status);
        return res.json();
      })
      .then(function () {
        resetRunControlState();
        resetPreviewGrid();
        appendLog("warning", "Close All requested - all open windows closed");
        showToast("All open windows closed", "info");
      })
      .catch(function (err) {
        showToast("Failed to close windows: " + err.message, "error");
      })
      .finally(function () {
        closeAllBtn.disabled = false;
      });
  });

  clearAllBtn.addEventListener("click", () => {
    const hadRunning = running;
    clearDashboardState();
    appendLog("info", hadRunning
      ? "Dashboard cleared - incoming updates may appear again while a run is still active"
      : "Dashboard cleared");
    showToast("Processing, windows, and logs cleared", "info");
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

  document.querySelectorAll(".export-option").forEach((link) => {
    link.addEventListener("click", () => {
      const raw = link.getAttribute("href") || "";
      const basePath = raw.split("?")[0];
      const url = new URL(basePath, window.location.origin);
      if (promptMode === "instruction") {
        url.searchParams.set("scope", "all");
      }
      link.setAttribute("href", url.pathname + url.search);
    });
  });

  // ══════════════════════════════════════
  // Prompt Mode Toggle (Manual / File Upload)
  // ══════════════════════════════════════

  function setPromptMode(mode) {
    promptMode = mode;
    modeManualBtn.classList.toggle("active", mode === "manual");
    modeFileBtn.classList.toggle("active", mode === "file");
    if (modeInstructionBtn) modeInstructionBtn.classList.toggle("active", mode === "instruction");
    manualSection.classList.toggle("hidden", mode !== "manual");
    fileSection.classList.toggle("hidden", mode !== "file");
    if (instructionSection) instructionSection.classList.toggle("hidden", mode !== "instruction");
    // Show/hide paste & clear buttons (only for manual mode)
    pasteBtn.style.display = mode === "manual" ? "" : "none";
    clearBtn.style.display = mode === "manual" ? "" : "none";
    // Show/hide system prompt details (manual + file modes only)
    var sysDet = document.getElementById("system-prompt-details");
    if (sysDet) sysDet.style.display = mode === "file" ? "" : "none";
    // Show/hide global start/stop (not in instruction mode — it has its own)
    var promptControls = document.querySelector(".prompt-controls");
    if (promptControls) promptControls.style.display = mode === "instruction" ? "none" : "";
    saveSettings();
  }

  modeManualBtn.addEventListener("click", () => setPromptMode("manual"));
  modeFileBtn.addEventListener("click", () => setPromptMode("file"));
  if (modeInstructionBtn) modeInstructionBtn.addEventListener("click", () => setPromptMode("instruction"));

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

  // Attach button opens file picker
  document.getElementById("btn-attach").addEventListener("click", () => imageFileInput.click());

  // Drag-drop images onto the prompt input area
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
    const imageFiles = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith("image/"));
    if (imageFiles.length > 0) {
      handleImageFiles(imageFiles);
    }
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

    // Update attach hint
    const hint = document.getElementById("attach-hint");
    if (hint) {
      hint.textContent = uploadedImages.length > 0
        ? `${uploadedImages.length} image${uploadedImages.length > 1 ? "s" : ""} attached`
        : "";
    }
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
        batchAggSize: batchAggSizeInput.value,
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
      batchAggSizeInput.value = state.batchAggSize || 1;
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
    const rangedPrompts = allPrompts.slice(start - 1, clampedEnd);

    // Aggregate N rows into a single prompt (batch aggregation)
    const aggSize = Math.max(1, parseInt(batchAggSizeInput.value, 10) || 1);
    if (aggSize > 1) {
      uploadedPrompts = [];
      for (let i = 0; i < rangedPrompts.length; i += aggSize) {
        uploadedPrompts.push(rangedPrompts.slice(i, i + aggSize).join("\n\n"));
      }
    } else {
      uploadedPrompts = rangedPrompts;
    }

    // Update aggregation info
    if (aggSize > 1) {
      batchAggInfo.textContent = `(${rangedPrompts.length} rows \u2192 ${uploadedPrompts.length} prompt(s))`;
    } else {
      batchAggInfo.textContent = "";
    }

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
  batchAggSizeInput.addEventListener("input", onColumnChange);

  function updateBatchInfo() {
    const wc = parseInt(windowCountInput.value, 10) || 4;
    const total = uploadedPrompts.length;
    if (total === 0) {
      batchInfoDiv.textContent = "";
      return;
    }
    const batches = Math.ceil(total / wc);
    const pps = parseInt(promptsPerSessionInput.value, 10) || 1;
    const sessions = Math.ceil(batches / pps);
    const navs = Math.max(sessions - 1, 0);
    batchInfoDiv.textContent = `${total} prompt(s) \u2192 ${batches} batch(es) of ${wc} window(s) \u2192 ${sessions} session(s), ${navs} re-nav(s)`;
  }

  // Update batch info when window count changes
  windowCountInput.addEventListener("input", updateBatchInfo);
  promptsPerSessionInput.addEventListener("input", updateBatchInfo);

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
    // HTML preview carousel keyboard nav
    if (!htmlPreviewModal.classList.contains("hidden")) {
      if (e.key === "Escape") { htmlPreviewModal.classList.add("hidden"); return; }
      if (e.key === "ArrowLeft") { htmlPrevBtn.click(); e.preventDefault(); return; }
      if (e.key === "ArrowRight") { htmlNextBtn.click(); e.preventDefault(); return; }
      if (e.key === "1") { previewMode = "a"; renderPreview(); return; }
      if (e.key === "2") { previewMode = "b"; renderPreview(); return; }
      if (e.key === "3") { previewMode = "both"; renderPreview(); return; }
      if (e.key === "f") { previewZoom = "fit"; applyPreviewZoom(); return; }
      if (e.key === "0") { previewZoom = "1"; applyPreviewZoom(); return; }
      return;
    }
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
      const runId = viewBtn.dataset.runId || null;
      const side = viewBtn.dataset.side;
      const key = getResultKey(workerId, runId);
      const result = incrementalResults[key] || (runResults && runResults.find(r => r.worker_id === workerId && (r.run_id || null) === runId));
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
      const runId = copyBtn.dataset.runId || null;
      const side = copyBtn.dataset.side;
      const key = getResultKey(workerId, runId);
      const result = incrementalResults[key] || (runResults && runResults.find(r => r.worker_id === workerId && (r.run_id || null) === runId));
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
  // Main Tabs (Main / Live Preview)
  // ══════════════════════════════════════

  const mainTabResults  = document.getElementById("main-tab-results");
  const mainTabPreview  = document.getElementById("main-tab-preview");

  let currentMainTab = "results";   // "results" or "preview"
  let currentSubTab  = "table";     // "table", "json", "html"

  function setMainTab(tab) {
    currentMainTab = tab;
    mainTabResults.classList.toggle("active", tab === "results");
    mainTabPreview.classList.toggle("active", tab === "preview");

    // Toggle full-screen preview mode
    document.body.classList.toggle("preview-active", tab === "preview");
    previewGridWrap.classList.toggle("hidden", tab !== "preview");

    // Subscribe/unsubscribe to preview screenshots
    if (tab === "preview" && !previewSubscribed) {
      send({ type: "subscribe_preview" });
      previewSubscribed = true;
    } else if (tab !== "preview" && previewSubscribed) {
      send({ type: "unsubscribe_preview" });
      previewSubscribed = false;
    }
  }

  function setSubTab(active) {
    currentSubTab = active;
    tabTable.classList.toggle("active", active === "table");
    tabJson.classList.toggle("active", active === "json");
    tabHtml.classList.toggle("active", active === "html");
    resultsTableWrap.classList.toggle("hidden", active !== "table");
    resultsJsonPre.classList.toggle("hidden", active !== "json");
    resultsHtmlWrap.classList.toggle("hidden", active !== "html");
  }

  mainTabResults.addEventListener("click", () => {
    setMainTab("results");
  });

  mainTabPreview.addEventListener("click", () => {
    setMainTab("preview");
  });

  tabTable.addEventListener("click", () => {
    setSubTab("table");
  });

  tabJson.addEventListener("click", () => {
    setSubTab("json");
  });

  tabHtml.addEventListener("click", () => {
    setSubTab("html");
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
    // Workers are stacked vertically in the sidebar — no grid needed
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

  // ── HTML Preview Modal — Carousel ──
  let htmlBlocksCache = {};
  let previewEntryKeys = [];
  let previewIndex = 0;
  let previewMode = "both"; // "a" | "b" | "both"
  let previewZoom = "fit";  // "fit" | "1" | "0.75" | "0.5"

  const htmlPreviewModal = document.getElementById("html-preview-modal");
  const htmlPreviewContent = document.getElementById("html-preview-content");
  const htmlPageInfo = document.getElementById("html-page-info");
  const htmlPrevBtn = document.getElementById("html-prev");
  const htmlNextBtn = document.getElementById("html-next");
  const htmlModeA = document.getElementById("html-mode-a");
  const htmlModeB = document.getElementById("html-mode-b");
  const htmlModeBoth = document.getElementById("html-mode-both");

  function buildHtmlPreviewKey(result, seq) {
    const runId = result.run_id || "default";
    const wid = Number.isFinite(result.worker_id) ? result.worker_id : -1;
    const batch = Number.isFinite(result.batch_index) ? result.batch_index : 0;
    const turn = Number.isFinite(result.turn_index) ? result.turn_index : 0;
    return `${runId}::b${batch}::t${turn}::w${wid}::s${seq}`;
  }

  function getRunSortIndex(runId) {
    if (!runId) return Number.MAX_SAFE_INTEGER;
    const card = promptCards[runId];
    if (card && Number.isFinite(card.index)) return card.index;
    return Number.MAX_SAFE_INTEGER;
  }

  function formatPreviewLabelSafe(meta) {
    const runLabel = getRunLabel(meta.run_id);
    const left = runLabel ? `${runLabel} | ` : "";
    return `${left}W${meta.worker_id + 1} | T${meta.turn_index + 1}`;
  }

  function formatPreviewLabel(meta) {
    const runLabel = getRunLabel(meta.run_id);
    const left = runLabel ? `${runLabel} · ` : "";
    return `${left}W${meta.worker_id + 1} · T${meta.turn_index + 1}`;
  }

  function renderHtmlPreviews() {
    htmlResultsList.innerHTML = "";
    htmlBlocksCache = {};
    const results = runResults || Object.values(incrementalResults);
    if (!results || results.length === 0) {
      htmlResultsList.innerHTML = '<div class="html-empty">No results yet</div>';
      return;
    }

    let hasAnyHtml = false;
    results.forEach((r, seq) => {
      const blocksA = extractHtmlBlocks(r.model_a_response_html);
      const blocksB = extractHtmlBlocks(r.model_b_response_html);
      if (blocksA.length === 0 && blocksB.length === 0) return;
      hasAnyHtml = true;

      const previewKey = buildHtmlPreviewKey(r, seq);
      htmlBlocksCache[previewKey] = {
        a: blocksA,
        b: blocksB,
        nameA: r.model_a_name || "Model A",
        nameB: r.model_b_name || "Model B",
        meta: {
          key: previewKey,
          seq: seq,
          run_id: r.run_id || null,
          worker_id: Number.isFinite(r.worker_id) ? r.worker_id : 0,
          batch_index: Number.isFinite(r.batch_index) ? r.batch_index : 0,
          turn_index: Number.isFinite(r.turn_index) ? r.turn_index : 0,
        },
      };
      const meta = htmlBlocksCache[previewKey].meta;
      const previewLabel = formatPreviewLabelSafe(meta);

      const card = document.createElement("div");
      card.className = "html-result-card";
      const row = document.createElement("div");
      row.className = "html-result-row";
      row.innerHTML = `
        <span class="html-result-window">${escapeHtml(previewLabel)}</span>
        <div class="html-result-models">
          <button class="btn-html-preview" data-preview-key="${escapeAttr(previewKey)}" data-side="a"
            ${blocksA.length === 0 ? "disabled" : ""}>
            ${escapeHtml(r.model_a_name || "Model A")}
            ${blocksA.length > 0 ? "&#8212; " + blocksA.length + " block(s)" : "&#8212; no HTML"}
          </button>
          <button class="btn-html-preview" data-preview-key="${escapeAttr(previewKey)}" data-side="b"
            ${blocksB.length === 0 ? "disabled" : ""}>
            ${escapeHtml(r.model_b_name || "Model B")}
            ${blocksB.length > 0 ? "&#8212; " + blocksB.length + " block(s)" : "&#8212; no HTML"}
          </button>
          <button class="btn-html-preview btn-html-compare" data-preview-key="${escapeAttr(previewKey)}" data-side="both"
            ${blocksA.length === 0 && blocksB.length === 0 ? "disabled" : ""}>
            &#9881; Compare
          </button>
        </div>
      `;
      card.appendChild(row);
      htmlResultsList.appendChild(card);
    });

    if (!hasAnyHtml) {
      htmlResultsList.innerHTML = '<div class="html-empty">No HTML code blocks found in responses</div>';
    }
  }

  // Open modal from button list
  htmlResultsList.addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-html-preview");
    if (!btn || btn.disabled) return;
    const previewKey = btn.dataset.previewKey || "";
    const side = btn.dataset.side;
    openHtmlPreviewModal(previewKey, side);
  });

  function openHtmlPreviewModal(previewKey, side) {
    // Build ordered list of all result entries that have HTML (across runs/turns).
    previewEntryKeys = Object.keys(htmlBlocksCache).sort((a, b) => {
      const ma = htmlBlocksCache[a] && htmlBlocksCache[a].meta;
      const mb = htmlBlocksCache[b] && htmlBlocksCache[b].meta;
      if (!ma && !mb) return 0;
      if (!ma) return 1;
      if (!mb) return -1;
      const runCmp = getRunSortIndex(ma.run_id) - getRunSortIndex(mb.run_id);
      if (runCmp !== 0) return runCmp;
      if (ma.batch_index !== mb.batch_index) return ma.batch_index - mb.batch_index;
      if (ma.turn_index !== mb.turn_index) return ma.turn_index - mb.turn_index;
      if (ma.worker_id !== mb.worker_id) return ma.worker_id - mb.worker_id;
      return ma.seq - mb.seq;
    });
    previewIndex = Math.max(0, previewEntryKeys.indexOf(previewKey));
    previewMode = side;
    renderPreview();
    htmlPreviewModal.classList.remove("hidden");
  }

  function renderPreview() {
    const entryKey = previewEntryKeys[previewIndex];
    const cache = htmlBlocksCache[entryKey];
    if (!cache) return;
    const previewLabel = formatPreviewLabelSafe(cache.meta);

    // Update page info
    htmlPageInfo.textContent = `${previewLabel} (${previewIndex + 1}/${previewEntryKeys.length})`;

    // Update nav button states
    htmlPrevBtn.disabled = previewIndex === 0;
    htmlNextBtn.disabled = previewIndex === previewEntryKeys.length - 1;

    // Update mode button labels + active state
    htmlModeA.textContent = cache.nameA;
    htmlModeB.textContent = cache.nameB;
    htmlModeBoth.textContent = "Compare";
    htmlModeA.classList.toggle("active", previewMode === "a");
    htmlModeB.classList.toggle("active", previewMode === "b");

    htmlModeBoth.classList.toggle("active", previewMode === "both");

    // Render content
    htmlPreviewContent.innerHTML = "";

    function buildFloatingCopyBtn(side, label) {
      const btn = document.createElement("button");
      btn.className = "btn-copy-floating";
      btn.title = `Copy ${label} HTML`;
      btn.textContent = `\u{1F4CB} Copy HTML`;
      btn.addEventListener("click", () => copyModelHtml(side));
      return btn;
    }

    if (previewMode === "both") {
      htmlPreviewContent.className = "html-preview-content html-preview-split";
      [["a", cache.nameA, cache.a], ["b", cache.nameB, cache.b]].forEach(([side, name, blocks]) => {
        const col = document.createElement("div");
        col.className = "html-preview-col";
        if (blocks.length === 0) {
          const empty = document.createElement("div");
          empty.className = "html-panel-empty";
          empty.textContent = "No HTML code blocks";
          col.appendChild(empty);
        } else {
          col.appendChild(buildPreviewIframe(blocks));
          col.appendChild(buildFloatingCopyBtn(side, name));
        }
        htmlPreviewContent.appendChild(col);
      });
    } else {
      htmlPreviewContent.className = "html-preview-content html-preview-single";
      const side = previewMode;
      const name = previewMode === "a" ? cache.nameA : cache.nameB;
      const blocks = previewMode === "a" ? cache.a : cache.b;
      if (blocks.length === 0) {
        const empty = document.createElement("div");
        empty.className = "html-panel-empty";
        empty.textContent = "No HTML code blocks";
        htmlPreviewContent.appendChild(empty);
      } else {
        htmlPreviewContent.appendChild(buildPreviewIframe(blocks));
        htmlPreviewContent.appendChild(buildFloatingCopyBtn(side, name));
      }
    }

    // Update side arrow visibility
    const sideLeft = document.getElementById("html-side-prev");
    const sideRight = document.getElementById("html-side-next");
    sideLeft.classList.toggle("hidden", previewIndex === 0);
    sideRight.classList.toggle("hidden", previewIndex === previewEntryKeys.length - 1);
  }

  function buildPreviewIframe(blocks) {
    const html = blocks.join("\n<hr style='margin:2rem 0;border:1px dashed #ccc'>\n");
    const wrap = document.createElement("div");
    wrap.className = "html-iframe-scaled";
    const iframe = document.createElement("iframe");
    iframe.className = "html-preview-iframe-full";
    iframe.sandbox = "allow-scripts";
    iframe.srcdoc = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #fff; color: #111;
  font-family: system-ui, -apple-system, sans-serif; font-size: 14px;
  overflow: auto; line-height: 1.5; min-height: 0; }
</style></head><body>${html}</body></html>`;
    iframe.onload = () => applyPreviewZoom();
    wrap.appendChild(iframe);
    return wrap;
  }

  function applyPreviewZoom() {
    // Update active button
    document.querySelectorAll("#html-zoom-group .btn-zoom").forEach((b) => {
      b.classList.toggle("active", b.dataset.zoom === previewZoom);
    });

    const iframes = htmlPreviewContent.querySelectorAll(".html-preview-iframe-full");
    iframes.forEach((iframe) => {
      const container = iframe.parentElement;
      if (!container) return;

      if (previewZoom === "1") {
        // 1:1 — actual size, scrollable
        iframe.style.transform = "none";
        iframe.style.width = "100%";
        iframe.style.height = "100%";
        container.style.overflow = "auto";
        try {
          iframe.contentDocument.documentElement.style.overflow = "";
          iframe.contentDocument.body.style.overflow = "";
        } catch {}
        return;
      }

      let scale;
      if (previewZoom === "fit") {
        try {
          const doc = iframe.contentDocument;
          const body = doc.body;
          const root = doc.documentElement;
          const cw = Math.max(
            body?.scrollWidth || 0,
            body?.offsetWidth || 0,
            body?.clientWidth || 0,
            root?.scrollWidth || 0,
            root?.offsetWidth || 0,
            root?.clientWidth || 0,
            1
          );
          const ch = Math.max(
            body?.scrollHeight || 0,
            body?.offsetHeight || 0,
            body?.clientHeight || 0,
            root?.scrollHeight || 0,
            root?.offsetHeight || 0,
            root?.clientHeight || 0,
            1
          );
          const bw = container.clientWidth || 1;
          const bh = container.clientHeight || 1;
          scale = Math.min(bw / cw, bh / ch, 1);
        } catch {
          scale = 1;
        }
      } else {
        scale = parseFloat(previewZoom) || 1;
      }

      container.style.overflow = "hidden";
      iframe.style.width = (100 / scale) + "%";
      iframe.style.height = (100 / scale) + "%";
      iframe.style.transform = `scale(${scale})`;
      // Hide scrollbars inside the iframe content when scaled
      try {
        iframe.contentDocument.documentElement.style.overflow = "hidden";
        iframe.contentDocument.body.style.overflow = "hidden";
      } catch {}
    });
  }

  // Navigation buttons (top bar + side arrows)
  function prevWindow() { if (previewIndex > 0) { previewIndex--; renderPreview(); } }
  function nextWindow() { if (previewIndex < previewEntryKeys.length - 1) { previewIndex++; renderPreview(); } }

  htmlPrevBtn.addEventListener("click", prevWindow);
  htmlNextBtn.addEventListener("click", nextWindow);
  document.getElementById("html-side-prev").addEventListener("click", prevWindow);
  document.getElementById("html-side-next").addEventListener("click", nextWindow);

  // Mode toggle buttons
  [htmlModeA, htmlModeB, htmlModeBoth].forEach((btn) => {
    btn.addEventListener("click", () => {
      previewMode = btn.dataset.mode;
      renderPreview();
    });
  });

  // Zoom buttons
  document.getElementById("html-zoom-group").addEventListener("click", (e) => {
    const btn = e.target.closest(".btn-zoom");
    if (!btn) return;
    previewZoom = btn.dataset.zoom;
    applyPreviewZoom();
  });

  // Copy HTML source — called by floating buttons inside preview panes
  async function copyModelHtml(side) {
    const entryKey = previewEntryKeys[previewIndex];
    const cache = htmlBlocksCache[entryKey];
    if (!cache) return;
    const blocks = side === "a" ? cache.a : cache.b;
    const name = side === "a" ? cache.nameA : cache.nameB;
    if (!blocks || blocks.length === 0) {
      showToast(`No HTML blocks for ${name}`, "warning");
      return;
    }
    try {
      await navigator.clipboard.writeText(blocks.join("\n\n"));
      showToast(`${name} HTML copied`, "success");
    } catch {
      showToast("Clipboard access denied", "warning");
    }
  }

  // Close
  document.getElementById("html-preview-close").addEventListener("click", () => {
    htmlPreviewModal.classList.add("hidden");
  });

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
  // Multi-Prompt Cards
  // ══════════════════════════════════════

  // ── Process a single image file into {data, mime_type, filename, objectUrl} ──
  function processImageFile(file) {
    return new Promise(function (resolve, reject) {
      if (!file.type.match(/^image\/(png|jpeg|webp|gif)$/)) {
        reject(new Error("Unsupported format: " + file.name));
        return;
      }
      if (file.size > MAX_IMAGE_SIZE) {
        reject(new Error(file.name + " exceeds 5 MB limit"));
        return;
      }
      var reader = new FileReader();
      reader.onerror = function () { reject(new Error("Read failed")); };
      reader.onload = function () {
        var img = new Image();
        img.onerror = function () { reject(new Error("Image load failed")); };
        img.onload = function () {
          var w = img.width, h = img.height;
          if (w > MAX_IMAGE_DIM || h > MAX_IMAGE_DIM) {
            var scale = MAX_IMAGE_DIM / Math.max(w, h);
            w = Math.round(w * scale);
            h = Math.round(h * scale);
          }
          var canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          canvas.getContext("2d").drawImage(img, 0, 0, w, h);
          var outType = file.type === "image/png" ? "image/png" : "image/jpeg";
          var quality = outType === "image/jpeg" ? 0.85 : undefined;
          var dataUrl = canvas.toDataURL(outType, quality);
          resolve({
            data: dataUrl.split(",")[1],
            mime_type: outType,
            filename: file.name,
            objectUrl: URL.createObjectURL(file),
          });
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  // ── Build a single turn DOM section ──
  function buildTurnHtml(turnIndex, removable) {
    return (
      '<div class="card-turn" data-turn-index="' + turnIndex + '">' +
        '<div class="card-turn-header">' +
          '<span class="card-turn-label">Turn ' + (turnIndex + 1) + '</span>' +
          (removable
            ? '<button class="btn-remove-turn" title="Remove turn">&times;</button>'
            : '') +
        '</div>' +
        '<textarea class="card-turn-prompt" rows="2" placeholder="Turn ' + (turnIndex + 1) + ' prompt..."></textarea>' +
        '<div class="card-turn-image-area">' +
          '<div class="card-turn-image-drop" title="Click or drop images here">+ Images</div>' +
          '<input type="file" class="card-turn-image-input" accept="image/png,image/jpeg,image/webp,image/gif" multiple style="display:none" />' +
          '<div class="card-turn-thumbs"></div>' +
        '</div>' +
      '</div>'
    );
  }

  // ── Wire events for a single turn section inside a card ──
  function wireTurnEvents(cardId, turnEl) {
    var turnIndex = parseInt(turnEl.getAttribute("data-turn-index"), 10);
    var card = promptCards[cardId];
    var cardEl = turnEl.closest(".prompt-card");

    // Image drop zone
    var dropZone = turnEl.querySelector(".card-turn-image-drop");
    var fileInput = turnEl.querySelector(".card-turn-image-input");

    dropZone.addEventListener("click", function () { fileInput.click(); });
    dropZone.addEventListener("dragover", function (e) { e.preventDefault(); dropZone.classList.add("drag-over"); });
    dropZone.addEventListener("dragleave", function () { dropZone.classList.remove("drag-over"); });
    dropZone.addEventListener("drop", function (e) {
      e.preventDefault();
      dropZone.classList.remove("drag-over");
      if (e.dataTransfer.files.length) handleCardTurnImages(cardId, turnIndex, e.dataTransfer.files);
    });
    fileInput.addEventListener("change", function () {
      if (fileInput.files.length) handleCardTurnImages(cardId, turnIndex, fileInput.files);
      fileInput.value = "";
    });

    // Remove turn button
    var removeBtn = turnEl.querySelector(".btn-remove-turn");
    if (removeBtn) {
      removeBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        removeTurnFromCard(cardId, turnIndex);
      });
    }

    // Preview update on prompt input
    turnEl.querySelector(".card-turn-prompt").addEventListener("input", function () {
      updateCardPreview(cardId);
    });
  }

  // ── Handle image files dropped/selected on a card turn ──
  function handleCardTurnImages(cardId, turnIndex, fileList) {
    var card = promptCards[cardId];
    if (!card || !card.turns[turnIndex]) return;
    var turn = card.turns[turnIndex];
    var remaining = MAX_IMAGES - turn._uploadedImages.length;
    if (remaining <= 0) { showToast("Max " + MAX_IMAGES + " images per turn", "warning"); return; }
    var files = Array.from(fileList).slice(0, remaining);
    files.forEach(function (file) {
      processImageFile(file).then(function (imgData) {
        card.turns[turnIndex]._uploadedImages.push(imgData);
        renderCardTurnThumbs(cardId, turnIndex);
      }).catch(function (err) {
        showToast(err.message, "warning");
      });
    });
  }

  // ── Render image thumbnails for a card turn ──
  function renderCardTurnThumbs(cardId, turnIndex) {
    var card = promptCards[cardId];
    if (!card || !card.turns[turnIndex]) return;
    var images = card.turns[turnIndex]._uploadedImages;
    var cardEl = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
    if (!cardEl) return;
    var turnEls = cardEl.querySelectorAll(".card-turn");
    var turnEl = null;
    for (var i = 0; i < turnEls.length; i++) {
      if (parseInt(turnEls[i].getAttribute("data-turn-index"), 10) === turnIndex) {
        turnEl = turnEls[i];
        break;
      }
    }
    if (!turnEl) return;
    var thumbsContainer = turnEl.querySelector(".card-turn-thumbs");
    thumbsContainer.innerHTML = "";
    images.forEach(function (img, idx) {
      var thumb = document.createElement("div");
      thumb.className = "image-thumb";
      thumb.innerHTML =
        '<img src="' + (img.objectUrl || "data:" + img.mime_type + ";base64," + img.data) + '" alt="' + escapeAttr(img.filename) + '" />' +
        '<button class="image-thumb-remove" data-img-idx="' + idx + '">&times;</button>' +
        '<span class="image-thumb-name">' + escapeHtml(truncate(img.filename, 12)) + '</span>';
      thumbsContainer.appendChild(thumb);
    });
    thumbsContainer.querySelectorAll(".image-thumb-remove").forEach(function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var imgIdx = parseInt(btn.getAttribute("data-img-idx"), 10);
        var turnImgs = card.turns[turnIndex]._uploadedImages;
        if (turnImgs[imgIdx] && turnImgs[imgIdx].objectUrl) URL.revokeObjectURL(turnImgs[imgIdx].objectUrl);
        turnImgs.splice(imgIdx, 1);
        renderCardTurnThumbs(cardId, turnIndex);
      });
    });
  }

  // ── Add a turn to a card ──
  function addTurnToCard(cardId) {
    var card = promptCards[cardId];
    if (!card) return -1;
    if (card.turns.length >= 10) { showToast("Max 10 turns per card", "warning"); return -1; }
    var turnIndex = card.turns.length;
    card.turns.push({ text: "", _uploadedImages: [] });
    var cardEl = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
    if (!cardEl) return turnIndex;
    var container = cardEl.querySelector(".card-turns-container");
    var turnHtml = buildTurnHtml(turnIndex, true);
    var temp = document.createElement("div");
    temp.innerHTML = turnHtml;
    var turnEl = temp.firstElementChild;
    container.appendChild(turnEl);
    wireTurnEvents(cardId, turnEl);
    updateCardPreview(cardId);
    return turnIndex;
  }

  // ── Remove a turn from a card ──
  function removeTurnFromCard(cardId, turnIndex) {
    var card = promptCards[cardId];
    if (!card || card.turns.length <= 1) return;
    // Revoke object URLs
    var removed = card.turns.splice(turnIndex, 1)[0];
    if (removed) removed._uploadedImages.forEach(function (img) {
      if (img.objectUrl) URL.revokeObjectURL(img.objectUrl);
    });
    // Rebuild turns DOM
    rebuildTurnsDOM(cardId);
    updateCardPreview(cardId);
  }

  // ── Rebuild the turns DOM from card.turns state ──
  function rebuildTurnsDOM(cardId) {
    var card = promptCards[cardId];
    if (!card) return;
    var cardEl = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
    if (!cardEl) return;
    var container = cardEl.querySelector(".card-turns-container");
    container.innerHTML = "";
    card.turns.forEach(function (turn, idx) {
      var turnHtml = buildTurnHtml(idx, idx > 0);
      var temp = document.createElement("div");
      temp.innerHTML = turnHtml;
      var turnEl = temp.firstElementChild;
      container.appendChild(turnEl);
      // Restore prompt text
      turnEl.querySelector(".card-turn-prompt").value = turn.text || "";
      wireTurnEvents(cardId, turnEl);
      // Restore image thumbnails
      if (turn._uploadedImages.length > 0) renderCardTurnThumbs(cardId, idx);
    });
  }

  // ── Update card preview text ──
  function updateCardPreview(cardId) {
    var card = promptCards[cardId];
    if (!card) return;
    var cardEl = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
    if (!cardEl) return;

    // Sync turn texts from DOM
    var turnEls = cardEl.querySelectorAll(".card-turn-prompt");
    for (var i = 0; i < turnEls.length && i < card.turns.length; i++) {
      card.turns[i].text = turnEls[i].value;
    }

    var preview = cardEl.querySelector(".prompt-card-preview");
    var firstText = (card.turns[0] && card.turns[0].text || "").trim();
    if (card.turns.length > 1) {
      preview.textContent = card.turns.length + " turns: " + (firstText ? (firstText.length > 60 ? firstText.substring(0, 60) + "..." : firstText) : "No prompt");
    } else {
      preview.textContent = firstText ? (firstText.length > 80 ? firstText.substring(0, 80) + "..." : firstText) : "No prompt";
    }
    preview.title = firstText || "";

    // Tags
    var tags = cardEl.querySelector(".prompt-card-tags");
    var w = cardEl.querySelector(".card-window-count").value;
    var a = cardEl.querySelector(".card-model-a").value.trim();
    var b = cardEl.querySelector(".card-model-b").value.trim();
    var parts = [w + "w"];
    if (card.turns.length > 1) parts.push(card.turns.length + "t");
    if (a) parts.push(a);
    if (b) parts.push(b);
    tags.textContent = parts.join(" \u00b7 ");
  }

  function createPromptCard(cardId) {
    if (!cardId) {
      cardId = generateCardId();
    }
    var index = nextCardIndex - 1;
    if (promptCards[cardId]) return cardId; // already exists

    var card = {
      id: cardId,
      index: index,
      running: false,
      paused: false,
      pauseTransitionPending: false,
      runStartTime: null,
      workerStartTimes: {},
      workerData: {},
      incrementalResults: {},
      runResults: null,
      turns: [{ text: "", _uploadedImages: [] }],
      settings: {
        prompt: "",
        system_prompt: "",
        combine_with_first: false,
        window_count: 4,
        submission_gap: 30,
        model_a: "",
        model_b: "",
        retain_output: "both",
        clear_cookies: false,
        incognito: false,
        simultaneous_start: false,
        zoom_pct: 100,
      },
    };

    promptCards[cardId] = card;

    var el = document.createElement("div");
    el.className = "prompt-card";
    el.setAttribute("data-card-id", cardId);
    el.innerHTML =
      // ── Header (always visible: compact single row) ──
      '<div class="prompt-card-header">' +
        '<span class="prompt-card-index">#' + index + '</span>' +
        '<span class="prompt-card-preview">No prompt</span>' +
        '<span class="prompt-card-tags"></span>' +
        '<span class="prompt-card-status">Idle</span>' +
        '<div class="prompt-card-actions">' +
          '<button class="btn-card-run" data-card-id="' + cardId + '" title="Run">&#9654;</button>' +
          '<button class="btn-card-stop" data-card-id="' + cardId + '" title="Stop" disabled>&#9632;</button>' +
          '<button class="btn-card-collapse" data-card-id="' + cardId + '" title="Expand">&#9660;</button>' +
        '</div>' +
      '</div>' +
      // ── Body (hidden by default, shown on expand) ──
      '<div class="prompt-card-body collapsed">' +
        '<div class="card-turns-container">' +
          buildTurnHtml(0, false) +
        '</div>' +
        '<button class="btn-add-turn" title="Add another turn to this conversation">+ Add Turn</button>' +
        '<div class="card-settings-row">' +
          '<label>Win <input type="number" class="card-window-count" value="4" min="1" max="12" /></label>' +
          '<label>Gap <input type="number" class="card-gap" value="30" min="5" max="300" /></label>' +
          '<label>A <input type="text" class="card-model-a" placeholder="any" /></label>' +
          '<label>B <input type="text" class="card-model-b" placeholder="any" /></label>' +
          '<label>Retain <select class="card-retain"><option value="both">Both</option><option value="model_a">A</option><option value="model_b">B</option></select></label>' +
          '<label>Zoom <input type="number" class="card-zoom" value="100" min="25" max="200" step="5" /></label>' +
          '<label class="card-chk"><input type="checkbox" class="card-clear-cookies" /> Cookies</label>' +
          '<label class="card-chk"><input type="checkbox" class="card-incognito" /> Incog</label>' +
          '<label class="card-chk"><input type="checkbox" class="card-simultaneous" /> Simul</label>' +
        '</div>' +
        '<div class="card-progress hidden">' +
          '<span class="card-eta">ETA: &mdash;</span>' +
          '<div class="progress-track"><div class="progress-fill card-progress-fill"></div></div>' +
          '<span class="card-progress-pct">0%</span>' +
        '</div>' +
      '</div>';

    var container = document.getElementById("instruction-cards-container");
    if (container) container.appendChild(el);

    // Wire turn 0 events
    var turnEl = el.querySelector(".card-turn");
    if (turnEl) wireTurnEvents(cardId, turnEl);

    // Add Turn button
    el.querySelector(".btn-add-turn").addEventListener("click", function (e) {
      e.stopPropagation();
      addTurnToCard(cardId);
    });

    // ── Event listeners ──
    el.querySelector(".btn-card-run").addEventListener("click", function (e) {
      e.stopPropagation();
      var c = promptCards[cardId];
      if (c.running) {
        if (c.pauseTransitionPending) return;
        c.pauseTransitionPending = true;
        updateCardRunState(cardId);
        send({ type: c.paused ? "resume_run" : "pause_run", run_id: cardId });
      } else {
        startCardRun(cardId);
      }
    });

    el.querySelector(".btn-card-stop").addEventListener("click", function (e) {
      e.stopPropagation();
      stopCardRun(cardId);
    });

    // Toggle expand/collapse
    function toggleBody() {
      var body = el.querySelector(".prompt-card-body");
      var btn = el.querySelector(".btn-card-collapse");
      body.classList.toggle("collapsed");
      btn.innerHTML = body.classList.contains("collapsed") ? "&#9660;" : "&#9650;";
    }

    el.querySelector(".btn-card-collapse").addEventListener("click", function (e) {
      e.stopPropagation();
      toggleBody();
    });

    // Click header to toggle too
    el.querySelector(".prompt-card-header").addEventListener("click", toggleBody);

    // Sync preview on settings changes
    el.querySelector(".card-window-count").addEventListener("input", function () { updateCardPreview(cardId); });
    el.querySelector(".card-model-a").addEventListener("input", function () { updateCardPreview(cardId); });
    el.querySelector(".card-model-b").addEventListener("input", function () { updateCardPreview(cardId); });

    // Init preview
    updateCardPreview(cardId);

    return cardId;
  }

  function startCardRun(cardId, totalWindows, tileOffset, layoutGroupId) {
    var card = promptCards[cardId];
    if (!card || card.running) return;

    var el = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');

    // Sync turn texts from DOM
    var turnEls = el.querySelectorAll(".card-turn-prompt");
    for (var i = 0; i < turnEls.length && i < card.turns.length; i++) {
      card.turns[i].text = turnEls[i].value;
    }

    // Validate: at least the first turn must have text
    var firstText = (card.turns[0] && card.turns[0].text || "").trim();
    if (!firstText) {
      var firstPrompt = el.querySelector(".card-turn-prompt");
      if (firstPrompt) firstPrompt.focus();
      return;
    }

    card.running = true;
    card.paused = false;
    card.pauseTransitionPending = false;
    card.runStartTime = Date.now();
    card.workerStartTimes = {};
    card.workerData = {};
    card.incrementalResults = {};
    card.runResults = null;

    // Read per-card settings
    var windowCount = parseInt(el.querySelector(".card-window-count").value, 10) || 4;
    var gap = parseFloat(el.querySelector(".card-gap").value) || null;
    var modelA = el.querySelector(".card-model-a").value.trim() || null;
    var modelB = el.querySelector(".card-model-b").value.trim() || null;
    var retain = el.querySelector(".card-retain").value;
    var zoom = parseInt(el.querySelector(".card-zoom").value, 10) || 100;
    var clearCookies = el.querySelector(".card-clear-cookies").checked;
    var incognito = el.querySelector(".card-incognito").checked;
    var simultaneous = el.querySelector(".card-simultaneous").checked;

    // Read global settings
    var monW = parseInt(monitorWidthInput.value, 10) || screen.availWidth || 1920;
    var monH = parseInt(monitorHeightInput.value, 10) || screen.availHeight || 1080;

    // Build message
    var msg = {
      type: "start_run",
      run_id: cardId,
      prompt: firstText,
      system_prompt: "",
      combine_with_first: false,
      window_count: windowCount,
      submission_gap_seconds: gap,
      prompts_per_session: parseInt(promptsPerSessionInput.value, 10) || 1,
      model_a: modelA,
      model_b: modelB,
      retain_output: retain,
      clear_cookies: clearCookies,
      incognito: incognito,
      minimized: minimizedModeInput.checked,
      headless: headlessModeInput.checked,
      simultaneous_start: simultaneous,
      zoom_pct: zoom,
      start_monitor: parseInt(startMonitorInput.value, 10) || 1,
      monitor_count: parseInt(monitorCountInput.value, 10) || 1,
      monitor_width: monW,
      monitor_height: monH,
      taskbar_height: parseInt(taskbarHeightInput.value, 10) || 0,
      margin: parseInt(tileMarginInput.value, 10) || 0,
      proxies: parseProxyList(proxyListInput.value),
      proxy_on_challenge: proxyOnChallengeInput.checked,
      windows_per_proxy: parseInt(windowsPerProxyInput.value, 10) || 4,
    };

    // Pre-computed tiling for parallel instruction runs
    if (layoutGroupId) msg.layout_group_id = layoutGroupId;
    if (totalWindows != null) msg.total_windows = totalWindows;
    if (tileOffset != null) msg.tile_offset = tileOffset;

    // Build turns array
    var nonEmptyTurns = card.turns.filter(function (t) { return (t.text || "").trim(); });
    if (nonEmptyTurns.length > 1) {
      // Multi-turn: send turns array
      msg.turns = nonEmptyTurns.map(function (t) {
        var entry = { text: t.text.trim() };
        if (t._uploadedImages && t._uploadedImages.length > 0) {
          entry.images = t._uploadedImages.map(function (img) {
            return { data: img.data, mime_type: img.mime_type, filename: img.filename };
          });
        }
        return entry;
      });
    } else {
      // Single turn: send prompt + images (backward compat)
      var firstTurn = nonEmptyTurns[0] || card.turns[0];
      if (firstTurn._uploadedImages && firstTurn._uploadedImages.length > 0) {
        msg.images = firstTurn._uploadedImages.map(function (img) {
          return { data: img.data, mime_type: img.mime_type, filename: img.filename };
        });
      }
    }

    // Update UI
    updateCardRunState(cardId);
    send(msg);

    var turnInfo = nonEmptyTurns.length > 1 ? " (" + nonEmptyTurns.length + " turns)" : "";
    appendLog("info", "[Prompt #" + card.index + "] Starting run with " + windowCount + " window(s)" + turnInfo + "...");
  }

  function stopCardRun(cardId) {
    send({ type: "stop_run", run_id: cardId });
    var card = promptCards[cardId];
    appendLog("warning", "[Prompt #" + (card ? card.index : "?") + "] Stop requested...");
  }

  function removePromptCard(cardId) {
    var card = promptCards[cardId];
    if (card && card.running) {
      stopCardRun(cardId);
    }
    delete promptCards[cardId];
    var el = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
    if (el) el.remove();
    // Ensure at least one card exists
    if (Object.keys(promptCards).length === 0) {
      createPromptCard();
    }
  }

  function getCardImages(cardId) {
    var card = promptCards[cardId];
    if (!card) return [];
    // Return images from the first turn (backward compat)
    return card.turns && card.turns[0] ? card.turns[0]._uploadedImages : [];
  }

  function updateCardRunState(cardId) {
    var card = promptCards[cardId];
    var el = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
    if (!el || !card) return;

    var statusEl = el.querySelector(".prompt-card-status");
    var runBtn = el.querySelector(".btn-card-run");
    var stopBtn2 = el.querySelector(".btn-card-stop");
    var progressEl = el.querySelector(".card-progress");

    el.classList.remove("running", "completed", "error");

    if (card.running) {
      el.classList.add("running");
      statusEl.textContent = card.paused ? "Paused" : "Running";
      statusEl.className = "prompt-card-status status-running";
      runBtn.innerHTML = card.paused ? "&#9654; Resume" : "&#9208; Pause";
      runBtn.disabled = card.pauseTransitionPending;
      stopBtn2.disabled = false;
      progressEl.classList.remove("hidden");
    } else {
      var hasResults = Object.keys(card.incrementalResults || {}).length > 0;
      if (hasResults) {
        el.classList.add("completed");
        statusEl.textContent = "Complete";
        statusEl.className = "prompt-card-status status-complete";
      } else {
        statusEl.textContent = "Idle";
        statusEl.className = "prompt-card-status";
      }
      runBtn.innerHTML = "&#9654; Run";
      runBtn.disabled = false;
      stopBtn2.disabled = true;
    }
  }

  // ── Card-specific message handlers ──

  function updateCardWorker(runId, msg) {
    var card = promptCards[runId];
    if (!card) return;

    // Store worker data in card
    card.workerData[msg.worker_id] = {
      state: msg.state,
      progress_pct: msg.progress_pct,
      message: msg.message || "",
      error: msg.error || null,
    };
    if (!card.workerStartTimes[msg.worker_id] && msg.state !== "idle") {
      card.workerStartTimes[msg.worker_id] = Date.now();
    }

    // Also update the global worker cards (in sidebar)
    updateWorkerCard({ ...msg, run_id: runId });
    // And the global results table
    updateResultRow({ ...msg, run_id: runId });
  }

  function onCardWorkerResult(runId, result) {
    var card = promptCards[runId];
    if (!card) return;

    card.incrementalResults[result.worker_id] = result;

    // Update card results count
    var el = document.querySelector('.prompt-card[data-card-id="' + runId + '"]');
    if (el) {
      var countEl = el.querySelector(".card-results-count");
      if (countEl) countEl.textContent = Object.keys(card.incrementalResults).length;
    }

    // Also update global results table
    onWorkerResult({ ...result, run_id: runId });
  }

  function onCardPartialResult(runId, partial) {
    var card = promptCards[runId];
    if (!card) return;

    if (!card.incrementalResults[partial.worker_id]) {
      card.incrementalResults[partial.worker_id] = { worker_id: partial.worker_id };
    }
    var ir = card.incrementalResults[partial.worker_id];
    if (partial.slide === "a") {
      ir.model_a_name = partial.model_name;
      ir.model_a_response = partial.response;
      ir.model_a_response_html = partial.response_html;
    } else {
      ir.model_b_name = partial.model_name;
      ir.model_b_response = partial.response;
      ir.model_b_response_html = partial.response_html;
    }

    // Also update global
    onWorkerPartialResult({ ...partial, run_id: runId });
  }

  function updateCardProgress(runId, msg) {
    var card = promptCards[runId];
    if (!card) return;

    var el = document.querySelector('.prompt-card[data-card-id="' + runId + '"]');
    if (!el) return;

    var pct = Math.round(msg.overall_pct);
    var fillEl = el.querySelector(".card-progress-fill");
    var pctEl = el.querySelector(".card-progress-pct");
    var etaEl = el.querySelector(".card-eta");

    if (fillEl) fillEl.style.width = pct + "%";
    if (pctEl) pctEl.textContent = pct + "%";

    if (card.paused) {
      if (etaEl) etaEl.textContent = "Paused";
    } else if (card.runStartTime && pct > 0 && pct < 100) {
      var elapsed = (Date.now() - card.runStartTime) / 1000;
      var totalEstimate = (elapsed / pct) * 100;
      var remaining = Math.max(0, totalEstimate - elapsed);
      if (etaEl) etaEl.textContent = "ETA: ~" + formatDuration(remaining);
    }

    // Also update global progress (file mode uses this)
    updateProgress(msg);
  }

  function onCardRunComplete(runId, msg) {
    var card = promptCards[runId];
    if (!card) return;

    card.running = false;
    card.paused = false;
    card.pauseTransitionPending = false;
    card.runResults = msg.results;

    var el = document.querySelector('.prompt-card[data-card-id="' + runId + '"]');
    if (el) {
      var fillEl = el.querySelector(".card-progress-fill");
      var pctEl = el.querySelector(".card-progress-pct");
      var etaEl = el.querySelector(".card-eta");
      var countEl = el.querySelector(".card-results-count");
      if (fillEl) fillEl.style.width = "100%";
      if (pctEl) pctEl.textContent = "100%";
      if (etaEl) etaEl.textContent = "Complete";
      if (countEl) countEl.textContent = msg.results.length;
    }

    updateCardRunState(runId);
    updateStopAllState();

    // Update global results
    onRunComplete(msg, { append: true });
  }

  function onCardRunCancelled(runId) {
    var card = promptCards[runId];
    if (!card) return;

    card.running = false;
    card.paused = false;
    card.pauseTransitionPending = false;

    var el = document.querySelector('.prompt-card[data-card-id="' + runId + '"]');
    if (el) {
      var etaEl = el.querySelector(".card-eta");
      if (etaEl) etaEl.textContent = "Cancelled";
    }

    updateCardRunState(runId);
    updateStopAllState();
    appendLog("warning", "[Prompt #" + card.index + "] Run cancelled");
  }

  function onCardRunPaused(runId) {
    var card = promptCards[runId];
    if (!card || !card.running) return;
    card.paused = true;
    card.pauseTransitionPending = false;
    updateCardRunState(runId);
  }

  function onCardRunResumed(runId) {
    var card = promptCards[runId];
    if (!card || !card.running) return;
    card.paused = false;
    card.pauseTransitionPending = false;
    updateCardRunState(runId);
  }

  function updateStopAllState() {
    var anyRunning = Object.values(promptCards).some(function (c) { return c.running; });
    var btn = document.getElementById("btn-stop-all");
    if (btn) btn.disabled = !anyRunning;
  }

  // ══════════════════════════════════════
  // Instruction Load Mode
  // ══════════════════════════════════════

  let instructionRunning = false;
  let instructionStopRequested = false;
  let loadedInstructions = [];
  let instructionRunQueue = [];
  let currentInstructionCardId = null;

  function handleInstructionUpload(fileOrFiles) {
    var formData = new FormData();
    // Accept a single file, FileList, or array of files
    var fileList = fileOrFiles instanceof FileList ? Array.from(fileOrFiles)
      : Array.isArray(fileOrFiles) ? fileOrFiles : [fileOrFiles];
    fileList.forEach(function (f) { formData.append("files", f); });

    fetch("/upload-instructions", { method: "POST", body: formData })
      .then(function (res) {
        if (!res.ok) {
          return res.json().then(function (err) { throw new Error(err.error || err.detail || "Upload failed"); });
        }
        return res.json();
      })
      .then(function (data) {
        if (data.error) {
          showToast(data.error, "error");
          return;
        }
        loadedInstructions = data.instructions || [];
        if (loadedInstructions.length === 0) {
          showToast("No valid instructions found in file", "warning");
          return;
        }

        // Show info
        instructionFileName.textContent = data.filename || (fileList[0] && fileList[0].name) || "instructions";
        instructionCountSpan.textContent = loadedInstructions.length + " instruction(s)";
        instructionUploadArea.classList.add("hidden");
        instructionInfoDiv.classList.remove("hidden");

        // Clear existing cards
        promptCards = {};
        nextCardIndex = 1;
        instructionCardsContainer.innerHTML = "";

        // Generate cards from instructions
        loadedInstructions.forEach(function (inst) {
          var cardId = createPromptCard();
          var card = promptCards[cardId];
          var el = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
          if (!el || !card) return;

          // Populate turns
          if (inst.turns && inst.turns.length > 0) {
            // Multi-turn instruction
            inst.turns.forEach(function (turn, idx) {
              if (idx === 0) {
                // First turn already exists
                card.turns[0].text = turn.text || "";
                el.querySelector(".card-turn-prompt").value = turn.text || "";
                // Pre-populate images from file
                if (turn.images && Array.isArray(turn.images)) {
                  turn.images.forEach(function (img) {
                    if (img.data && img.mime_type) {
                      card.turns[0]._uploadedImages.push({
                        data: img.data,
                        mime_type: img.mime_type,
                        filename: img.filename || "",
                        objectUrl: "",
                      });
                    }
                  });
                  if (card.turns[0]._uploadedImages.length > 0) renderCardTurnThumbs(cardId, 0);
                }
              } else {
                // Add subsequent turns
                var turnIdx = addTurnToCard(cardId);
                if (turnIdx >= 0) {
                  card.turns[turnIdx].text = turn.text || "";
                  var turnPrompts = el.querySelectorAll(".card-turn-prompt");
                  if (turnPrompts[turnIdx]) turnPrompts[turnIdx].value = turn.text || "";
                  // Pre-populate images
                  if (turn.images && Array.isArray(turn.images)) {
                    turn.images.forEach(function (img) {
                      if (img.data && img.mime_type) {
                        card.turns[turnIdx]._uploadedImages.push({
                          data: img.data,
                          mime_type: img.mime_type,
                          filename: img.filename || "",
                          objectUrl: "",
                        });
                      }
                    });
                    if (card.turns[turnIdx]._uploadedImages.length > 0) renderCardTurnThumbs(cardId, turnIdx);
                  }
                }
              }
            });
          } else if (inst.prompt) {
            // Single-turn (backward compat)
            card.turns[0].text = inst.prompt;
            el.querySelector(".card-turn-prompt").value = inst.prompt;
            // Pre-populate images for single-turn
            if (inst.images && Array.isArray(inst.images)) {
              inst.images.forEach(function (img) {
                if (img && img.data && img.mime_type) {
                  card.turns[0]._uploadedImages.push({
                    data: img.data,
                    mime_type: img.mime_type,
                    filename: img.filename || "",
                    objectUrl: "",
                  });
                }
              });
              if (card.turns[0]._uploadedImages.length > 0) renderCardTurnThumbs(cardId, 0);
            }
          }

          // Populate settings
          if (inst.window_count) el.querySelector(".card-window-count").value = inst.window_count;
          if (inst.submission_gap_seconds) el.querySelector(".card-gap").value = inst.submission_gap_seconds;
          if (inst.model_a) el.querySelector(".card-model-a").value = inst.model_a;
          if (inst.model_b) el.querySelector(".card-model-b").value = inst.model_b;
          if (inst.retain_output) el.querySelector(".card-retain").value = inst.retain_output;
          if (inst.zoom_pct) el.querySelector(".card-zoom").value = inst.zoom_pct;
          if (inst.clear_cookies !== undefined) el.querySelector(".card-clear-cookies").checked = !!inst.clear_cookies;
          if (inst.incognito !== undefined) el.querySelector(".card-incognito").checked = !!inst.incognito;
          if (inst.simultaneous_start !== undefined) el.querySelector(".card-simultaneous").checked = !!inst.simultaneous_start;

          updateCardPreview(cardId);
        });

        updateInstructionOverallProgress();
        appendLog("info", "Loaded " + loadedInstructions.length + " instruction(s) from " + (data.filename || "file"));
      })
      .catch(function (err) {
        showToast("Failed to parse instruction file: " + err.message, "error");
      });
  }

  function clearInstructions() {
    loadedInstructions = [];
    instructionRunQueue = [];
    currentInstructionCardId = null;
    instructionRunning = false;
    instructionStopRequested = false;
    promptCards = {};
    nextCardIndex = 1;
    if (instructionCardsContainer) instructionCardsContainer.innerHTML = "";
    if (instructionUploadArea) instructionUploadArea.classList.remove("hidden");
    if (instructionInfoDiv) instructionInfoDiv.classList.add("hidden");
    updateInstructionOverallProgress();
  }

  function startInstructionSequence() {
    if (instructionRunning) return;
    instructionRunning = true;
    instructionStopRequested = false;

    // Build queue of card IDs that are not yet completed
    instructionRunQueue = Object.keys(promptCards).filter(function (cid) {
      var c = promptCards[cid];
      return !c.running && Object.keys(c.incrementalResults || {}).length === 0;
    });

    if (instructionRunQueue.length === 0) {
      // All done or no cards
      instructionRunning = false;
      return;
    }

    instructionRunBtn.disabled = true;
    instructionStopBtn.disabled = false;

    // Calculate total windows and per-card tile offsets for non-overlapping placement.
    var totalWindows = 0;
    var cardOffsets = {};
    var layoutGroupId = "instruction-layout-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 8);
    instructionRunQueue.forEach(function (cardId) {
      var el = document.querySelector('.prompt-card[data-card-id="' + cardId + '"]');
      var wc = parseInt(el.querySelector(".card-window-count").value, 10) || 4;
      cardOffsets[cardId] = totalWindows;
      totalWindows += wc;
    });

    // Start all pending instruction cards in parallel with pre-computed tiling.
    instructionRunQueue.forEach(function (cardId) {
      startCardRun(cardId, totalWindows, cardOffsets[cardId], layoutGroupId);
    });
    currentInstructionCardId = null;
    updateInstructionOverallProgress();
  }

  function runNextInstruction() {
    var anyRunning = Object.values(promptCards).some(function (c) {
      return c.running;
    });
    if (anyRunning) {
      updateInstructionOverallProgress();
      return;
    }
    onInstructionSequenceComplete();
  }

  function stopInstructionSequence() {
    instructionStopRequested = true;
    Object.keys(promptCards).forEach(function (cid) {
      if (promptCards[cid] && promptCards[cid].running) {
        stopCardRun(cid);
      }
    });
  }

  function onInstructionSequenceComplete() {
    instructionRunning = false;
    instructionStopRequested = false;
    currentInstructionCardId = null;
    instructionRunBtn.disabled = false;
    instructionStopBtn.disabled = true;
    updateInstructionOverallProgress();
    appendLog("info", "Instruction sequence complete");
  }

  function updateInstructionOverallProgress() {
    var total = Object.keys(promptCards).length;
    var done = 0;
    Object.values(promptCards).forEach(function (c) {
      if (!c.running && Object.keys(c.incrementalResults || {}).length > 0) done++;
    });
    var pct = total > 0 ? Math.round((done / total) * 100) : 0;
    if (instructionEta) instructionEta.textContent = done + " / " + total;
    if (instructionProgressFill) instructionProgressFill.style.width = pct + "%";
    if (instructionProgressPct) instructionProgressPct.textContent = pct + "%";
  }

  // Wire instruction upload area
  if (instructionUploadArea) {
    instructionUploadArea.addEventListener("click", function () {
      if (instructionFileInput) instructionFileInput.click();
    });
    instructionUploadArea.addEventListener("dragover", function (e) {
      e.preventDefault();
      instructionUploadArea.classList.add("dragover");
    });
    instructionUploadArea.addEventListener("dragleave", function () {
      instructionUploadArea.classList.remove("dragover");
    });
    instructionUploadArea.addEventListener("drop", function (e) {
      e.preventDefault();
      instructionUploadArea.classList.remove("dragover");
      if (e.dataTransfer.files.length > 0) handleInstructionUpload(e.dataTransfer.files);
    });
  }
  if (instructionFileInput) {
    instructionFileInput.addEventListener("change", function () {
      if (instructionFileInput.files.length > 0) handleInstructionUpload(instructionFileInput.files);
      instructionFileInput.value = "";
    });
  }
  if (removeInstructionsBtn) {
    removeInstructionsBtn.addEventListener("click", clearInstructions);
  }
  if (instructionRunBtn) {
    instructionRunBtn.addEventListener("click", startInstructionSequence);
  }
  if (instructionStopBtn) {
    instructionStopBtn.addEventListener("click", stopInstructionSequence);
  }

  // ══════════════════════════════════════
  // Settings Persistence (localStorage)
  // ══════════════════════════════════════

  const STORAGE_KEY = "lmarena_settings";

  const settingsFields = [
    { el: windowCountInput,   key: "window_count" },
    { el: submissionGapInput, key: "submission_gap" },
    { el: promptsPerSessionInput, key: "prompts_per_session" },
    { el: arenaUrlInput,      key: "arena_url" },
    { el: modelAInput,        key: "model_a" },
    { el: modelBInput,        key: "model_b" },
    { el: retainOutputInput,  key: "retain_output" },
    { el: zoomInput,          key: "zoom" },
    { el: clearCookiesInput,  key: "clear_cookies", checkbox: true },
    { el: incognitoModeInput, key: "incognito_mode", checkbox: true },
    { el: minimizedModeInput, key: "minimized_mode", checkbox: true },
    { el: headlessModeInput, key: "headless_mode", checkbox: true },
    { el: simultaneousStartInput, key: "simultaneous_start", checkbox: true },
    { el: startMonitorInput,  key: "start_monitor" },
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
    const startMonitor = parseInt(startMonitorInput.value, 10) || 1;
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
      `Start monitor ${startMonitor} \u2014 ${cols}\u00d7${rows} grid \u2014 each window ${winW}\u00d7${winH}px` +
      (monitors > 1 ? ` across ${totalW}\u00d7${totalH} total` : "");
  }

  [windowCountInput, startMonitorInput, monitorCountInput, monitorWidthInput,
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
        activeRunId = state.run_id || null;
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
            ensureWorkerCard(w.worker_id, state.run_id);
            updateWorkerCard({
              worker_id: w.worker_id,
              run_id: state.run_id,
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
            ensureResultRow(r.worker_id, state.run_id || null);
            r.run_id = r.run_id || state.run_id || null;
            updateResultRowWithData(r);
            incrementalResults[getResultKey(r.worker_id, r.run_id || null)] = r;
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
  // Live Preview — Screenshot Handler
  // ══════════════════════════════════════

  function syncPreviewOpenButton(button, shot) {
    if (!button) return;
    if (shot) {
      button.dataset.runId = shot.run_id || "";
      button.dataset.workerId = String(shot.worker_index);
    }
    const headless = !!headlessModeInput.checked;
    button.classList.toggle("headless", headless);
    button.setAttribute("aria-disabled", headless ? "true" : "false");
    button.title = headless
      ? "Original window is unavailable in headless mode"
      : "Open original window";
  }

  function syncAllPreviewOpenButtons() {
    previewGrid.querySelectorAll(".preview-open-btn").forEach(function (button) {
      syncPreviewOpenButton(button);
    });
  }

  function onPreviewScreenshots(screenshots) {
    if (!screenshots.length) {
      if (!previewGrid.children.length) {
        previewGrid.innerHTML =
          '<div class="preview-empty">' +
          '<div class="preview-empty-icon">&#128247;</div>' +
          '<span>No windows active</span>' +
          '<span>Start a run to see live previews</span>' +
          '</div>';
      }
      previewStatus.textContent = "No windows active";
      return;
    }

    previewStatus.textContent = screenshots.length + " window" + (screenshots.length !== 1 ? "s" : "") + " active";

    // Build a set of current keys to detect removed cards
    const activeKeys = new Set();

    screenshots.forEach(function (shot) {
      const key = shot.run_id + "_w" + shot.worker_index;
      activeKeys.add(key);

      let card = previewGrid.querySelector('[data-preview-key="' + key + '"]');
      if (!card) {
        card = createPreviewCard(key, shot);
        previewGrid.appendChild(card);
      }

      // Update screenshot image
      const img = card.querySelector(".preview-screenshot-img");
      if (img) {
        img.src = "data:image/jpeg;base64," + shot.data;
      }

      const openBtn = card.querySelector(".preview-open-btn");
      syncPreviewOpenButton(openBtn, shot);

      // Update state badge from workerData
      const wKey = getWorkerKey(shot.worker_index, shot.run_id || null);
      const wd = workerData[wKey] || workerData[getWorkerKey(shot.worker_index, null)];
      if (wd) {
        const stateBadge = card.querySelector(".preview-card-state");
        if (stateBadge) {
          const st = (wd.state || "idle").toLowerCase();
          stateBadge.textContent = st;
          stateBadge.className = "preview-card-state st-" + st.replace(/[^a-z]/g, "");
        }
        const proxyEl = card.querySelector(".preview-card-proxy");
        if (proxyEl && wd.proxy) {
          proxyEl.textContent = wd.proxy;
        }
      }

      // Update timestamp
      const timeEl = card.querySelector(".preview-card-time");
      if (timeEl && shot.timestamp) {
        const d = new Date(shot.timestamp * 1000);
        timeEl.textContent = d.toLocaleTimeString();
      }
    });

    // Remove cards for windows that are no longer active
    Array.from(previewGrid.querySelectorAll(".preview-card")).forEach(function (card) {
      if (!activeKeys.has(card.dataset.previewKey)) {
        card.remove();
      }
    });

    // Remove empty placeholder if we have real cards
    const emptyEl = previewGrid.querySelector(".preview-empty");
    if (emptyEl && screenshots.length > 0) {
      emptyEl.remove();
    }
  }

  function createPreviewCard(key, shot) {
    const card = document.createElement("div");
    card.className = "preview-card";
    card.dataset.previewKey = key;
    card.innerHTML =
      '<div class="preview-card-header">' +
      '  <div class="preview-card-title-row">' +
      '    <span class="preview-card-title">Window ' + (shot.worker_index + 1) + '</span>' +
      '    <button class="preview-open-btn" type="button" title="Open original window">&#9974;</button>' +
      '  </div>' +
      '  <span class="preview-card-state st-idle">idle</span>' +
      '</div>' +
      '<div class="preview-card-screenshot">' +
      '  <img class="preview-screenshot-img" src="" alt="Preview" />' +
      '</div>' +
      '<div class="preview-card-footer">' +
      '  <span class="preview-card-proxy"></span>' +
      '  <span class="preview-card-time"></span>' +
      '</div>';

    const openBtn = card.querySelector(".preview-open-btn");
    syncPreviewOpenButton(openBtn, shot);
    if (openBtn) {
      openBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        const workerId = parseInt(openBtn.dataset.workerId, 10);
        if (!Number.isFinite(workerId)) {
          showToast("Window is not ready yet", "warning");
          return;
        }
        if (headlessModeInput.checked) {
          showToast("Headless mode is active, so no original window is available", "warning");
          return;
        }

        const qs = new URLSearchParams({ worker_id: String(workerId) });
        const runId = openBtn.dataset.runId || "";
        if (runId) qs.set("run_id", runId);

        fetch("/api/preview/open-window?" + qs.toString(), { method: "POST" })
          .then(function (res) { return res.json(); })
          .then(function (data) {
            if (data && data.ok) {
              showToast(data.maximized ? "Original window opened" : "Window focused", "info");
              return;
            }
            showToast((data && data.message) || "Failed to open original window", data && data.reason === "headless" ? "warning" : "error");
          })
          .catch(function () {
            showToast("Failed to open original window", "error");
          });
      });
    }

    // Click to expand
    card.addEventListener("click", function (event) {
      if (event.target.closest(".preview-open-btn")) return;
      const img = card.querySelector(".preview-screenshot-img");
      if (img && img.src && !img.src.endsWith("/")) {
        openPreviewExpandModal(img.src);
      }
    });

    return card;
  }

  function openPreviewExpandModal(imageSrc) {
    const overlay = document.createElement("div");
    overlay.className = "preview-expand-modal";
    overlay.innerHTML = '<img src="' + escapeAttr(imageSrc) + '" />';
    overlay.addEventListener("click", function () {
      overlay.remove();
    });
    document.body.appendChild(overlay);
  }

  // Re-subscribe on reconnect if the preview tab is active
  function resubscribePreview() {
    if (previewSubscribed) {
      previewSubscribed = false;
      if (currentMainTab === "preview") {
        send({ type: "subscribe_preview" });
        previewSubscribed = true;
      }
    }
  }

  // ══════════════════════════════════════
  // Headless Mode Toggle
  // ══════════════════════════════════════

  headlessModeInput.addEventListener("change", function () {
    const enabled = headlessModeInput.checked;
    fetch("/api/toggle-headless?enabled=" + enabled, { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        syncAllPreviewOpenButtons();
        showToast("Headless mode " + (data.headless ? "enabled" : "disabled"), "info");
      })
      .catch(function () {
        showToast("Failed to toggle headless mode", "error");
      });
  });

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
  syncAllPreviewOpenButtons();

  // Auto-expand system prompt if it has saved content
  if (systemPromptInput.value.trim()) {
    document.getElementById("system-prompt-details").open = true;
  }

  // Restore file upload state if present
  restoreFileState();

  connect();
})();
