import unittest

from src.workers.arena_worker import ArenaWorker


class NavigationRetryPolicyTests(unittest.TestCase):
    def test_initial_blank_urls_require_window_settle(self) -> None:
        self.assertTrue(ArenaWorker._is_initial_window_url("about:blank"))
        self.assertTrue(ArenaWorker._is_initial_window_url("chrome://newtab/"))
        self.assertFalse(
            ArenaWorker._is_initial_window_url(
                "https://arena.ai/text/side-by-side"
            )
        )

    def test_first_connection_closed_retries_in_same_window(self) -> None:
        should_retry = ArenaWorker._should_retry_navigation_in_same_window(
            RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED"),
            attempt=1,
        )
        self.assertTrue(should_retry)

    def test_later_connection_closed_does_not_block_recycle(self) -> None:
        should_retry = ArenaWorker._should_retry_navigation_in_same_window(
            RuntimeError("Page.goto: net::ERR_CONNECTION_CLOSED"),
            attempt=2,
        )
        self.assertFalse(should_retry)

    def test_proxy_errors_still_allow_recycle(self) -> None:
        should_retry = ArenaWorker._should_retry_navigation_in_same_window(
            RuntimeError("Page.goto: net::ERR_PROXY_CONNECTION_FAILED"),
            attempt=1,
        )
        self.assertFalse(should_retry)


if __name__ == "__main__":
    unittest.main()
