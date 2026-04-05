import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLLER_PATH = ROOT / "src" / "workers" / "response_poller.py"


class ResponsePollerReasoningFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = POLLER_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_reasoning_summary_helper_exists(self) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "ResponsePoller":
                helper_names = {
                    item.name
                    for item in node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                self.assertIn("_looks_like_reasoning_summary", helper_names)
                self.assertIn(
                    "_scroll_streaming_responses_to_bottom",
                    helper_names,
                )
                return
        self.fail("ResponsePoller class not found")

    def test_reasoning_markers_are_present_in_source(self) -> None:
        self.assertIn("thought for", self.source)
        self.assertIn("reasoned for", self.source)
        self.assertIn("thinking...", self.source)

    def test_poller_blanks_reasoning_only_text_before_stability_checks(self) -> None:
        self.assertIn("if self._looks_like_reasoning_summary(cur_a):", self.source)
        self.assertIn("if self._looks_like_reasoning_summary(cur_b):", self.source)

    def test_poller_scrolls_smoothly_while_streaming(self) -> None:
        self.assertIn("if streaming_active:", self.source)
        self.assertIn(
            "await self._scroll_streaming_responses_to_bottom(",
            self.source,
        )
        self.assertIn('behavior: "smooth"', self.source)


if __name__ == "__main__":
    unittest.main()
