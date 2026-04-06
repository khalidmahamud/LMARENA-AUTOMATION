import unittest

from src.workers.arena_worker import ArenaWorker


class ImageRetryReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_last_prompt_reuses_stored_images(self) -> None:
        worker = object.__new__(ArenaWorker)
        worker._id = 1
        worker._last_prompt = "hello"
        worker._last_model_a = "gpt-5.1"
        worker._last_model_b = "gpt-5.2"
        worker._last_images = [{"data": "abc", "mime_type": "image/png"}]

        captured = {}

        async def _fake_submit_prompt(
            prompt,
            model_a=None,
            model_b=None,
            retry_on_challenge=0,
            pause_event=None,
            images=None,
        ):
            captured["prompt"] = prompt
            captured["model_a"] = model_a
            captured["model_b"] = model_b
            captured["retry_on_challenge"] = retry_on_challenge
            captured["images"] = images

        worker.submit_prompt = _fake_submit_prompt

        await worker._replay_last_prompt(retry_on_challenge=3)

        self.assertEqual(captured["prompt"], "hello")
        self.assertEqual(captured["model_a"], "gpt-5.1")
        self.assertEqual(captured["model_b"], "gpt-5.2")
        self.assertEqual(captured["retry_on_challenge"], 3)
        self.assertEqual(captured["images"], worker._last_images)
        self.assertIsNot(captured["images"], worker._last_images)


if __name__ == "__main__":
    unittest.main()
