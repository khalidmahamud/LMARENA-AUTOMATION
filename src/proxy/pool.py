"""Health-aware, persistent, auto-refreshing proxy pool."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

PROXIFLY_URL = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main"
    "/proxies/protocols/{protocol}/data.json"
)

MAX_FAIL_COUNT = 3
STALE_THRESHOLD = 3600.0  # prune auto-fetched unhealthy proxies after 1 hour


@dataclass
class ProxyEntry:
    server: str
    username: Optional[str] = None
    password: Optional[str] = None
    healthy: bool = True
    last_checked: Optional[float] = None
    fail_count: int = 0
    source: str = "manual"

    def to_playwright_dict(self) -> dict:
        """Return a Playwright-compatible proxy dict."""
        d: dict = {"server": self.server}
        if self.username:
            d["username"] = self.username
        if self.password:
            d["password"] = self.password
        return d


class ProxyPool:
    """Central proxy pool with health tracking, persistence, and auto-refresh."""

    PERSIST_PATH = Path("data/proxy_pool.json")

    def __init__(
        self,
        check_fn: Optional[Callable[[str, int], bool]] = None,
        max_healthy: int = 50,
    ) -> None:
        self._entries: Dict[str, ProxyEntry] = {}
        self._healthy_index: int = 0
        self._lock = asyncio.Lock()
        self._check_fn = check_fn
        self._max_healthy = max_healthy
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_refresh: Optional[str] = None
        # Persisted auto-refresh settings
        self._auto_refresh_protocol: str = "http"
        self._auto_refresh_enabled: bool = False

    # ── Core API ──

    def set_max_healthy(self, limit: int) -> None:
        """Update the max healthy proxies cap and trim excess."""
        self._max_healthy = max(1, limit)
        # Trim excess healthy proxies (remove auto_fetch first, then manual)
        healthy = sorted(
            [e for e in self._entries.values() if e.healthy],
            key=lambda e: (0 if e.source == "manual" else 1),
        )
        excess = healthy[self._max_healthy:]
        for entry in excess:
            del self._entries[entry.server]
        if excess:
            logger.info("Trimmed %d excess proxies (new max: %d)", len(excess), self._max_healthy)
        logger.info("Max healthy proxies set to %d", self._max_healthy)

    def add_proxies(self, proxies: List[dict], source: str = "manual") -> int:
        """Merge proxies into pool. Returns count of newly added."""
        added = 0
        for p in proxies:
            if self.healthy_count >= self._max_healthy:
                logger.debug("Pool at max healthy cap (%d), skipping remaining", self._max_healthy)
                break
            server = p.get("server", "")
            if not server:
                continue
            if server in self._entries:
                continue
            self._entries[server] = ProxyEntry(
                server=server,
                username=p.get("username"),
                password=p.get("password"),
                source=source,
            )
            added += 1
        if added:
            logger.info("Added %d new proxies to pool (total: %d, max: %d)", added, len(self._entries), self._max_healthy)
        return added

    def get_next_healthy(self) -> Optional[dict]:
        """Round-robin through healthy proxies. Returns Playwright-compatible dict or None."""
        healthy = [e for e in self._entries.values() if e.healthy]
        if not healthy:
            return None
        idx = self._healthy_index % len(healthy)
        self._healthy_index += 1
        return healthy[idx].to_playwright_dict()

    def mark_unhealthy(self, server: str) -> None:
        """Increment fail_count; mark unhealthy after MAX_FAIL_COUNT consecutive failures."""
        entry = self._entries.get(server)
        if not entry:
            return
        entry.fail_count += 1
        entry.last_checked = time.time()
        if entry.fail_count >= MAX_FAIL_COUNT:
            entry.healthy = False
            logger.info("Proxy %s marked unhealthy (fail_count=%d)", server, entry.fail_count)

    def mark_healthy(self, server: str) -> None:
        """Reset fail_count and mark healthy."""
        entry = self._entries.get(server)
        if not entry:
            return
        entry.fail_count = 0
        entry.healthy = True
        entry.last_checked = time.time()

    def remove_proxy(self, server: str) -> None:
        """Remove a proxy entirely."""
        self._entries.pop(server, None)

    # ── Persistence ──

    def save_to_file(self, path: Optional[Path] = None) -> Path:
        """Serialize pool to JSON. Caps saved healthy proxies to max_healthy."""
        path = path or self.PERSIST_PATH
        path.parent.mkdir(parents=True, exist_ok=True)

        # Separate healthy and unhealthy; prioritize manual over auto_fetch
        healthy = sorted(
            [e for e in self._entries.values() if e.healthy],
            key=lambda e: (0 if e.source == "manual" else 1),
        )
        unhealthy = [e for e in self._entries.values() if not e.healthy]
        capped_healthy = healthy[: self._max_healthy]

        data = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "max_healthy": self._max_healthy,
            "auto_refresh_enabled": self._auto_refresh_enabled,
            "auto_refresh_protocol": self._auto_refresh_protocol,
            "proxies": [asdict(e) for e in capped_healthy + unhealthy],
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info("Proxy pool saved to %s (%d proxies)", path, len(capped_healthy) + len(unhealthy))
        return path

    def load_from_file(self, path: Optional[Path] = None) -> int:
        """Load proxies and settings from JSON. Returns count loaded."""
        path = path or self.PERSIST_PATH
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text())

            # Restore settings
            if "max_healthy" in data:
                self._max_healthy = max(1, data["max_healthy"])
            if "auto_refresh_enabled" in data:
                self._auto_refresh_enabled = data["auto_refresh_enabled"]
            if "auto_refresh_protocol" in data:
                self._auto_refresh_protocol = data["auto_refresh_protocol"]

            loaded = 0
            for p in data.get("proxies", []):
                server = p.get("server", "")
                if not server or server in self._entries:
                    continue
                self._entries[server] = ProxyEntry(
                    server=server,
                    username=p.get("username"),
                    password=p.get("password"),
                    healthy=p.get("healthy", True),
                    last_checked=p.get("last_checked"),
                    fail_count=p.get("fail_count", 0),
                    source=p.get("source", "manual"),
                )
                loaded += 1
            logger.info("Loaded %d proxies from %s (total: %d, max: %d)", loaded, path, len(self._entries), self._max_healthy)
            return loaded
        except Exception as exc:
            logger.error("Failed to load proxy pool from %s: %s", path, exc)
            return 0

    # ── Background Auto-Refresh ──

    async def start_auto_refresh(
        self,
        protocol: str = "http",
        fetch_limit: int = 20,
        interval: float = 300.0,
    ) -> None:
        """Start background task that periodically fetches and health-checks proxies."""
        await self.stop_auto_refresh()
        self._running = True
        self._auto_refresh_enabled = True
        self._auto_refresh_protocol = protocol
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(protocol, fetch_limit, interval)
        )
        logger.info("Auto-refresh started (protocol=%s, interval=%.0fs)", protocol, interval)

    async def stop_auto_refresh(self) -> None:
        """Cancel the background refresh task."""
        self._running = False
        self._auto_refresh_enabled = False
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None

    async def _refresh_loop(
        self, protocol: str, fetch_limit: int, interval: float
    ) -> None:
        while self._running:
            try:
                stats = await self._refresh_cycle(protocol, fetch_limit)
                logger.info(
                    "Refresh cycle: fetched=%d, new_healthy=%d, recovered=%d",
                    stats["fetched"], stats["new_healthy"], stats["recovered"],
                )
                self._last_refresh = datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                logger.error("Refresh cycle failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _refresh_cycle(self, protocol: str, fetch_limit: int) -> dict:
        """Single refresh cycle: fetch, test, add healthy, recover unhealthy, prune stale."""
        loop = asyncio.get_event_loop()
        stats = {"fetched": 0, "tested": 0, "new_healthy": 0, "recovered": 0}

        # 1. Fetch from CDN
        url = PROXIFLY_URL.format(protocol=protocol)
        try:
            def _fetch():
                req = urllib.request.Request(url, headers={"User-Agent": "LMArenaAutomation/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode())

            data = await loop.run_in_executor(None, _fetch)
            random.shuffle(data)
            candidates = [entry["proxy"] for entry in data[:fetch_limit * 5] if "proxy" in entry]
            stats["fetched"] = len(candidates)
        except Exception as exc:
            logger.warning("Failed to fetch proxies from CDN: %s", exc)
            candidates = []

        # 2. Health-check new candidates
        if candidates and self._check_fn:
            results = await asyncio.gather(
                *(loop.run_in_executor(None, self._check_fn, proxy, 8) for proxy in candidates)
            )
            healthy_new = [proxy for proxy, ok in zip(candidates, results) if ok]
            stats["tested"] = len(candidates)
            added = self.add_proxies(
                [{"server": s} for s in healthy_new], source="auto_fetch"
            )
            stats["new_healthy"] = added
        elif candidates:
            # No check function — add all
            added = self.add_proxies(
                [{"server": s} for s in candidates], source="auto_fetch"
            )
            stats["new_healthy"] = added

        # 3. Re-check currently unhealthy proxies
        if self._check_fn:
            unhealthy = [e for e in self._entries.values() if not e.healthy]
            if unhealthy:
                recheck_results = await asyncio.gather(
                    *(loop.run_in_executor(None, self._check_fn, e.server, 8) for e in unhealthy)
                )
                for entry, ok in zip(unhealthy, recheck_results):
                    if ok:
                        entry.healthy = True
                        entry.fail_count = 0
                        entry.last_checked = time.time()
                        stats["recovered"] += 1

        # 4. Prune stale auto-fetched unhealthy proxies (>1 hour)
        now = time.time()
        to_remove = [
            server for server, e in self._entries.items()
            if not e.healthy
            and e.source == "auto_fetch"
            and e.last_checked
            and (now - e.last_checked) > STALE_THRESHOLD
        ]
        for server in to_remove:
            del self._entries[server]
        if to_remove:
            logger.info("Pruned %d stale unhealthy proxies", len(to_remove))

        return stats

    async def health_check_all(self) -> dict:
        """Re-check all proxies in pool. Returns stats."""
        if not self._check_fn:
            return {"error": "no check function configured"}

        loop = asyncio.get_event_loop()
        entries = list(self._entries.values())
        results = await asyncio.gather(
            *(loop.run_in_executor(None, self._check_fn, e.server, 8) for e in entries)
        )

        healthy_count = 0
        recovered = 0
        for entry, ok in zip(entries, results):
            entry.last_checked = time.time()
            if ok:
                if not entry.healthy:
                    recovered += 1
                entry.healthy = True
                entry.fail_count = 0
                healthy_count += 1
            else:
                entry.fail_count += 1
                if entry.fail_count >= MAX_FAIL_COUNT:
                    entry.healthy = False

        return {
            "total": len(entries),
            "healthy": healthy_count,
            "unhealthy": len(entries) - healthy_count,
            "recovered": recovered,
        }

    # ── Status ──

    def get_status(self) -> dict:
        healthy = sum(1 for e in self._entries.values() if e.healthy)
        return {
            "total": len(self._entries),
            "healthy": healthy,
            "unhealthy": len(self._entries) - healthy,
            "max_healthy": self._max_healthy,
            "auto_refresh_active": self._running,
            "last_refresh": self._last_refresh,
            "proxies": [
                {
                    "server": e.server,
                    "healthy": e.healthy,
                    "fail_count": e.fail_count,
                    "last_checked": e.last_checked,
                    "source": e.source,
                }
                for e in self._entries.values()
            ],
        }

    @property
    def auto_refresh_settings(self) -> dict:
        """Return persisted auto-refresh settings."""
        return {
            "enabled": self._auto_refresh_enabled,
            "protocol": self._auto_refresh_protocol,
        }

    @property
    def healthy_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.healthy)

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def to_proxy_list(self) -> List[dict]:
        """Return all healthy proxies as Playwright-compatible dicts."""
        return [e.to_playwright_dict() for e in self._entries.values() if e.healthy]
