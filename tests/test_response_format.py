import unittest

from src.core.response_format import validate_response_format


class ResponseFormatValidationTests(unittest.TestCase):
    def test_accepts_valid_json(self) -> None:
        ok, detail = validate_response_format('{"name":"arena"}', "json")
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_accepts_fenced_json(self) -> None:
        ok, detail = validate_response_format("```json\n{\"ok\": true}\n```", "json")
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_rejects_non_json_when_json_required(self) -> None:
        ok, detail = validate_response_format("hello world", "json")
        self.assertFalse(ok)
        self.assertIn("invalid JSON", detail)

    def test_accepts_html(self) -> None:
        ok, detail = validate_response_format("<div><p>Hello</p></div>", "html")
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_rejects_plain_text_when_html_required(self) -> None:
        ok, detail = validate_response_format("hello world", "html")
        self.assertFalse(ok)
        self.assertIn("HTML", detail)

    def test_accepts_plain_text(self) -> None:
        ok, detail = validate_response_format("hello world", "plain_text")
        self.assertTrue(ok)
        self.assertIsNone(detail)

    def test_rejects_json_when_plain_text_required(self) -> None:
        ok, detail = validate_response_format('{"hello":"world"}', "plain_text")
        self.assertFalse(ok)
        self.assertIn("JSON", detail)

    def test_rejects_html_when_plain_text_required(self) -> None:
        ok, detail = validate_response_format("<section>Hello</section>", "plain_text")
        self.assertFalse(ok)
        self.assertIn("HTML", detail)


if __name__ == "__main__":
    unittest.main()
