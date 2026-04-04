from __future__ import annotations

from typing import Optional, Tuple


def is_first_batch_in_session(
    batch_idx: int,
    prompts_per_session: int,
) -> bool:
    pps = max(1, int(prompts_per_session or 1))
    return batch_idx % pps == 0


def prompt_models_for_batch(
    batch_idx: int,
    prompts_per_session: int,
    system_prompt: str,
    model_a: Optional[str],
    model_b: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    # Model selection should happen once per session. If a system prompt is
    # being sent at the start of this session, the models are already chosen
    # during that submission and must not be re-selected for the follow-up
    # user prompt.
    if is_first_batch_in_session(batch_idx, prompts_per_session) and not system_prompt:
        return model_a, model_b
    return None, None
