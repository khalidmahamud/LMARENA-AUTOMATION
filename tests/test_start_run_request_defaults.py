import unittest

from src.models.messages import StartRunRequest


class StartRunRequestDefaultsTests(unittest.TestCase):
    def test_proxy_defaults_match_ui_expectations(self) -> None:
        request = StartRunRequest(prompt="hello")

        self.assertEqual(request.windows_per_proxy, 2)
        self.assertEqual(request.problematic_ip_cooldown_minutes, 30)
        self.assertEqual(request.response_format, "any")


if __name__ == "__main__":
    unittest.main()
