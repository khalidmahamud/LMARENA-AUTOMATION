const STORAGE_KEY = "managedZoomPct";
const MIN_ZOOM = 25;
const MAX_ZOOM = 500;
const ARENA_HOSTS = new Set(["arena.ai", "www.arena.ai"]);
const ZOOM_EPSILON = 0.001;

const lastAppliedZoomByTab = new Map();
const inFlightZoomByTab = new Map();

function normalizeZoomPct(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 100;
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.round(parsed)));
}

function isArenaUrl(urlString) {
  if (!urlString) return false;
  try {
    const url = new URL(urlString);
    return (url.protocol === "http:" || url.protocol === "https:") && ARENA_HOSTS.has(url.hostname);
  } catch {
    return false;
  }
}

async function getManagedZoomPct() {
  const stored = await chrome.storage.local.get(STORAGE_KEY);
  return normalizeZoomPct(stored[STORAGE_KEY] ?? 100);
}

async function setManagedZoomPct(zoomPct) {
  const normalized = normalizeZoomPct(zoomPct);
  await chrome.storage.local.set({ [STORAGE_KEY]: normalized });
  return normalized;
}

async function applyZoomToTab(tabId, url) {
  if (!tabId || tabId < 0 || !isArenaUrl(url)) return false;
  if (inFlightZoomByTab.has(tabId)) {
    return inFlightZoomByTab.get(tabId);
  }

  const task = (async () => {
    const zoomPct = await getManagedZoomPct();
    const targetZoom = zoomPct / 100;
    const lastApplied = lastAppliedZoomByTab.get(tabId);
    if (typeof lastApplied === "number" && Math.abs(lastApplied - targetZoom) < ZOOM_EPSILON) {
      return false;
    }

    const currentZoom = await chrome.tabs.getZoom(tabId);
    if (Math.abs(currentZoom - targetZoom) < ZOOM_EPSILON) {
      lastAppliedZoomByTab.set(tabId, currentZoom);
      return false;
    }

    await chrome.tabs.setZoomSettings(tabId, {
      mode: "automatic",
      scope: "per-origin"
    });
    await chrome.tabs.setZoom(tabId, targetZoom);
    lastAppliedZoomByTab.set(tabId, targetZoom);
    return true;
  })();

  inFlightZoomByTab.set(tabId, task);
  try {
    return await task;
  } finally {
    if (inFlightZoomByTab.get(tabId) === task) {
      inFlightZoomByTab.delete(tabId);
    }
  }
}

async function applyZoomToAllTabs() {
  const tabs = await chrome.tabs.query({});
  let applied = 0;
  for (const tab of tabs) {
    try {
      if (await applyZoomToTab(tab.id, tab.url)) {
        applied += 1;
      }
    } catch (error) {
      console.warn("Failed to apply browser zoom", tab.id, error);
    }
  }
  return applied;
}

async function getArenaZoomState() {
  const zoomPct = await getManagedZoomPct();
  const tabs = await chrome.tabs.query({});
  const arenaTabs = [];

  for (const tab of tabs) {
    if (!tab || !tab.id || tab.id < 0 || !isArenaUrl(tab.url)) continue;
    let zoom = null;
    try {
      zoom = await chrome.tabs.getZoom(tab.id);
    } catch (error) {
      console.warn("Failed to inspect browser zoom", tab.id, error);
    }

    arenaTabs.push({
      tabId: tab.id,
      url: tab.url,
      zoom,
      lastAppliedZoom: lastAppliedZoomByTab.get(tab.id) ?? null
    });
  }

  return { ok: true, zoomPct, arenaTabs };
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ [STORAGE_KEY]: 100 });
});

chrome.runtime.onStartup.addListener(() => {
  applyZoomToAllTabs().catch((error) => console.warn("Startup zoom sync failed", error));
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  const url = tab.url || changeInfo.url;
  if (!url) return;
  applyZoomToTab(tabId, url).catch((error) => console.warn("Tab update zoom failed", tabId, error));
});

chrome.tabs.onRemoved.addListener((tabId) => {
  lastAppliedZoomByTab.delete(tabId);
  inFlightZoomByTab.delete(tabId);
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message !== "object") {
    return false;
  }

  if (message.type === "set-managed-zoom") {
    (async () => {
      const zoomPct = await setManagedZoomPct(message.zoomPct);
      const appliedTabs = await applyZoomToAllTabs();
      sendResponse({ ok: true, zoomPct, appliedTabs });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message.type === "get-managed-zoom") {
    (async () => {
      const zoomPct = await getManagedZoomPct();
      sendResponse({ ok: true, zoomPct });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  return false;
});

globalThis.configureManagedZoom = async (zoomPct) => {
  const normalized = await setManagedZoomPct(zoomPct);
  const appliedTabs = await applyZoomToAllTabs();
  const incognitoAllowed = await chrome.extension.isAllowedIncognitoAccess();
  const state = await getArenaZoomState();
  return {
    ok: true,
    zoomPct: normalized,
    appliedTabs,
    incognitoAllowed,
    arenaTabs: state.arenaTabs
  };
};

globalThis.getManagedZoomState = async () => {
  return await getArenaZoomState();
};
