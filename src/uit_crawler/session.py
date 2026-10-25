"""Persistent manual login and fail-closed browser safety checks."""

from __future__ import annotations

import asyncio
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout, async_playwright

from .models import StopCrawl
from .urls import is_facebook_url

# Probe visible page chrome, not user-written articles. A confession mentioning a
# checkpoint must never be mistaken for an account checkpoint.
_SAFETY_PROBE = r"""() => {
    const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    const outsideContent = e => !e.closest('[role="article"], [aria-posinset], [role="feed"], [data-ad-preview], [data-ad-comet-preview], [contenteditable="true"], script, style');
    const text = root => {
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        const parts = [];
        let node, length = 0;
        while ((node = walker.nextNode()) && length < 12000) {
            const parent = node.parentElement;
            if (parent && outsideContent(parent) && visible(parent)) {
                const value = node.textContent.trim();
                if (value) { parts.push(value); length += value.length; }
            }
        }
        return parts.join('\n');
    };
    const notices = [...document.querySelectorAll('[role="dialog"], [role="alert"]')]
        .filter(e => visible(e) && outsideContent(e)).map(text).join('\n');
    return {
        url: location.href,
        readyState: document.readyState,
        password: [...document.querySelectorAll('input[type="password"]')].some(visible),
        captcha: [...document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], input[name="captcha_response"]')].some(visible),
        notices,
        pageText: document.body ? text(document.body) : '',
    };
}"""

_CHECKPOINT = re.compile(
    r"confirm (?:that )?it[’']?s you|confirm your identity|security check|"
    r"xác nhận (?:đó là bạn|danh tính)|kiểm tra bảo mật", re.IGNORECASE
)
_RESTRICTED = re.compile(
    r"your account (?:has been|is) (?:locked|suspended|disabled|restricted)|"
    r"we suspended your account|tài khoản của bạn (?:đã |đang )?bị (?:khóa|đình chỉ|vô hiệu hóa|hạn chế)", re.IGNORECASE
)
_THROTTLED = re.compile(
    r"you[’']?re temporarily blocked|you are temporarily blocked|"
    r"you can[’']?t use this feature right now|we limit how often you can|"
    r"bạn (?:đang |đã )?tạm thời bị chặn|bạn hiện không thể dùng tính năng này|"
    r"chúng tôi giới hạn tần suất", re.IGNORECASE
)


def classify_safety(url: str, probe: dict, authenticated: bool, http_stop: str | None = None) -> str | None:
    """Classify only explicit page/auth signals, never arbitrary article text."""
    path = urlsplit(url).path.casefold()
    if http_stop:
        return http_stop
    if any(part in path for part in ("/checkpoint", "/two_step_verification", "/challenge", "/recover")):
        return "checkpoint"
    if probe.get("captcha"):
        return "checkpoint"
    chrome = probe.get("notices", "") + "\n" + probe.get("pageText", "")
    if _RESTRICTED.search(chrome):
        return "access_restricted"
    if _THROTTLED.search(chrome):
        return "rate_limited"
    if _CHECKPOINT.search(chrome):
        return "checkpoint"
    if "/login" in path or probe.get("password") or not authenticated:
        return "auth_required"
    if not is_facebook_url(url):
        return "unexpected_navigation"
    return None


_STOP_MESSAGES = {
    "auth_required": "Facebook requires manual login. Run the login command before crawling.",
    "checkpoint": "Facebook requested a security check. Automation has stopped; handle it manually.",
    "rate_limited": "Facebook limited access. Automation has stopped; do not immediately retry.",
    "access_restricted": "Facebook restricted access. Automation has stopped; inspect the account manually.",
    "unexpected_navigation": "The browser left the expected Facebook origin. Automation has stopped.",
    "browser_closed": "The browser was closed before the operation finished.",
    "navigation_unstable": "Facebook kept navigating while its state was being checked. Wait for the page to settle, then run login again.",
}


def _navigation_interrupted(exc: PlaywrightError) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in (
        "execution context was destroyed",
        "cannot find context with specified id",
        "cannot find context with id",
    ))


