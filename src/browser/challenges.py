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
    SECURITY_MODAL = "security_modal"
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

    # Arena security modal shown after submit attempts.
    try:
        has_security_modal = await page.evaluate(
            """() => {
                const text = document.body?.innerText || "";
                return (
                    text.includes("Security Verification") &&
                    text.includes("quick security check")
                );
            }"""
        )
        if has_security_modal:
            logger.warning("Security verification modal detected")
            return ChallengeType.SECURITY_MODAL
    except Exception:
        pass

    # reCAPTCHA — only flag if the iframe is actually visible
    # (Arena embeds invisible reCAPTCHA in the background; that's not a blocker)
    try:
        sel = selectors.get("challenge.recaptcha_iframe")
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            logger.warning("reCAPTCHA challenge detected")
            return ChallengeType.RECAPTCHA
    except KeyError:
        pass

    return ChallengeType.NONE
