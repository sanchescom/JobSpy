"""Patchright-based Twitter/X search scraper (DOM, no twscrape HTTP API).

X migrated its web frontend to ``/x-web/x-web/`` (mid-2026), which broke the
HTTP ``x-client-transaction-id`` signing that twscrape relies on — every
GraphQL search now fails. Logged-out users can't see search results at all
(X redirects to onboarding), so there is no guest HTTP path either.

This module sidesteps the whole signing problem by letting a real logged-in
browser render the search results and reading tweets straight out of the DOM.
It reuses the same persistent Chrome profile and login flow as
``browser_login`` (via :func:`ensure_logged_in`), so X's own JS computes every
anti-bot header for us.
"""

from __future__ import annotations

import logging
import os
import random
import urllib.parse

from jobspy.google.proxy_relay import ProxyRelay
from jobspy.twitter.browser_login import BrowserLoginError, ensure_logged_in

log = logging.getLogger("JobSpy:Twitter:BrowserSearch")


# Pulls the fields we need out of every tweet article currently in the DOM.
# Returns a list of plain dicts (JSON-serializable) keyed by tweet id.
_EXTRACT_TWEETS_JS = r"""
() => {
    const out = [];
    for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
        try {
            const textEl = art.querySelector('[data-testid="tweetText"]');
            const text = textEl ? textEl.innerText : "";

            // permalink + id: the status link wrapping the timestamp
            let permalink = "", id = "", iso = "";
            const timeEl = art.querySelector('time[datetime]');
            if (timeEl) {
                iso = timeEl.getAttribute('datetime') || "";
                const a = timeEl.closest('a[href*="/status/"]');
                if (a) {
                    permalink = a.href;
                    const m = a.href.match(/\/status\/(\d+)/);
                    if (m) id = m[1];
                }
            }
            if (!id) {
                const a = art.querySelector('a[href*="/status/"]');
                if (a) {
                    permalink = a.href;
                    const m = a.href.match(/\/status\/(\d+)/);
                    if (m) id = m[1];
                }
            }

            // author display name + @handle
            let name = "", handle = "";
            const nameEl = art.querySelector('[data-testid="User-Name"]');
            if (nameEl) {
                const txt = nameEl.innerText || "";
                const at = txt.match(/@([A-Za-z0-9_]+)/);
                if (at) handle = at[1];
                name = (txt.split("@")[0] || "").replace(/\n/g, " ").trim();
            }
            if (!handle && permalink) {
                const hm = permalink.match(/(?:x|twitter)\.com\/([A-Za-z0-9_]+)\/status/);
                if (hm) handle = hm[1];
            }

            // external links: real URLs X shows as link text (href is a t.co shim)
            const links = [];
            for (const a of art.querySelectorAll('a[href]')) {
                const href = a.href || "";
                const label = (a.innerText || "").trim();
                if (/^https?:\/\//.test(href) && !/(twitter|x)\.com|t\.co/.test(href)) {
                    links.push({href, text: label});
                } else if (/\.[a-z]{2,}\//i.test(label) || /^https?:\/\//.test(label)) {
                    // displayed text looks like a URL even if href is a t.co shim
                    links.push({href, text: label});
                }
            }

            if (id && text) out.push({id, handle, name, text, iso, permalink, links});
        } catch (e) { /* skip malformed article */ }
    }
    return out;
}
"""


def _build_search_url(query: str, latest: bool = True) -> str:
    q = urllib.parse.quote(query)
    url = f"https://x.com/search?q={q}&src=typed_query"
    if latest:
        url += "&f=live"
    return url