class BrowserSession:
    def __init__(self, page: Page, action_delay: float = 2.0):
        self.page = page
        self.action_delay = action_delay
        self._http_stop: str | None = None
        self._last_action = time.monotonic()
        self._document_generation = 0
        page.on("response", self._on_response)
        page.on("framenavigated", self._on_navigation)

    def _on_navigation(self, frame) -> None:
        if frame == self.page.main_frame:
            self._document_generation += 1

    def _on_response(self, response) -> None:
        if is_facebook_url(response.url):
            if response.status == 429:
                self._http_stop = "rate_limited"
            elif response.status == 403 and response.request.resource_type == "document":
                self._http_stop = "access_restricted"

    async def inspect(self, *, timeout: float = 10.0) -> str | None:
        """Read cookies and safety DOM from one ready, unchanged document."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.page.is_closed():
                return "browser_closed"
            if self._http_stop:
                return self._http_stop
            try:
                remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
                await self.page.wait_for_load_state("domcontentloaded", timeout=remaining_ms)
                generation = self._document_generation
                probe = await self.page.evaluate(_SAFETY_PROBE)
                cookies = await self.page.context.cookies("https://www.facebook.com/")
            except PlaywrightError as exc:
                if self.page.is_closed():
                    return "browser_closed"
                if not isinstance(exc, PlaywrightTimeout) and not _navigation_interrupted(exc):
                    raise
            else:
                # A redirect can commit after evaluate returns but before the
                # cookie read completes. Never classify mixed-document state.
                if (generation == self._document_generation
                        and probe["url"] == self.page.url
                        and probe["readyState"] != "loading"):
                    authenticated = any(cookie["name"] == "c_user" and cookie.get("value") for cookie in cookies)
                    return classify_safety(probe["url"], probe, authenticated, self._http_stop)
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
        raise StopCrawl("navigation_unstable", _STOP_MESSAGES["navigation_unstable"])

    async def check(self) -> None:
        reason = await self.inspect()
        if reason:
            raise StopCrawl(reason, _STOP_MESSAGES[reason])

    async def settle(self) -> None:
        remaining = self.action_delay - (time.monotonic() - self._last_action)
        if remaining > 0:
            await asyncio.sleep(remaining)
        # A minimum render interval is not an anti-detection mechanism. SPA
        # extraction additionally checks content progress after each action.
        await self.page.wait_for_timeout(250)
        await self.check()
        self._last_action = time.monotonic()

    async def goto(self, url: str) -> None:
        if not is_facebook_url(url):
            raise ValueError("Crawler navigation is restricted to Facebook HTTPS URLs.")
        for attempt in range(2):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await self.page.locator("body").wait_for(timeout=10000)
                await self.settle()
                return
            except (PlaywrightTimeout, PlaywrightError) as exc:
                if self.page.is_closed():
                    raise StopCrawl("browser_closed", _STOP_MESSAGES["browser_closed"]) from exc
                # A security response must never become a navigation retry.
                if is_facebook_url(self.page.url):
                    await self.check()
                if attempt:
                    raise
                await asyncio.sleep(2)

    async def manual_login(self, page_url: str, timeout: float = 600) -> None:
        """Observe manual login only; never fill/click login or security forms."""
        await self.page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.page.is_closed():
                raise StopCrawl("browser_closed", _STOP_MESSAGES["browser_closed"])
            # Login allows human verification to finish. A redirect or a new
            # login/checkpoint page is not a successful authenticated session.
            try:
                reason = await self.inspect(timeout=min(10.0, deadline - time.monotonic()))
                if reason is None:
                    await asyncio.sleep(min(1.0, max(0, deadline - time.monotonic())))
                    confirmed = await self.inspect(timeout=min(10.0, max(0, deadline - time.monotonic())))
                    if confirmed is not None:
                        continue
                    try:
                        await self.goto(page_url)
                    except StopCrawl as exc:
                        if exc.reason in {"auth_required", "checkpoint", "navigation_unstable"}:
                            continue
                        raise
                    return
            except StopCrawl as exc:
                if exc.reason != "navigation_unstable":
                    raise
            await asyncio.sleep(min(1.0, max(0, deadline - time.monotonic())))
        raise StopCrawl("auth_required", "Manual login did not finish before the timeout; no crawling was attempted.")


@asynccontextmanager
async def open_session(profile_dir: Path, action_delay: float = 2.0, channel: str | None = None, headless: bool = False):
    profile_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        profile_dir.chmod(0o700)
    async with async_playwright() as playwright:
        options = {"headless": headless, "viewport": {"width": 1365, "height": 900}}
        if channel:
            options["channel"] = channel
        context = await playwright.chromium.launch_persistent_context(str(profile_dir), **options)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            yield BrowserSession(page, action_delay)
        finally:
            await context.close()
