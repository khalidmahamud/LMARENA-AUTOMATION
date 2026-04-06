import unittest

from src.workers.arena_worker import ArenaWorker


class NavigationErrorLoggingTests(unittest.TestCase):
    def test_describe_navigation_error_includes_type_and_message(self) -> None:
        detail = ArenaWorker._describe_navigation_error(
            RuntimeError("proxy connect ECONNREFUSED 1.2.3.4:1080")
        )

        self.assertIn("RuntimeError:", detail)
        self.assertIn("ECONNREFUSED", detail)

    def test_describe_navigation_error_truncates_long_messages(self) -> None:
        detail = ArenaWorker._describe_navigation_error(
            RuntimeError("x" * 400)
        )

        self.assertLessEqual(len(detail), 220)
        self.assertTrue(detail.endswith("..."))


if __name__ == "__main__":
    unittest.main()
