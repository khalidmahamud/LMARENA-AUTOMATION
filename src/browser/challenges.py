from __future__ import annotations

import logging
from enum import Enum

from playwright.async_api import Page

from src.browser.selectors import SelectorRegistry

logger = logging.getLogger(__name__)


class ChallengeType(str, Enum):
    NONE = "none"
    TURNSTILE = "turnstile"
    RECAPTCHA = "recaptcha"
    LOGIN_WALL = "login_wall"


async def detect_challenge(page: Page) -> ChallengeType:
    """Inspect the current page for known challenge / login barriers.

    Returns ``ChallengeType.NONE`` if the page is clean.
    """
    selectors = SelectorRegistry.instance()

    # Cloudflare Turnstile
    try:
        sel = selectors.get("challenge.turnstile_iframe")
        if await page.query_selector(sel):
            logger.warning("Turnstile challenge detected")
            return ChallengeType.TURNSTILE
    except KeyError:
        pass

    try:
        sel = selectors.get("challenge.turnstile_container")
        if await page.query_selector(sel):
            logger.warning("Turnstile container detected")
            return ChallengeType.TURNSTILE
    except KeyError:
        pass

    # Check page title for "Just a moment" (Cloudflare interstitial)
    title = await page.title()
    if "just a moment" in title.lower():
        logger.warning("Cloudflare interstitial detected via page title")
        return ChallengeType.TURNSTILE

    # reCAPTCHA
    try:
        sel = selectors.get("challenge.recaptcha_iframe")
        if await page.query_selector(sel):
            logger.warning("reCAPTCHA challenge detected")
            return ChallengeType.RECAPTCHA
    except KeyError:
        pass

    # Login wall
    try:
        sel = selectors.get("challenge.login_button")
        if await page.query_selector(sel):
            logger.info("Login wall detected")
            return ChallengeType.LOGIN_WALL
    except KeyError:
        pass

    return ChallengeType.NONE
