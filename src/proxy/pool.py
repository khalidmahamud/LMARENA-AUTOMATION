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
CHALLENGER_RECHECK_COOLDOWN = 600.0  # avoid re-testing same challenger too frequently


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
        self._auto_refresh_interval: float = 300.0
        self._auto_refresh_fetch_limit: int = 20
        self._challenger_recent_checks: Dict[str, float] = {}

    # ── Core API ──

    @staticmethod
    def _is_usable(entry: ProxyEntry) -> bool:
        """A proxy is usable only after a successful recent latency check."""
        return entry.healthy and entry.latency_ms is not None

    @staticmethod
    def _entry_rank(entry: ProxyEntry) -> tuple:
        """Return a rank where lower is better."""
        if entry.healthy and entry.latency_ms is not None:
            state_rank = 0
        elif entry.healthy:
            state_rank = 1
        else:
            state_rank = 2
        latency_rank = entry.latency_ms if entry.latency_ms is not None else float("inf")
        source_rank = 0 if entry.source == "manual" else 1
        return (state_rank, latency_rank, source_rank, entry.server)

    def _trim_pool_to_cap(self) -> int:
        """Keep only the best entries up to the configured cap."""
        ranked = sorted(self._entries.values(), key=self._entry_rank)
        keep = {entry.server for entry in ranked[: self._max_healthy]}
        to_remove = [server for server in self._entries if server not in keep]
        for server in to_remove:
            del self._entries[server]
        return len(to_remove)

    def set_max_healthy(self, limit: int) -> None:
        """Update the pool cap and trim excess entries."""
        self._max_healthy = max(1, limit)
        removed = self._trim_pool_to_cap()
        if removed:
            logger.info("Trimmed %d excess proxies (new max: %d)", removed, self._max_healthy)
        logger.info("Max proxy pool size set to %d", self._max_healthy)

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
        removed = self._trim_pool_to_cap()
        if added:
            logger.info(
                "Added %d proxies to pool, removed %d slower/worse entries (total: %d, max: %d)",
                added,
                removed,
                len(self._entries),
                self._max_healthy,
            )
        return added

    def get_next_healthy(self) -> Optional[dict]:
        """Round-robin through healthy proxies sorted by latency (fastest first).

        Each call returns a different proxy so that separate browser windows
        get distinct IPs.
        """
        healthy = sorted(
            [e for e in self._entries.values() if self._is_usable(e)],
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
        """Serialize pool to JSON."""
        path = path or self.PERSIST_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self._trim_pool_to_cap()
        ranked = sorted(self._entries.values(), key=self._entry_rank)

        data = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "max_healthy": self._max_healthy,
            "max_latency_ms": self._max_latency_ms,
            "auto_refresh_enabled": self._auto_refresh_enabled,
            "auto_refresh_protocol": self._auto_refresh_protocol,
            "auto_refresh_interval": self._auto_refresh_interval,
            "auto_refresh_fetch_limit": self._auto_refresh_fetch_limit,
            "proxies": [asdict(e) for e in ranked],
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info("Proxy pool saved to %s (%d proxies)", path, len(ranked))
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
            if "auto_refresh_interval" in data:
                self._auto_refresh_interval = max(60.0, float(data["auto_refresh_interval"]))
            if "auto_refresh_fetch_limit" in data:
                self._auto_refresh_fetch_limit = max(1, int(data["auto_refresh_fetch_limit"]))

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
            self._trim_pool_to_cap()
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
        self._auto_refresh_interval = interval
        self._auto_refresh_fetch_limit = max(1, int(fetch_limit))
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(protocol, self._auto_refresh_fetch_limit, interval)
        )
        logger.info(
            "Auto-refresh started (protocol=%s, fetch_limit=%d, interval=%.0fs)",
            protocol,
            self._auto_refresh_fetch_limit,
            interval,
        )

    async def stop_auto_refresh(self, clear_enabled: bool = True) -> None:
        """Cancel the background refresh task.

        When shutting down the app, keep the persisted preference so auto-refresh
        can resume on the next startup.
        """
        self._running = False
        if clear_enabled:
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
                async with self._lock:
                    stats = await self.maintain_pool(protocol, fetch_limit=fetch_limit)
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

    async def _check_in_batches(self, proxies, timeout=5, batch_size=20):
        """Check proxies in batches to avoid overwhelming the network."""
        loop = asyncio.get_event_loop()
        all_results = []
        for i in range(0, len(proxies), batch_size):
            batch = proxies[i : i + batch_size]
            results = await asyncio.gather(
                *(loop.run_in_executor(None, self._check_fn, p, timeout) for p in batch)
            )
            all_results.extend(results)
        return all_results

    async def maintain_pool(
        self, protocol: str = "http", fetch_limit: Optional[int] = None
    ) -> dict:
        """Self-healing pool maintenance: re-check all, purge dead, fill gaps.

        This is the single lifecycle method that keeps the pool healthy.
        Called by auto-refresh loop, health-check button, and on startup.
        """
        loop = asyncio.get_event_loop()
        effective_fetch_limit = max(
            1,
            int(fetch_limit if fetch_limit is not None else self._auto_refresh_fetch_limit),
        )
        stats = {
            "checked": 0, "healthy": 0, "unhealthy": 0,
            "dropped": 0, "recovered": 0,
            "fetched": 0, "added": 0,
            "avg_latency_ms": None,
        }

        # ── Step 1: Re-check ALL existing proxies (in batches of 20) ──
        if self._check_fn and self._entries:
            entries = list(self._entries.values())
            results = await self._check_in_batches(
                [e.server for e in entries], timeout=5
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
                    # Do not keep showing a stale "good" latency after a failed check.
                    entry.latency_ms = None
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
        trimmed = self._trim_pool_to_cap()
        if trimmed:
            logger.info("Trimmed %d excess proxies after health check", trimmed)

        # ── Step 3: Fetch challengers to fill gaps or replace slower proxies ──
        gap = self._max_healthy - self.healthy_count
        challenger_count = gap if gap > 0 else min(3, max(1, self._max_healthy // 3))
        if challenger_count > 0 and self._check_fn:
            candidate_budget = max(effective_fetch_limit, challenger_count * 5)
            logger.info(
                "Pool has %d/%d healthy — testing up to %d challenger proxies",
                self.healthy_count,
                self._max_healthy,
                candidate_budget,
            )
            url = PROXIFLY_URL.format(protocol=protocol)
            try:
                def _fetch():
                    req = urllib.request.Request(url, headers={"User-Agent": "LMArenaAutomation/1.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        return json.loads(resp.read().decode())

                data = await loop.run_in_executor(None, _fetch)
                random.shuffle(data)

                # Keep a short cooldown so each cycle explores fresh candidates.
                now = time.time()
                cooldown_cutoff = now - CHALLENGER_RECHECK_COOLDOWN
                if self._challenger_recent_checks:
                    self._challenger_recent_checks = {
                        proxy: ts
                        for proxy, ts in self._challenger_recent_checks.items()
                        if ts >= cooldown_cutoff
                    }

                seen = set()
                candidates = []
                for entry in data:
                    proxy = entry.get("proxy") if isinstance(entry, dict) else None
                    if (
                        not proxy
                        or proxy in seen
                        or proxy in self._entries
                        or self._challenger_recent_checks.get(proxy, 0) >= cooldown_cutoff
                    ):
                        continue
                    seen.add(proxy)
                    candidates.append(proxy)
                    if len(candidates) >= candidate_budget:
                        break
                stats["fetched"] = len(candidates)

                if candidates:
                    checked_at = time.time()
                    for proxy in candidates:
                        self._challenger_recent_checks[proxy] = checked_at

                    results = await self._check_in_batches(candidates, timeout=5)
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
        async with self._lock:
            return await self.maintain_pool(
                protocol=self._auto_refresh_protocol,
                fetch_limit=self._auto_refresh_fetch_limit,
            )

    # ── Status ──

    def get_status(self) -> dict:
        healthy_entries = [e for e in self._entries.values() if self._is_usable(e)]
        degraded_entries = [e for e in self._entries.values() if e.healthy and e.latency_ms is None]
        unhealthy_entries = [e for e in self._entries.values() if not e.healthy]
        latencies = [e.latency_ms for e in healthy_entries if e.latency_ms is not None]
        avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None
        return {
            "total": len(self._entries),
            "healthy": len(healthy_entries),
            "raw_healthy": len([e for e in self._entries.values() if e.healthy]),
            "degraded": len(degraded_entries),
            "unhealthy": len(unhealthy_entries),
            "max_healthy": self._max_healthy,
            "max_latency_ms": self._max_latency_ms,
            "avg_latency_ms": avg_latency,
            "auto_refresh_enabled": self._auto_refresh_enabled,
            "auto_refresh_active": self._running,
            "auto_refresh_interval": self._auto_refresh_interval,
            "last_refresh": self._last_refresh,
            "proxies": [
                {
                    "server": e.server,
                    "healthy": self._is_usable(e),
                    "degraded": e.healthy and e.latency_ms is None,
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
            "interval": self._auto_refresh_interval,
            "fetch_limit": self._auto_refresh_fetch_limit,
        }

    @property
    def healthy_count(self) -> int:
        return sum(1 for e in self._entries.values() if self._is_usable(e))

    @property
    def total_count(self) -> int:
        return len(self._entries)

    def to_proxy_list(self) -> List[dict]:
        """Return all healthy proxies as Playwright-compatible dicts."""
        return [e.to_playwright_dict() for e in self._entries.values() if self._is_usable(e)]
