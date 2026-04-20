"""
LinkedIn Posts scraper using Patchright browser automation.

LinkedIn posts (not job listings) are a valuable source of job leads: people
publish "we're hiring a PHP dev" without creating a formal listing. Unlike
LinkedIn's job search API (which works unauthenticated), the content/posts
search is only available to logged-in users via the full SPA.

Research showed that LinkedIn's Voyager API returns `totalResultCount` for
CONTENT searches but all `entityResult` values come back as `null`. The HTML
is a pure SPA shell (762KB JS). The only working approach is Patchright
(a Playwright fork with CDP signal patches) to render the page in headless
Chrome and parse the DOM.

Architecture mirrors the Twitter scraper pattern: persistent Chrome profile,
fast-path session check, login if needed, then scrape. The difference is that
Twitter uses the browser only for login (then twscrape API for search),
whereas LinkedIn Posts uses the browser for everything because there is no
working API.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta, timezone

from jobspy.google.proxy_relay import ProxyRelay
from jobspy.linkedin_posts.constant import (
    FEED_URL,
    LOGIN_URL,
    MIN_POST_LENGTH,
    POST_ACTIVITY_LINK,
    POST_AUTHOR,
    POST_AUTHOR_SUBTITLE,
    POST_CONTAINER,
    POST_TEXT,
    POST_TIMESTAMP,
    SEARCH_URL,
)
from jobspy.model import (
    JobPost,
    JobResponse,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import extract_job_type

log = logging.getLogger("JobSpy:LinkedInPosts")


class LinkedInPosts(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        linkedin_accounts: str | list[dict] | None = None,
        linkedin_profile_dir: str | None = None,
    ):
        super().__init__(Site.LINKEDIN_POSTS, proxies=proxies, ca_cert=ca_cert)

        # Parse accounts: accept JSON string or list of dicts
        if isinstance(linkedin_accounts, str):
            try:
                self.accounts = json.loads(linkedin_accounts) if linkedin_accounts else []
            except json.JSONDecodeError:
                log.error("Invalid linkedin_accounts JSON: %s", linkedin_accounts)
                self.accounts = []
        elif isinstance(linkedin_accounts, list):
            self.accounts = linkedin_accounts
        else:
            self.accounts = []

        self.profile_dir = linkedin_profile_dir or os.path.expanduser(
            "~/.jobspy/linkedin/profiles"
        )

        # Resolve proxy URL
        self.proxy_url = None
        if proxies:
            if isinstance(proxies, list):
                self.proxy_url = proxies[0]
            else:
                self.proxy_url = proxies

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """Sync entry point — bridges to async via asyncio.run()."""
        return asyncio.run(self._scrape(scraper_input))

    async def _scrape(self, scraper_input: ScraperInput) -> JobResponse:
        if not self.accounts:
            log.error("No LinkedIn accounts configured — cannot scrape posts")
            return JobResponse(jobs=[])

        account = self.accounts[0]
        try:
            jobs = await asyncio.wait_for(
                asyncio.to_thread(self._browser_scrape, account, scraper_input),
                timeout=120,
            )
        except asyncio.TimeoutError:
            log.error("LinkedIn Posts browser scrape timed out after 120s")
            jobs = []
        except Exception as e:
            log.error("LinkedIn Posts scrape failed: %s", e)
            jobs = []

        log.info("Parsed %d job posts from LinkedIn Posts", len(jobs))
        return JobResponse(jobs=jobs)

    def _browser_scrape(
        self, account: dict, scraper_input: ScraperInput
    ) -> list[JobPost]:
        """Run the full browser flow: login (if needed) + scrape. Blocking."""
        try:
            from patchright.sync_api import sync_playwright
            lib_name = "patchright"
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
                lib_name = "playwright"
                log.warning(
                    "patchright not installed — falling back to stock playwright. "
                    "LinkedIn may detect automation; run: pip install patchright && patchright install chromium"
                )
            except ImportError as e:
                raise RuntimeError(
                    "Neither patchright nor playwright installed — "
                    "run: pip install patchright && patchright install chromium"
                ) from e

        username = account.get("username", "")
        password = account.get("password", "")
        proxy = account.get("proxy") or self.proxy_url

        if not username or not password:
            log.error("LinkedIn account missing username or password")
            return []

        headless = os.getenv("LINKEDIN_LOGIN_HEADLESS", "1") != "0"

        # Proxy relay for Chromium (can't do HTTP proxy auth for HTTPS CONNECT)
        relay = None
        proxy_arg = None
        if proxy:
            relay = ProxyRelay(upstream_proxy=proxy)
            relay.start()
            proxy_arg = {"server": f"http://127.0.0.1:{relay.port}"}
            log.info("Proxy relay listening on 127.0.0.1:%d", relay.port)

        user_data_dir = os.path.join(self.profile_dir, username)
        os.makedirs(user_data_dir, exist_ok=True)

        try:
            with sync_playwright() as p:
                launch_kwargs: dict = {
                    "channel": "chrome",
                    "headless": headless,
                    "no_viewport": True,
                }
                if proxy_arg:
                    launch_kwargs["proxy"] = proxy_arg

                log.info("Launching %s persistent context at %s", lib_name, user_data_dir)
                context = p.chromium.launch_persistent_context(
                    user_data_dir, **launch_kwargs
                )
                try:
                    page = context.pages[0] if context.pages else context.new_page()

                    # === Login or session check ===
                    self._ensure_logged_in(context, page, username, password)

                    # === Scrape ===
                    return self._scrape_posts(page, scraper_input)
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
        finally:
            if relay:
                relay.stop()

    def _ensure_logged_in(self, context, page, username: str, password: str) -> None:
        """Check for existing session; if not logged in, perform full login."""
        log.info("Checking for existing LinkedIn session")
        try:
            page.goto(FEED_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
        except Exception:
            pass

        jar = {c["name"]: c["value"] for c in context.cookies()}
        if "li_at" in jar and "/login" not in page.url:
            log.info("Profile already authenticated — skipping login for %s", username)
            return

        log.info("No existing session — starting login flow for %s", username)
        _debug_screenshot(page, "01_before_login", username)

        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(random.randint(1500, 3000))
        _debug_screenshot(page, "02_login_page", username)

        # Fill username
        log.info("Entering username")
        username_input = page.locator("#username")
        username_input.wait_for(state="visible", timeout=15000)
        username_input.click()
        page.wait_for_timeout(random.randint(200, 500))
        for ch in username:
            page.keyboard.type(ch)
            page.wait_for_timeout(random.randint(30, 100))

        page.wait_for_timeout(random.randint(300, 700))

        # Fill password
        log.info("Entering password")
        password_input = page.locator("#password")
        password_input.click()
        page.wait_for_timeout(random.randint(200, 500))
        for ch in password:
            page.keyboard.type(ch)
            page.wait_for_timeout(random.randint(30, 100))

        page.wait_for_timeout(random.randint(300, 700))
        _debug_screenshot(page, "03_credentials_entered", username)

        # Submit
        page.keyboard.press("Enter")
        log.info("Submitted login form")
        page.wait_for_timeout(random.randint(3000, 5000))
        _debug_screenshot(page, "04_after_submit", username)

        # CAPTCHA / challenge: manual intervention
        manual = os.getenv("LINKEDIN_LOGIN_MANUAL_CODE") == "1"
        if manual and ("/checkpoint" in page.url or "/challenge" in page.url):
            wait_s = int(os.getenv("LINKEDIN_LOGIN_MANUAL_TIMEOUT", "300"))
            log.info(
                "Challenge detected — manual mode. Please solve in the browser window. "
                "Waiting up to %ds...", wait_s
            )
            deadline = datetime.now(timezone.utc).timestamp() + wait_s
            while datetime.now(timezone.utc).timestamp() < deadline:
                jar = {c["name"]: c["value"] for c in context.cookies()}
                if "li_at" in jar:
                    log.info("Challenge cleared (li_at cookie appeared)")
                    break
                current_url = page.url
                if "/feed" in current_url and "/login" not in current_url:
                    log.info("Challenge cleared (redirected to feed)")
                    break
                page.wait_for_timeout(1000)
            _debug_screenshot(page, "05_after_challenge", username)

        # Wait for login to settle
        log.info("Waiting for login to settle")
        for _ in range(30):
            url = page.url
            if "/feed" in url and "/login" not in url:
                break
            page.wait_for_timeout(1000)

        jar = {c["name"]: c["value"] for c in context.cookies()}
        if "li_at" not in jar:
            _debug_screenshot(page, "99_login_failed", username)
            raise RuntimeError(
                f"LinkedIn login did not produce li_at cookie. "
                f"URL: {page.url}. Cookies: {sorted(jar.keys())}"
            )

        log.info("Successfully logged in as %s", username)

    def _scrape_posts(
        self, page, scraper_input: ScraperInput
    ) -> list[JobPost]:
        """Navigate to content search and parse post DOM."""
        query = scraper_input.search_term or ""
        limit = scraper_input.results_wanted

        params = f"keywords={query}&sortBy=date_posted"
        if scraper_input.location:
            params += f"&origin=FACETED_SEARCH"
        url = f"{SEARCH_URL}?{params}"

        log.info("Navigating to search: %s", url)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(random.randint(2000, 4000))

        _debug_screenshot(page, "10_search_results", "scrape")

        # Wait for post containers to appear
        try:
            page.wait_for_selector(POST_CONTAINER, timeout=15000)
        except Exception:
            log.warning("No post containers found — page may not have loaded")
            _debug_screenshot(page, "11_no_posts", "scrape")
            return []

        # Scroll to load more posts (infinite scroll)
        scroll_count = 5
        for i in range(scroll_count):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(random.randint(1500, 3000))
            current_count = page.locator(POST_CONTAINER).count()
            log.info("Scroll %d/%d — %d posts visible", i + 1, scroll_count, current_count)
            if current_count >= limit * 2:
                break

        _debug_screenshot(page, "12_after_scroll", "scrape")

        # Parse posts from DOM
        posts = page.locator(POST_CONTAINER)
        count = posts.count()
        log.info("Found %d post containers to parse", count)

        jobs: list[JobPost] = []
        for i in range(count):
            if len(jobs) >= limit:
                break
            try:
                post = posts.nth(i)
                job = self._parse_post(post, page)
                if job:
                    jobs.append(job)
            except Exception as e:
                log.debug("Failed to parse post %d: %s", i, e)

        return jobs

    def _parse_post(self, post, page) -> JobPost | None:
        """Extract a JobPost from a single post DOM element."""
        # Extract post text
        text = ""
        try:
            text_el = post.locator(POST_TEXT).first
            if text_el.count() > 0:
                text = text_el.inner_text()
        except Exception:
            pass

        if not text:
            # Fallback: try getting all text from the description area
            try:
                text = post.locator("div.feed-shared-update-v2__description").first.inner_text()
            except Exception:
                pass

        if len(text) < MIN_POST_LENGTH:
            return None

        # Extract author name
        author = ""
        try:
            author_el = post.locator(POST_AUTHOR).first
            if author_el.count() > 0:
                author = author_el.inner_text().strip()
        except Exception:
            pass

        # Extract author subtitle (often contains company/title info)
        author_subtitle = ""
        try:
            subtitle_el = post.locator(POST_AUTHOR_SUBTITLE).first
            if subtitle_el.count() > 0:
                author_subtitle = subtitle_el.inner_text().strip()
        except Exception:
            pass

        # Extract activity URL (urn:li:activity:*)
        post_url = ""
        try:
            link_el = post.locator(POST_ACTIVITY_LINK).first
            if link_el.count() > 0:
                post_url = link_el.get_attribute("href") or ""
                if post_url and not post_url.startswith("http"):
                    post_url = f"https://www.linkedin.com{post_url}"
        except Exception:
            pass

        if not post_url:
            # Try data-urn attribute
            try:
                urn = post.get_attribute("data-urn") or ""
                if "activity" in urn:
                    activity_id = urn.split(":")[-1]
                    post_url = f"https://www.linkedin.com/feed/update/urn:li:activity:{activity_id}/"
            except Exception:
                pass

        # Extract title from first line or regex
        title = self._extract_title(text)
        if not title:
            return None

        # Extract company from author subtitle or text
        company = self._extract_company(text, author, author_subtitle)

        # Determine date
        date_posted = self._extract_date(post)

        # Location from text
        location = self._extract_location(text)

        # Remote detection
        remote = _is_remote(text)

        # Job type
        job_types = extract_job_type(text)

        return JobPost(
            title=title,
            company_name=company,
            job_url=post_url or "",
            location=location,
            description=text,
            date_posted=date_posted,
            is_remote=remote if remote else None,
            job_type=job_types if job_types else None,
        )

    @staticmethod
    def _extract_title(text: str) -> str | None:
        """Extract a job title from post text."""
        patterns = [
            r"(?:hiring|looking for|we need|open position|job opening|now hiring)[:\s]+(.+?)(?:\n|$)",
            r"^(.+?)\s+(?:needed|wanted|required)",
            r"(?:role|position)[:\s]+(.+?)(?:\n|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                title = match.group(1).strip()
                title = re.sub(r"\s*#\S+", "", title).strip()
                if len(title) > 5:
                    return title[:150]

        # Fallback: first non-empty line
        for line in text.split("\n"):
            line = line.strip()
            if len(line) > 10:
                cleaned = re.sub(r"^[#@\s]+", "", line).strip()
                if cleaned:
                    return cleaned[:100]
        return None

    @staticmethod
    def _extract_company(text: str, author: str, subtitle: str) -> str:
        """Extract company name from post context."""
        # Try subtitle (often "Title at Company")
        if subtitle:
            at_match = re.search(r"\bat\s+(.+?)(?:\s*[|·•]|$)", subtitle, re.IGNORECASE)
            if at_match:
                return at_match.group(1).strip()[:100]

        # Try text patterns
        company_patterns = [
            r"(?:at|@|company)[:\s]+(.+?)(?:\n|$|\||#)",
            r"(?:join|work (?:at|for))\s+(.+?)(?:\n|$|\||#|!)",
        ]
        for pattern in company_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                company = match.group(1).strip()
                company = re.sub(r"\s*#\S+", "", company).strip()
                if len(company) > 1:
                    return company[:100]

        return author

    @staticmethod
    def _extract_date(post) -> date | None:
        """Try to extract post date from DOM."""
        try:
            time_el = post.locator(POST_TIMESTAMP).first
            if time_el.count() > 0:
                datetime_attr = time_el.get_attribute("datetime")
                if datetime_attr:
                    return datetime.fromisoformat(datetime_attr.replace("Z", "+00:00")).date()

                # Fallback: parse relative time text like "3d", "1w", "2h"
                time_text = time_el.inner_text().strip().lower()
                return _parse_relative_time(time_text)
        except Exception:
            pass
        return None

    @staticmethod
    def _extract_location(text: str) -> Location | None:
        """Extract location from post text."""
        patterns = [
            r"(?:location|based in|located in|office in)[:\s]+(.+?)(?:\n|$|\|)",
            r"\U0001F4CD\s*(.+?)(?:\n|$)",  # pin emoji
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                loc = match.group(1).strip()
                if loc:
                    return Location(city=loc[:100])
        return None


def _is_remote(text: str) -> bool:
    """Check if the post indicates a remote position."""
    text_lower = text.lower()
    keywords = ["remote", "wfh", "work from home", "work-from-home", "fully remote", "100% remote"]
    return any(kw in text_lower for kw in keywords)


def _parse_relative_time(text: str) -> date | None:
    """Parse LinkedIn relative timestamps like '3d', '1w', '2h' into a date."""
    match = re.match(r"(\d+)\s*([hdwmo])", text)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2)
    now = datetime.now(timezone.utc)
    if unit == "h":
        return now.date()
    elif unit == "d":
        return (now - timedelta(days=value)).date()
    elif unit == "w":
        return (now - timedelta(weeks=value)).date()
    elif unit == "m" or unit == "o":
        return (now - timedelta(days=value * 30)).date()
    return None


def _debug_screenshot(page, step: str, username: str) -> None:
    """Save a screenshot if LINKEDIN_LOGIN_DEBUG is set."""
    if not os.getenv("LINKEDIN_LOGIN_DEBUG"):
        return
    try:
        path = f"/tmp/li_posts_{username}_{step}.png"
        page.screenshot(path=path, full_page=True)
        log.info("Debug screenshot saved: %s", path)
    except Exception:
        pass
