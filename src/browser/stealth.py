from __future__ import annotations

import logging

from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)

_stealth_instance = None


def _get_stealth():
    global _stealth_instance
    if _stealth_instance is None:
        from playwright_stealth import Stealth
        _stealth_instance = Stealth()
    return _stealth_instance


async def apply_stealth(context: BrowserContext) -> None:
    """Apply anti-detection patches to *context*.

    Uses playwright-stealth's Stealth class which patches the context
    so all current and future pages get stealth scripts injected.
    """
    stealth = _get_stealth()
    await stealth.apply_stealth_async(context)
    logger.debug("Stealth patches applied to context")
