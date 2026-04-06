import unittest

from src.workers.arena_worker import ArenaWorker


class _FakePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.wait_calls: list[tuple[str, int]] = []

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        self.wait_calls.append((state, timeout))

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, bootstrap_page: _FakePage, automation_page: _FakePage) -> None:
        self.bootstrap_page = bootstrap_page
        self.automation_page = automation_page
        self.new_page_calls = 0

    async def new_page(self) -> _FakePage:
        self.new_page_calls += 1
        return self.automation_page


class BootstrapTabReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_tab_is_replaced_with_dedicated_automation_tab(self) -> None:
        worker = object.__new__(ArenaWorker)
        bootstrap_page = _FakePage("chrome://newtab/")
        automation_page = _FakePage("about:blank")
        worker._page = bootstrap_page
        worker._context = _FakeContext(bootstrap_page, automation_page)
        logs: list[tuple[str, str]] = []

        async def _fake_log(level: str, text: str) -> None:
            logs.append((level, text))

        async def _fake_sleep(duration: float, pause_event=None) -> None:
            return None

        worker._log = _fake_log
        worker._sleep_with_controls = _fake_sleep

        await worker._prepare_page_for_arena_navigation()

        self.assertIs(worker._page, automation_page)
        self.assertTrue(bootstrap_page.closed)
        self.assertEqual(worker._context.new_page_calls, 1)
        self.assertTrue(
            any("dedicated Arena tab" in text for _, text in logs)
        )


if __name__ == "__main__":
    unittest.main()