def search_tweets_via_browser(
    query: str,
    limit: int,
    account: dict,
    proxy: str | None = None,
    headless: bool | None = None,
    profile_root: str | None = None,
) -> list[dict]:
    """Run an X search in a logged-in browser and return raw tweet dicts.

    Each dict: ``{id, handle, name, text, iso, permalink, links}``.
    Returns ``[]`` if login fails or no tweets are found. Raises only on
    unrecoverable browser setup errors.
    """
    try:
        from patchright.sync_api import sync_playwright

        lib_name = "patchright"
    except ImportError:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore

            lib_name = "playwright"
            log.warning("patchright not installed — falling back to stock playwright")
        except ImportError as e:
            raise BrowserLoginError(
                "Neither patchright nor playwright installed — "
                "run: pip install patchright && patchright install chromium"
            ) from e

    username = account.get("username", "")
    password = account.get("password", "")
    email = account.get("email") or None
    email_password = account.get("email_password") or None
    proxy = account.get("proxy") or proxy

    if not username or not password:
        log.error("Twitter account missing username/password")
        return []

    if headless is None:
        headless = os.getenv("TWITTER_LOGIN_HEADLESS", "1") != "0"

    profile_root = profile_root or os.getenv(
        "TWITTER_LOGIN_PROFILE_DIR",
        os.path.expanduser("~/.jobspy/twitter/profiles"),
    )
    user_data_dir = os.path.join(profile_root, username)
    os.makedirs(user_data_dir, exist_ok=True)

    relay = None
    proxy_arg = None
    if proxy:
        relay = ProxyRelay(upstream_proxy=proxy)
        relay.start()
        proxy_arg = {"server": f"http://127.0.0.1:{relay.port}"}
        log.info("Proxy relay listening on 127.0.0.1:%d", relay.port)

    try:
        with sync_playwright() as p:
            launch_kwargs: dict = {
                "channel": "chrome",
                "headless": headless,
                "no_viewport": True,
                # Tall window so login fields / tweets aren't below the fold
                # (default headless window is too short for visibility checks).
                "args": ["--window-size=1280,1024"],
            }
            if proxy_arg:
                launch_kwargs["proxy"] = proxy_arg

            log.info("Launching %s persistent context at %s", lib_name, user_data_dir)
            context = p.chromium.launch_persistent_context(user_data_dir, **launch_kwargs)
            try:
                page = context.pages[0] if context.pages else context.new_page()

                # Reuse the shared login flow: fast-path if the profile is
                # already authenticated, otherwise full credential login.
                try:
                    ensure_logged_in(
                        context, page, username, password,
                        email=email, email_password=email_password,
                        auth_token=account.get("auth_token"),
                        ct0=account.get("ct0"),
                    )
                except BrowserLoginError as e:
                    log.error("Login failed for %s: %s", username, e)
                    return []

                url = _build_search_url(query, latest=True)
                log.info("Navigating to search: %s", url)
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_selector(
                        'article[data-testid="tweet"]', timeout=20000
                    )
                except Exception:
                    log.warning("No tweet articles appeared for query: %s", query)
                    return []

                # Scroll to lazy-load more tweets until we have enough or the
                # page stops growing.
                seen: dict[str, dict] = {}
                stale_rounds = 0
                max_scrolls = 40
                for i in range(max_scrolls):
                    for t in page.evaluate(_EXTRACT_TWEETS_JS):
                        if t["id"] not in seen:
                            seen[t["id"]] = t
                    log.info("Scroll %d: %d unique tweets so far", i + 1, len(seen))
                    if len(seen) >= limit:
                        break
                    before = len(seen)
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(random.randint(1200, 2200))
                    # collect again post-scroll to measure growth
                    for t in page.evaluate(_EXTRACT_TWEETS_JS):
                        if t["id"] not in seen:
                            seen[t["id"]] = t
                    if len(seen) == before:
                        stale_rounds += 1
                        if stale_rounds >= 3:
                            log.info("No new tweets after %d stale scrolls — stopping", stale_rounds)
                            break
                    else:
                        stale_rounds = 0

                tweets = list(seen.values())[: limit * 3]
                log.info("Collected %d tweets from DOM for query: %s", len(tweets), query)
                return tweets
            finally:
                try:
                    context.close()
                except Exception:
                    pass
    finally:
        if relay:
            relay.stop()
