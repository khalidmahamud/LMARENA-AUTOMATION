"""Health-aware, auto-refreshing proxy pool backed by an XLSX source."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_FAIL_COUNT = 3
STALE_THRESHOLD = 3600.0  # prune reloadable unhealthy proxies after 1 hour
CHALLENGER_RECHECK_COOLDOWN = 600.0  # avoid re-testing same challenger too frequently
DEFAULT_PROBLEM_COOLDOWN = 1800.0


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
    cooldown_until: Optional[float] = None
    last_failure_reason: Optional[str] = None
    problematic: bool = False

    def is_in_cooldown(self, now: Optional[float] = None) -> bool:
        if self.cooldown_until is None:
            return False
        current = time.time() if now is None else now
        return self.cooldown_until > current

    def to_playwright_dict(self) -> dict:
        """Return a Playwright-compatible proxy dict."""
        d: dict = {"server": self.server}
        if self.username:
            d["username"] = self.username
        if self.password:
            d["password"] = self.password
        return d


class ProxyPool:
    """Central proxy pool with health tracking and auto-refresh."""

    def __init__(
        self,
        check_fn: Optional[Callable[[str, int], bool]] = None,
        source_loader: Optional[Callable[[str, Optional[int]], List[dict]]] = None,
        max_healthy: int = 50,
        on_latency_update: Optional[Callable[[Dict[str, Dict]], None]] = None,
    ) -> None:
        self._entries: Dict[str, ProxyEntry] = {}
        self._healthy_index: int = 0
        self._lock = asyncio.Lock()
        self._check_fn = check_fn
        self._source_loader = source_loader
        self._max_healthy = max_healthy
        self._on_latency_update = on_latency_update
        self._max_latency_ms: float = 5000.0
        self._refresh_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_refresh: Optional[str] = None
        self._auto_refresh_protocol: str = "http"
        self._auto_refresh_enabled: bool = False
        self._auto_refresh_interval: float = 60.0
        self._auto_refresh_fetch_limit: int = 20
        self._challenger_recent_checks: Dict[str, float] = {}

    # ── Core API ──

    @staticmethod
    def _is_usable(entry: ProxyEntry) -> bool:
        """A proxy is usable only after a successful recent latency check."""
        return (
            entry.healthy
            and entry.latency_ms is not None
            and not entry.is_in_cooldown()
        )

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
        entry.cooldown_until = None
        entry.last_failure_reason = None
        entry.problematic = False

    def mark_problematic(
        self,
        server: str,
        *,
        reason: Optional[str] = None,
        cooldown_seconds: Optional[float] = None,
    ) -> None:
        """Flag a proxy/IP as problematic and keep it out of rotation for a cooldown."""
        entry = self._entries.get(server)
        if not entry:
            return
        now = time.time()
        cooldown = max(
            60.0,
            float(
                DEFAULT_PROBLEM_COOLDOWN
                if cooldown_seconds is None
                else cooldown_seconds
            ),
        )
        entry.fail_count = max(entry.fail_count + 1, MAX_FAIL_COUNT)
        entry.healthy = False
        entry.last_checked = now
        entry.latency_ms = None
        entry.cooldown_until = now + cooldown
        entry.last_failure_reason = reason
        entry.problematic = True
        logger.info(
            "Proxy %s flagged problematic for %.0f minutes%s",
            server,
            cooldown / 60.0,
            f" (reason={reason})" if reason else "",
        )

    def is_in_cooldown(self, server: str) -> bool:
        """Return whether a specific proxy is still cooling down."""
        entry = self._entries.get(server)
        if not entry:
            return False
        return entry.is_in_cooldown()

    def remove_proxy(self, server: str) -> None:
        """Remove a proxy entirely."""
        self._entries.pop(server, None)

    # ── Background Auto-Refresh ──

    async def start_auto_refresh(
        self,
        protocol: str = "http",
        fetch_limit: int = 20,
        interval: float = 60.0,
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
        """Cancel the background refresh task."""
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
            "cooling_down": 0,
        }

        # ── Step 1: Re-check ALL existing proxies (in batches of 20) ──
        if self._check_fn and self._entries:
            now = time.time()
            entries = list(self._entries.values())
            entries_to_check = []
            for entry in entries:
                if entry.is_in_cooldown(now):
                    stats["cooling_down"] += 1
                    continue
                entries_to_check.append(entry)
            results = await self._check_in_batches(
                [e.server for e in entries_to_check], timeout=5
            )
            stats["checked"] = len(entries_to_check)
            latencies = []
            for entry, latency in zip(entries_to_check, results):
                entry.last_checked = time.time()
                if latency > 0 and latency <= self._max_latency_ms:
                    if not entry.healthy:
                        stats["recovered"] += 1
                    entry.healthy = True
                    entry.fail_count = 0
                    entry.latency_ms = round(latency, 1)
                    entry.cooldown_until = None
                    entry.last_failure_reason = None
                    entry.problematic = False
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

            # Write back updated latencies to XLSX source
            if self._on_latency_update:
                updates = {}
                for entry in entries_to_check:
                    if entry.latency_ms is not None and entry.source == "xlsx":
                        updates[entry.server] = {
                            "latency_ms": entry.latency_ms,
                            "checked_at": datetime.now(timezone.utc).strftime(
                                "%Y-%m-%d %H:%M:%S UTC"
                            ),
                        }
                if updates:
                    try:
                        await loop.run_in_executor(
                            None, self._on_latency_update, updates
                        )
                    except Exception as exc:
                        logger.warning("Failed to write back latency updates: %s", exc)

        stats["healthy"] = self.healthy_count
        stats["unhealthy"] = len(self._entries) - stats["healthy"]

        # ── Step 2: Purge stale reloadable unhealthy proxies (>1 hour) ──
        now = time.time()
        to_remove = [
            server for server, e in self._entries.items()
            if not e.healthy
            and e.source != "manual"
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
        if challenger_count > 0 and self._check_fn and self._source_loader:
            candidate_budget = max(effective_fetch_limit, challenger_count * 5)
            logger.info(
                "Pool has %d/%d healthy — testing up to %d challenger proxies from source",
                self.healthy_count,
                self._max_healthy,
                candidate_budget,
            )
            try:
                source_scan_limit = max(candidate_budget * 5, self._max_healthy * 3)
                data = await loop.run_in_executor(
                    None,
                    partial(self._source_loader, protocol, source_scan_limit),
                )
                # Prefer low-latency candidates from the source
                data.sort(key=lambda d: d.get("latency_ms") or float("inf"))

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
                    proxy = entry.get("server") if isinstance(entry, dict) else None
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
                            {
                                "server": entry["server"],
                                "username": entry.get("username"),
                                "password": entry.get("password"),
                                "latency_ms": round(latency, 1),
                            }
                            for entry, latency in zip(
                                [e for e in data if e.get("server") in candidates],
                                results,
                            )
                            if latency > 0 and latency <= self._max_latency_ms
                        ],
                        key=lambda p: p["latency_ms"],
                    )
                    added = self.add_proxies(good, source="xlsx")
                    stats["added"] = added
                    if added:
                        logger.info("Filled pool with %d new proxies (%d still needed)",
                                    added, max(0, self._max_healthy - self.healthy_count))

            except Exception as exc:
                logger.warning("Failed to load replacement proxies from source: %s", exc)

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
        cooling_entries = [e for e in self._entries.values() if e.is_in_cooldown()]
        latencies = [e.latency_ms for e in healthy_entries if e.latency_ms is not None]
        avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None
        now = time.time()
        return {
            "total": len(self._entries),
            "healthy": len(healthy_entries),
            "raw_healthy": len([e for e in self._entries.values() if e.healthy]),
            "degraded": len(degraded_entries),
            "unhealthy": len(unhealthy_entries),
            "cooling_down": len(cooling_entries),
            "problematic": len([e for e in self._entries.values() if e.problematic]),
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
                    "cooling_down": e.is_in_cooldown(now),
                    "cooldown_remaining_seconds": (
                        max(0, round(e.cooldown_until - now))
                        if e.cooldown_until and e.cooldown_until > now
                        else 0
                    ),
                    "flagged_problematic": e.problematic,
                    "last_failure_reason": e.last_failure_reason,
                }
                for e in self._entries.values()
            ],
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
