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
    latency_ms: Optional[float] = None

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
        self._max_latency_ms: float = 5000.0
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
        # Keep lowest-latency proxies; prioritize manual over auto_fetch at same latency
        healthy = sorted(
            [e for e in self._entries.values() if e.healthy],
            key=lambda e: (
                e.latency_ms if e.latency_ms is not None else float("inf"),
                0 if e.source == "manual" else 1,
            ),
        )
        excess = healthy[self._max_healthy:]
        for entry in excess:
            del self._entries[entry.server]
        if excess:
            logger.info("Trimmed %d excess proxies (new max: %d)", len(excess), self._max_healthy)
        logger.info("Max healthy proxies set to %d", self._max_healthy)

    def set_max_latency(self, ms: float) -> None:
        """Update the max latency threshold and remove proxies exceeding it."""
        self._max_latency_ms = max(100.0, ms)
        # Remove proxies that exceed the new threshold
        to_remove = [
            server for server, e in self._entries.items()
            if e.healthy and e.latency_ms is not None and e.latency_ms > self._max_latency_ms
        ]
        for server in to_remove:
            del self._entries[server]
        if to_remove:
            logger.info("Removed %d proxies exceeding latency threshold %.0fms", len(to_remove), self._max_latency_ms)
        logger.info("Max latency set to %.0fms", self._max_latency_ms)

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
                latency_ms=p.get("latency_ms"),
            )
            added += 1
        if added:
            logger.info("Added %d new proxies to pool (total: %d, max: %d)", added, len(self._entries), self._max_healthy)
        return added

    def get_next_healthy(self) -> Optional[dict]:
        """Round-robin through healthy proxies sorted by latency (fastest first)."""
        healthy = sorted(
            [e for e in self._entries.values() if e.healthy],
            key=lambda e: (e.latency_ms if e.latency_ms is not None else float("inf")),
        )
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

        # Separate healthy and unhealthy; sort by latency (fastest first)
        healthy = sorted(
            [e for e in self._entries.values() if e.healthy],
            key=lambda e: (
                e.latency_ms if e.latency_ms is not None else float("inf"),
                0 if e.source == "manual" else 1,
            ),
        )
        unhealthy = [e for e in self._entries.values() if not e.healthy]
        capped_healthy = healthy[: self._max_healthy]

        data = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "max_healthy": self._max_healthy,
            "max_latency_ms": self._max_latency_ms,
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
            if "max_latency_ms" in data:
                self._max_latency_ms = max(100.0, float(data["max_latency_ms"]))
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
                    latency_ms=p.get("latency_ms"),
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
                stats = await self.maintain_pool(protocol)
                logger.info(
                    "Pool maintenance: checked=%d, healthy=%d, dropped=%d, recovered=%d, added=%d",
                    stats["checked"], stats["healthy"], stats["dropped"],
                    stats["recovered"], stats["added"],
                )
                self._last_refresh = datetime.now(timezone.utc).isoformat()
            except Exception as exc:
                logger.error("Pool maintenance failed: %s", exc)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def maintain_pool(self, protocol: str = "http") -> dict:
        """Self-healing pool maintenance: re-check all, purge dead, fill gaps.

        This is the single lifecycle method that keeps the pool healthy.
        Called by auto-refresh loop, health-check button, and on startup.
        """
        loop = asyncio.get_event_loop()
        stats = {
            "checked": 0, "healthy": 0, "unhealthy": 0,
            "dropped": 0, "recovered": 0,
            "fetched": 0, "added": 0,
            "avg_latency_ms": None,
        }

        # ── Step 1: Re-check ALL existing proxies ──
        if self._check_fn and self._entries:
            entries = list(self._entries.values())
            results = await asyncio.gather(
                *(loop.run_in_executor(None, self._check_fn, e.server, 8) for e in entries)
            )
            stats["checked"] = len(entries)
            latencies = []
            for entry, latency in zip(entries, results):
                entry.last_checked = time.time()
                if latency > 0 and latency <= self._max_latency_ms:
                    if not entry.healthy:
                        stats["recovered"] += 1
                    entry.healthy = True
                    entry.fail_count = 0
                    entry.latency_ms = round(latency, 1)
                    latencies.append(latency)
                else:
                    entry.fail_count += 1
                    was_healthy = entry.healthy
                    if entry.fail_count >= MAX_FAIL_COUNT:
                        entry.healthy = False
                    if was_healthy and not entry.healthy:
                        stats["dropped"] += 1
                        logger.info("Proxy %s dropped (latency=%.0f, threshold=%.0f)",
                                    entry.server, latency if latency > 0 else -1, self._max_latency_ms)

            stats["avg_latency_ms"] = round(sum(latencies) / len(latencies), 1) if latencies else None

        stats["healthy"] = self.healthy_count
        stats["unhealthy"] = len(self._entries) - stats["healthy"]

        # ── Step 2: Purge stale auto-fetched unhealthy proxies (>1 hour) ──
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

        # ── Step 3: Fill gap — fetch new proxies if below max_healthy ──
        gap = self._max_healthy - self.healthy_count
        if gap > 0 and self._check_fn:
            logger.info("Pool has %d/%d healthy — fetching %d replacements",
                        self.healthy_count, self._max_healthy, gap)
            url = PROXIFLY_URL.format(protocol=protocol)
            try:
                def _fetch():
                    req = urllib.request.Request(url, headers={"User-Agent": "LMArenaAutomation/1.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return json.loads(resp.read().decode())

                data = await loop.run_in_executor(None, _fetch)
                random.shuffle(data)
                # Test more candidates than needed to find enough good ones
                candidates = [
                    entry["proxy"] for entry in data[: gap * 10]
                    if "proxy" in entry and entry["proxy"] not in self._entries
                ]
                stats["fetched"] = len(candidates)

                if candidates:
                    results = await asyncio.gather(
                        *(loop.run_in_executor(None, self._check_fn, proxy, 8) for proxy in candidates)
                    )
                    good = sorted(
                        [
                            {"server": proxy, "latency_ms": round(latency, 1)}
                            for proxy, latency in zip(candidates, results)
                            if latency > 0 and latency <= self._max_latency_ms
                        ],
                        key=lambda p: p["latency_ms"],
                    )
                    added = self.add_proxies(good, source="auto_fetch")
                    stats["added"] = added
                    if added:
                        logger.info("Filled pool with %d new proxies (%d still needed)",
                                    added, max(0, self._max_healthy - self.healthy_count))

            except Exception as exc:
                logger.warning("Failed to fetch replacement proxies: %s", exc)

        # ── Step 4: Save ──
        self.save_to_file()

        stats["healthy"] = self.healthy_count
        stats["unhealthy"] = len(self._entries) - stats["healthy"]
        return stats

    async def health_check_all(self) -> dict:
        """Backward-compatible wrapper — runs full pool maintenance."""
        return await self.maintain_pool(protocol=self._auto_refresh_protocol)

    # ── Status ──

    def get_status(self) -> dict:
        healthy_entries = [e for e in self._entries.values() if e.healthy]
        latencies = [e.latency_ms for e in healthy_entries if e.latency_ms is not None]
        avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None
        return {
            "total": len(self._entries),
            "healthy": len(healthy_entries),
            "unhealthy": len(self._entries) - len(healthy_entries),
            "max_healthy": self._max_healthy,
            "max_latency_ms": self._max_latency_ms,
            "avg_latency_ms": avg_latency,
            "auto_refresh_active": self._running,
            "last_refresh": self._last_refresh,
            "proxies": [
                {
                    "server": e.server,
                    "healthy": e.healthy,
                    "fail_count": e.fail_count,
                    "last_checked": e.last_checked,
                    "source": e.source,
                    "latency_ms": e.latency_ms,
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
