"""
Patchright-based Twitter/X login.

Twitter's HTTP login API (used by twscrape) is blocked by anti-automation
heuristics that return error 399 even with valid credentials. A real browser
session bypasses these checks. This module launches Chrome via `patchright`
(a drop-in Playwright fork that patches the CDP/runtime signals Twitter
fingerprints), performs the full login flow (including email verification
via IMAP), and returns the auth_token + ct0 cookies that twscrape needs.

Stock Playwright — even with `playwright-stealth`, `ignore_default_args=
["--enable-automation"]`, and `--disable-blink-features=AutomationControlled`
— is detected by x.com's client-side JS: the login form silently resets on
submit. `patchright` patches the leaks at the library level (not at runtime
via JS injection) and is required for this flow to succeed.

Cookies are injected directly into twscrape's AccountsPool, which
auto-activates any account whose cookies contain ``ct0``, so twscrape's
own login step is skipped entirely.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone

from jobspy.google.proxy_relay import ProxyRelay

log = logging.getLogger("JobSpy:Twitter:BrowserLogin")


LOGIN_URL = "https://x.com/i/flow/login"


class BrowserLoginError(Exception):
    """Raised when browser-based Twitter login fails."""


def _dismiss_cookie_banner(page) -> None:
    """Click 'Refuse non-essential cookies' on the EU consent banner if shown."""
    for text in ("Refuse non-essential cookies", "Accept all cookies"):
        try:
            btn = page.get_by_role("button", name=text, exact=False).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                page.wait_for_timeout(random.randint(500, 1000))
                log.info(f"Dismissed cookie banner via: {text}")
                return
        except Exception:
            continue


_FOCUS_ON_TOP_JS = """
(selectors) => {
    function isOnTop(el) {
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) return false;
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') return false;
        const cx = r.left + r.width / 2;
        const cy = r.top + r.height / 2;
        if (cx < 0 || cy < 0 || cx > window.innerWidth || cy > window.innerHeight) return false;
        const topEl = document.elementFromPoint(cx, cy);
        if (!topEl) return false;
        return topEl === el || el.contains(topEl) || topEl.contains(el);
    }
    for (const sel of selectors) {
        for (const el of document.querySelectorAll(sel)) {
            if (isOnTop(el)) {
                el.scrollIntoView({ block: 'center', behavior: 'instant' });
                el.focus();
                return true;
            }
        }
    }
    return false;
}
"""


def _focus_visible_input(page, selectors: list[str], timeout_ms: int = 45000) -> bool:
    """
    Focus the first input matching one of the selectors that is actually the
    topmost element at its own center point. Returns True on success.

    Using elementFromPoint is the only reliable way to distinguish the modal's
    input from the same-selector input in the dimmed background page.
    """
    import time as _t

    deadline = _t.time() + timeout_ms / 1000
    while _t.time() < deadline:
        try:
            if page.evaluate(_FOCUS_ON_TOP_JS, selectors):
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    return False


def _type_into_focused(page, text: str) -> None:
    """Type text into whatever element currently has focus, with a per-char delay."""
    for ch in text:
        page.keyboard.type(ch)
        page.wait_for_timeout(random.randint(40, 130))


def _get_email_code(
    email: str, email_password: str, min_t: datetime, timeout: int = 90
) -> str | None:
    """
    Fetch a Twitter confirmation code from the mailbox.

    Runs in a dedicated thread with its own fresh event loop — patchright's
    sync_api keeps an event loop on the calling thread, so a direct
    ``asyncio.run()`` here would raise "cannot be called from a running
    event loop".
    """
    import concurrent.futures

    from twscrape.imap import imap_get_email_code, imap_login

    async def _inner():
        imap = await imap_login(email, email_password)
        try:
            return await imap_get_email_code(imap, email, min_t=min_t)
        finally:
            try:
                imap.close()
            except Exception:
                pass
            try:
                imap.logout()
            except Exception:
                pass

    # imap_get_email_code respects TWS_WAIT_EMAIL_CODE env var for timeout.
    os.environ.setdefault("TWS_WAIT_EMAIL_CODE", str(timeout))

    def _run_in_thread():
        return asyncio.run(_inner())

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            # Add headroom to the outer timeout so the inner IMAP poll can complete.
            return ex.submit(_run_in_thread).result(timeout=timeout + 30)
    except Exception as e:
        log.warning(f"IMAP email code fetch failed: {e}")
        return None


def _debug_screenshot(page, step: str, username: str) -> None:
    """Save a screenshot if TWITTER_LOGIN_DEBUG is set."""
    if not os.getenv("TWITTER_LOGIN_DEBUG"):
        return
    try:
        path = f"/tmp/tw_login_{username}_{step}.png"
        page.screenshot(path=path, full_page=True)
        log.info(f"Debug screenshot saved: {path}")
    except Exception:
        pass


def login_via_browser(
    username: str,
    password: str,
    email: str | None = None,
    email_password: str | None = None,
    proxy: str | None = None,
    headless: bool | None = None,
    user_agent: str | None = None,  # kept for backward-compat; ignored by patchright
) -> dict[str, str]:
    """
    Launch a real browser, perform full Twitter login, return cookies.

    Returns a dict containing at minimum ``auth_token`` and ``ct0``.
    Raises :class:`BrowserLoginError` on any failure.
    """
    # Patchright is a drop-in Playwright fork that patches the CDP/runtime
    # signals x.com fingerprints. Stock Playwright (even with stealth patches)
    # is detected and the login form silently resets on submit.
    try:
        from patchright.sync_api import sync_playwright
        lib_name = "patchright"
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
            lib_name = "playwright"
            log.warning(
                "patchright not installed — falling back to stock playwright. "
                "Twitter will likely reject the login form; run: pip install patchright && patchright install chromium"
            )
        except ImportError as e:
            raise BrowserLoginError(
                "Neither patchright nor playwright installed — run: pip install patchright && patchright install chromium"
            ) from e

    if headless is None:
        headless = os.getenv("TWITTER_LOGIN_HEADLESS", "1") != "0"

    relay = None
    proxy_arg = None
    if proxy:
        # Chromium can't do HTTP proxy auth for HTTPS CONNECT; use the
        # same local relay as the Google scraper.
        relay = ProxyRelay(upstream_proxy=proxy)
        relay.start()
        proxy_arg = {"server": f"http://127.0.0.1:{relay.port}"}
        log.info(f"Proxy relay listening on 127.0.0.1:{relay.port}")

    # Use a persistent, per-username user_data_dir. Twitter treats each Chrome
    # profile as a distinct "device": a fresh profile on every run triggers
    # email-verification challenges repeatedly, which defeats the whole point
    # of automation. Reusing the profile means after the first successful
    # login the browser stays logged in and subsequent runs just scrape the
    # auth_token/ct0 cookies straight out of the jar.
    profile_root = os.getenv(
        "TWITTER_LOGIN_PROFILE_DIR",
        os.path.expanduser("~/.jobspy/twitter/profiles"),
    )
    user_data_dir = os.path.join(profile_root, username)
    os.makedirs(user_data_dir, exist_ok=True)

    try:
        with sync_playwright() as p:
            launch_kwargs: dict = {
                # channel="chrome" uses the real Google Chrome binary, not
                # the Playwright-bundled Chromium which has additional
                # detectable signals.
                "channel": "chrome",
                "headless": headless,
                # Per patchright docs: do NOT customize user_agent or viewport
                # — those are the two strongest fingerprint leaks. Let Chrome
                # report its own real values.
                "no_viewport": True,
            }
            if proxy_arg:
                launch_kwargs["proxy"] = proxy_arg

            log.info(f"Launching {lib_name} persistent context at {user_data_dir}")
            context = p.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
            try:
                # Persistent context auto-creates a page; reuse it.
                page = context.pages[0] if context.pages else context.new_page()

                # Fast-path: if the persistent profile already has a valid
                # session, navigating to x.com/home will succeed without
                # redirecting to /i/flow/login. In that case we just read
                # auth_token + ct0 and skip the entire login flow.
                log.info("Checking for an existing authenticated session")
                try:
                    page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

                jar = {c["name"]: c["value"] for c in context.cookies()}
                if "auth_token" in jar and "ct0" in jar and "i/flow/login" not in page.url:
                    log.info(f"Profile already authenticated — skipping login flow for {username}")
                    cookie_map = {
                        c["name"]: c["value"]
                        for c in context.cookies()
                        if c.get("domain", "").endswith("x.com")
                        or c.get("domain", "").endswith("twitter.com")
                    }
                    return cookie_map

                log.info(f"No existing session — starting login flow for {username}")
                log.info(f"Navigating to {LOGIN_URL} for {username}")
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                # Wait for the login modal to be fully hydrated by the SPA:
                # the /i/flow/login page overlays a modal on top of x.com's
                # homepage, and that homepage has its own form with the same
                # selectors. We have to wait for the modal's inputs to actually
                # exist in the DOM or we'll type into the background form.
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(random.randint(2000, 3500))

                # Dismiss the EU cookie consent banner if present — its
                # presence can interfere with subsequent keyboard events.
                _dismiss_cookie_banner(page)

                _debug_screenshot(page, "01_loaded", username)

                # Step 1: username — use elementFromPoint-based focusing to
                # target the input that is *actually on top* at its own
                # center (the modal's input, not the dimmed background).
                log.info("Entering username")
                if not _focus_visible_input(
                    page,
                    [
                        'input[autocomplete="username"]',
                        'input[name="text"]',
                    ],
                    timeout_ms=45000,
                ):
                    _debug_screenshot(page, "ERR_no_username_input", username)
                    raise BrowserLoginError("Could not locate visible username input in modal")
                page.wait_for_timeout(random.randint(200, 500))
                _type_into_focused(page, username)
                _debug_screenshot(page, "02a_after_typing_username", username)
                page.wait_for_timeout(random.randint(500, 1200))
                page.keyboard.press("Enter")
                page.wait_for_timeout(random.randint(2500, 4500))
                _debug_screenshot(page, "02b_after_username_enter", username)

                # Step 2: optional email/phone challenge (appears when Twitter
                # can't uniquely identify the account from the username alone)
                if _focus_visible_input(
                    page,
                    ['input[data-testid="ocfEnterTextTextInput"]'],
                    timeout_ms=3000,
                ):
                    log.info("Alt-identifier challenge: entering email")
                    page.wait_for_timeout(random.randint(200, 500))
                    _type_into_focused(page, email or username)
                    page.wait_for_timeout(random.randint(500, 1200))
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(random.randint(2500, 4500))
                    _debug_screenshot(page, "03_after_alt", username)

                # Step 3: password
                log.info("Entering password")
                if not _focus_visible_input(
                    page,
                    [
                        'input[autocomplete="current-password"]',
                        'input[name="password"]',
                        'input[type="password"]',
                    ],
                    timeout_ms=45000,
                ):
                    _debug_screenshot(page, "ERR_no_password_input", username)
                    raise BrowserLoginError("Could not locate visible password input in modal")
                page.wait_for_timeout(random.randint(200, 500))
                _type_into_focused(page, password)
                page.wait_for_timeout(random.randint(500, 1200))
                submit_time = datetime.now(timezone.utc)
                page.keyboard.press("Enter")
                page.wait_for_timeout(random.randint(3000, 6000))
                _debug_screenshot(page, "04_after_password", username)

                # Step 4a: post-password "confirm your email" step — Twitter
                # sometimes asks the user to re-type their email BEFORE sending
                # the verification code. Same ocfEnterTextTextInput selector as
                # the code step, so distinguish by reading the input's
                # inputmode attribute: numeric → code, anything else → email.
                def _current_ocf_input_mode() -> str | None:
                    try:
                        return page.evaluate(
                            """() => {
                                const el = document.querySelector(
                                    'input[data-testid=\"ocfEnterTextTextInput\"]'
                                );
                                if (!el) return null;
                                return el.getAttribute('inputmode') || el.type || 'text';
                            }"""
                        )
                    except Exception:
                        return None

                if _focus_visible_input(
                    page,
                    ['input[data-testid="ocfEnterTextTextInput"]'],
                    timeout_ms=3000,
                ):
                    mode = _current_ocf_input_mode()
                    log.info(f"Post-password challenge detected (inputmode={mode})")

                    # Email-confirmation step (inputmode != numeric): auto-type
                    # the email if we have one, submit, and then check again
                    # for a follow-up code-entry step.
                    if mode != "numeric" and email:
                        log.info("Auto-filling email for confirmation step")
                        page.wait_for_timeout(random.randint(200, 500))
                        _type_into_focused(page, email)
                        page.wait_for_timeout(random.randint(500, 1200))
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(random.randint(2500, 4500))
                        _debug_screenshot(page, "04b_after_email_confirm", username)

                        # After submitting email, Twitter should show the code-
                        # entry field with inputmode=numeric. Re-focus so the
                        # rest of the flow can proceed uniformly.
                        _focus_visible_input(
                            page,
                            ['input[data-testid="ocfEnterTextTextInput"]'],
                            timeout_ms=10000,
                        )
                        mode = _current_ocf_input_mode()
                        log.info(f"After email confirm, inputmode={mode}")

                    log.info("Email verification challenge detected")
                    manual = os.getenv("TWITTER_LOGIN_MANUAL_CODE") == "1"

                    if manual:
                        # Manual mode: user submits the code themselves in the
                        # headed browser window. We just wait for the challenge
                        # to clear — either the URL leaves /i/flow/login, or
                        # auth_token cookie appears in the jar.
                        wait_s = int(os.getenv("TWITTER_LOGIN_MANUAL_TIMEOUT", "300"))
                        log.info(
                            f"Manual code entry mode — please enter the code from your email "
                            f"in the browser window. Waiting up to {wait_s}s..."
                        )
                        # In manual mode the ONLY reliable signal of success is
                        # the auth_token cookie appearing — URL might leave
                        # /i/flow/login for all sorts of reasons (user clicks
                        # the X logo, navigates home, closes the modal) without
                        # actually completing the challenge.
                        deadline = datetime.now(timezone.utc).timestamp() + wait_s
                        last_log = 0.0
                        while datetime.now(timezone.utc).timestamp() < deadline:
                            try:
                                jar = {c["name"]: c["value"] for c in context.cookies()}
                                if "auth_token" in jar:
                                    log.info("Challenge cleared (auth_token cookie appeared)")
                                    break
                            except Exception:
                                pass
                            # Heartbeat every 30s so the user knows we're alive.
                            now = datetime.now(timezone.utc).timestamp()
                            if now - last_log > 30:
                                remaining = int(deadline - now)
                                log.info(f"Still waiting for manual code entry ({remaining}s left)")
                                last_log = now
                            page.wait_for_timeout(1000)
                        _debug_screenshot(page, "05_after_manual_verification", username)
                    else:
                        # Automated mode: fetch code from IMAP.
                        if not (email and email_password):
                            raise BrowserLoginError(
                                "Twitter requested email verification but no email/email_password configured "
                                "(or set TWITTER_LOGIN_MANUAL_CODE=1 for manual entry)"
                            )
                        code = _get_email_code(email, email_password, submit_time, timeout=90)
                        if not code:
                            raise BrowserLoginError("Failed to retrieve email verification code from IMAP")
                        log.info(f"Submitting verification code: {code[:2]}***")
                        # Re-focus the input in case it lost focus while we waited for IMAP.
                        _focus_visible_input(
                            page,
                            ['input[data-testid="ocfEnterTextTextInput"]'],
                            timeout_ms=5000,
                        )
                        page.wait_for_timeout(random.randint(200, 500))
                        _type_into_focused(page, code)
                        page.wait_for_timeout(random.randint(500, 1200))
                        page.keyboard.press("Enter")
                        page.wait_for_timeout(random.randint(3000, 6000))
                        _debug_screenshot(page, "05_after_verification", username)

                # Step 5: wait for home redirect or cookie arrival
                log.info("Waiting for login to settle")
                for _ in range(30):
                    url = page.url
                    if (
                        "/home" in url
                        or url.rstrip("/") in ("https://x.com", "https://twitter.com")
                        or "i/flow/login" not in url
                    ):
                        break
                    page.wait_for_timeout(1000)

                cookies = context.cookies()
                cookie_map = {
                    c["name"]: c["value"]
                    for c in cookies
                    if c.get("domain", "").endswith("x.com")
                    or c.get("domain", "").endswith("twitter.com")
                }

                if "auth_token" not in cookie_map or "ct0" not in cookie_map:
                    _debug_screenshot(page, "99_final_missing_cookies", username)
                    raise BrowserLoginError(
                        f"Login did not produce auth_token/ct0. "
                        f"Cookies: {sorted(cookie_map.keys())}. URL: {page.url}"
                    )

                log.info(f"Successfully extracted auth_token and ct0 for {username}")
                return cookie_map
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    finally:
        if relay:
            relay.stop()
        # We intentionally do NOT delete user_data_dir — it's the persistent
        # Chrome profile keyed by username. Deleting it would force a fresh
        # email-verification challenge on every run.


def cookies_to_header(cookies: dict[str, str]) -> str:
    """Serialize a cookie dict into a single ``name=value; name=value`` string."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())
