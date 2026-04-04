import unittest

from src.orchestrator.session_rules import prompt_models_for_batch


class RunOrchestratorSessionRulesTests(unittest.TestCase):
    def test_first_batch_without_system_prompt_selects_models(self) -> None:
        self.assertEqual(
            prompt_models_for_batch(
                batch_idx=0,
                prompts_per_session=3,
                system_prompt="",
                model_a="gpt-5.1",
                model_b="gpt-5.2",
            ),
            ("gpt-5.1", "gpt-5.2"),
        )

    def test_first_batch_with_system_prompt_skips_prompt_model_reselection(self) -> None:
        self.assertEqual(
            prompt_models_for_batch(
                batch_idx=0,
                prompts_per_session=3,
                system_prompt="You are helpful.",
                model_a="gpt-5.1",
                model_b="gpt-5.2",
            ),
            (None, None),
        )

    def test_followup_batch_in_same_session_skips_model_reselection(self) -> None:
        self.assertEqual(
            prompt_models_for_batch(
                batch_idx=1,
                prompts_per_session=3,
                system_prompt="",
                model_a="gpt-5.1",
                model_b="gpt-5.2",
            ),
            (None, None),
        )


if __name__ == "__main__":
    unittest.main()
