import unittest

from src.proxy.pool import ProxyPool


class ProxyPoolCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_problematic_proxy_enters_cooldown(self) -> None:
        pool = ProxyPool()
        server = "http://1.2.3.4:8080"
        pool.add_proxies([{"server": server, "latency_ms": 120.0}])

        self.assertEqual(pool.healthy_count, 1)
        self.assertIsNotNone(pool.get_next_healthy())

        pool.mark_problematic(
            server,
            reason="rate_limit",
            cooldown_seconds=1800,
        )

        status = pool.get_status()
        proxy = status["proxies"][0]

        self.assertEqual(pool.healthy_count, 0)
        self.assertIsNone(pool.get_next_healthy())
        self.assertEqual(status["cooling_down"], 1)
        self.assertEqual(status["problematic"], 1)
        self.assertTrue(proxy["cooling_down"])
        self.assertTrue(proxy["flagged_problematic"])
        self.assertEqual(proxy["last_failure_reason"], "rate_limit")
        self.assertGreater(proxy["cooldown_remaining_seconds"], 0)

    async def test_health_check_skips_proxies_in_cooldown(self) -> None:
        calls = []

        def check_fn(server: str, timeout: int) -> float:
            calls.append((server, timeout))
            return 100.0

        pool = ProxyPool(check_fn=check_fn)
        server = "http://5.6.7.8:8080"
        pool.add_proxies([{"server": server, "latency_ms": 100.0}])
        pool.mark_problematic(server, reason="challenge", cooldown_seconds=300)

        stats = await pool.health_check_all()

        self.assertEqual(stats["checked"], 0)
        self.assertEqual(stats["cooling_down"], 1)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
