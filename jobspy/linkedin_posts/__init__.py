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
import fcntl
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
from jobspy.twitter.util import (
    extract_country_from_tweet,
    extract_location_from_tweet,
)
from jobspy.util import extract_job_type

log = logging.getLogger("JobSpy:LinkedInPosts")


# JavaScript that runs inside the browser to extract posts from LinkedIn's
# content search results.  LinkedIn uses hashed/obfuscated CSS class names
# that change on every build, so we cannot rely on stable selectors.
# Instead we:
#   1. Walk down from <main> to find a container whose children repeat with
#      the same tag (the post list).
#   2. For each child (post), split innerText on the "Публикация в ленте"
#      / "Feed post" header that LinkedIn prepends (screen-reader text),
#      and extract author, subtitle, time, body text, and any linkedin.com
#      activity links.
_EXTRACT_POSTS_JS = """
() => {
    // ---- Step 1: find the repeating post container ----
    // LinkedIn uses hashed CSS class names, so we walk down from <main>
    // looking for a parent with the MOST same-tag children (the post list).
    const main = document.querySelector('main');
    if (!main) return [];

    let bestGroup = null;
    let bestCount = 0;

    function findRepeating(el, depth) {
        if (depth > 15) return;
        const childTags = {};
        for (const child of el.children) {
            const cls = typeof child.className === 'string' ? child.className : '';
            const key = child.tagName + '.' + cls.split(' ')[0];
            if (!childTags[key]) childTags[key] = [];
            childTags[key].push(child);
        }
        for (const [, group] of Object.entries(childTags)) {
            const withText = group.filter(c => c.innerText && c.innerText.length > 100);
            if (withText.length > bestCount) {
                bestCount = withText.length;
                bestGroup = withText;
            }
        }
        for (const child of el.children) {
            findRepeating(child, depth + 1);
        }
    }
    findRepeating(main, 0);

    if (!bestGroup || bestGroup.length < 1) return [];

    // ---- Step 2: extract structured data from each post element ----
    const posts = [];
    for (const el of bestGroup) {
        const text = el.innerText || '';
        if (text.length < 50) continue;

        // Skip footer/nav elements
        if (/^(О компании|About|Справочный|Help Center|Условия|Terms|© LinkedIn)/i.test(text.trim())) continue;

        const lines = text.split('\\n').map(l => l.trim()).filter(Boolean);

        // Find author (first substantial line after preamble)
        let author = '';
        let subtitle = '';
        let timeStr = '';
        let bodyStart = 0;

        // Pattern: header → author → connection info → subtitle → time → body
        for (let i = 0; i < Math.min(lines.length, 12); i++) {
            const line = lines[i];
            // Skip preamble headers
            if (/^(Публикация в ленте|Feed post|Promoted|Рекламируется)/i.test(line)) continue;
            // Skip action buttons
            if (/^(Отслеживать|Follow|\\+\\s*Отслеживать|\\+\\s*Follow)/i.test(line)) continue;
            // Skip connection degree markers
            if (/^[•·]/.test(line) || /^\\d-(й|nd|rd|th|st)/i.test(line)) continue;
            // Skip single bullet/dot characters
            if (line.length <= 2) continue;

            if (!author) {
                author = line;
                continue;
            }
            // Time pattern: "5 ч." / "3d" / "2w" / "1mo" / "5 hours" / "5 ч. •"
            const cleaned = line.replace(/[•·]/g, '').trim();
            if (/^\\d+\\s*(ч|д|н|м|мин|h|d|w|mo|hr|min|sec|s|year|month|week|day|hour)/i.test(cleaned)) {
                timeStr = cleaned;
                bodyStart = i + 1;
                break;
            }
            if (!subtitle && i < 8) {
                subtitle = line;
            }
        }

        // Body = everything after the time line, minus trailing action buttons
        const bodyLines = [];
        const stopPatterns = /^(Нравится|Like$|Комментировать|Comment$|Поделиться|Share$|Отправить|Send$|Показать перевод|Show translation|\\d+\\s*(реакц|reaction|comment|коммент|like|нравит))/i;
        const skipPatterns = /^(…\\s*развернуть|…\\s*see more|развернуть|see more|Отслеживать|Follow|\\+\\s*Отслеживать|\\+\\s*Follow)$/i;
        for (let i = bodyStart; i < lines.length; i++) {
            if (stopPatterns.test(lines[i])) break;
            if (skipPatterns.test(lines[i])) continue;
            bodyLines.push(lines[i]);
        }
        const body = bodyLines.join('\\n');
        if (body.length < 30) continue;

        // Extract activity URL from links
        let url = '';
        const links = el.querySelectorAll('a[href]');
        for (const a of links) {
            const href = a.getAttribute('href') || '';
            // Prefer feed/activity links
            if (href.includes('/feed/update/') || href.includes('/activity/')) {
                url = href.startsWith('http') ? href : 'https://www.linkedin.com' + href;
                break;
            }
        }
        // Fallback: author profile link
        if (!url) {
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                if (href.includes('/in/') || href.includes('/company/')) {
                    url = href.startsWith('http') ? href : 'https://www.linkedin.com' + href;
                    break;
                }
            }
        }

        posts.push({ author, subtitle, time: timeStr, text: body, url });
    }
    return posts;
}
"""


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

        # --- Anti-ban: per-account cooldown ---
        cooldown_min = int(os.getenv("LINKEDIN_COOLDOWN_MINUTES", "10"))
        cooldown_file = os.path.join(user_data_dir, ".last_scrape")
        if os.path.exists(cooldown_file):
            try:
                last_ts = float(open(cooldown_file).read().strip())
                elapsed = datetime.now(timezone.utc).timestamp() - last_ts
                if elapsed < cooldown_min * 60:
                    remaining = int(cooldown_min * 60 - elapsed)
                    log.warning(
                        "Account %s on cooldown — %ds remaining (min %d min between scrapes)",
                        username, remaining, cooldown_min,
                    )
                    return []
            except (ValueError, OSError):
                pass

        # --- File lock: Chrome allows only one instance per profile dir ---
        # Multiple concurrent scrape requests (e.g. different search terms)
        # would all try to launch Chrome with the same user_data_dir,
        # causing "SingletonLock: File exists" errors.  We serialize access
        # with a file lock; only one browser instance runs at a time.
        lock_path = os.path.join(user_data_dir, ".chrome_lock")
        lock_fd = None
        try:
            lock_fd = open(lock_path, "w")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log.warning("Another Chrome instance is using profile %s — skipping", username)
                lock_fd.close()
                return []

            # Clean up stale SingletonLock from a previous crash
            singleton_lock = os.path.join(user_data_dir, "SingletonLock")
            if os.path.exists(singleton_lock):
                try:
                    os.remove(singleton_lock)
                    log.info("Removed stale SingletonLock")
                except OSError:
                    pass

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

                    # === Human-like: visit feed briefly before searching ===
                    self._browse_feed(page)

                    # === Scrape ===
                    result = self._scrape_posts(page, scraper_input)

                    # Update cooldown timestamp on success
                    try:
                        with open(cooldown_file, "w") as f:
                            f.write(str(datetime.now(timezone.utc).timestamp()))
                    except OSError:
                        pass

                    return result
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
        finally:
            if lock_fd:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()
                except Exception:
                    pass
            if relay:
                relay.stop()

    def _ensure_logged_in(self, context, page, username: str, password: str) -> None:
        """Check for existing session; if not logged in, perform full login."""
        # Fast-path: if LINKEDIN_LI_AT env var is set, inject the cookie
        # directly and skip the full login flow. This is essential for
        # server deployments where Chrome's cookie encryption is platform-
        # specific and profiles can't be copied between macOS and Linux.
        li_at_env = os.getenv("LINKEDIN_LI_AT")
        if li_at_env:
            jar = {c["name"]: c["value"] for c in context.cookies()}
            if "li_at" not in jar:
                log.info("Injecting li_at cookie from LINKEDIN_LI_AT env var")
                context.add_cookies([
                    {
                        "name": "li_at",
                        "value": li_at_env,
                        "domain": ".linkedin.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "None",
                    },
                    {
                        "name": "li_at",
                        "value": li_at_env,
                        "domain": ".www.linkedin.com",
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "None",
                    },
                ])

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

        # --- Handle "Welcome back" page (remembered account) ---
        # LinkedIn may show a page with the user's name and a "click to sign in"
        # button instead of the standard login form. We detect this and click
        # the remembered account, then fill only the password.
        remembered_account = None
        try:
            # The remembered account card is typically a clickable profile row
            # Look for elements that contain the masked email
            profile_cards = page.locator('[data-tracking-control-name="cold_join_sign_in"]')
            if profile_cards.count() == 0:
                # Also try a broad approach: any clickable profile-like element
                profile_cards = page.locator('button:has-text("@"), a:has-text("@")')
            if profile_cards.count() > 0:
                remembered_account = profile_cards.first
                log.info("Detected 'Welcome back' page with remembered account")
        except Exception:
            pass

        if remembered_account:
            # Click the remembered account card to proceed to password entry
            log.info("Clicking remembered account to proceed to password")
            remembered_account.click()
            page.wait_for_timeout(random.randint(2000, 3000))
            _debug_screenshot(page, "02b_after_remembered_click", username)

            # Check if clicking the remembered account logged us in directly
            jar = {c["name"]: c["value"] for c in context.cookies()}
            if "li_at" in jar and "/login" not in page.url:
                log.info("Remembered account click logged us in directly — skipping password")
                return
        else:
            # Standard login form: fill username.
            # LinkedIn's React SPA sometimes renders inputs with
            # display:contents on a parent, which Playwright's is_visible()
            # incorrectly reports as hidden. We use count() + fill(force=True)
            # to bypass this.
            log.info("Entering username")
            username_selectors = [
                "#username",
                'input[name="session_key"]',
                'input[autocomplete="username"]',
                'input[type="email"]',
                'input[type="text"]',
            ]
            username_filled = False
            for sel in username_selectors:
                loc = page.locator(sel).first
                try:
                    if loc.count() > 0:
                        loc.click(force=True, timeout=5000)
                        page.wait_for_timeout(random.randint(200, 500))
                        page.keyboard.type(username, delay=random.randint(30, 80))
                        log.info("Filled username via: %s", sel)
                        username_filled = True
                        break
                except Exception as e:
                    log.debug("Selector %s failed: %s", sel, e)
                    continue
            if not username_filled:
                _debug_screenshot(page, "ERR_no_username_input", username)
                raise RuntimeError("Could not locate username input on LinkedIn login page")

            page.wait_for_timeout(random.randint(300, 700))

        # Fill password
        log.info("Entering password")
        password_selectors = [
            "#password",
            'input[name="session_password"]',
            'input[autocomplete="current-password"]',
            'input[type="password"]',
        ]
        password_filled = False
        for sel in password_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0:
                    loc.click(force=True, timeout=5000)
                    page.wait_for_timeout(random.randint(200, 500))
                    page.keyboard.type(password, delay=random.randint(30, 80))
                    log.info("Filled password via: %s", sel)
                    password_filled = True
                    break
            except Exception as e:
                log.debug("Password selector %s failed: %s", sel, e)
                continue
        if not password_filled:
            _debug_screenshot(page, "ERR_no_password_input", username)
            raise RuntimeError("Could not locate password input on LinkedIn login page")

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

    @staticmethod
    def _browse_feed(page) -> None:
        """Briefly visit the feed to look like a normal user before searching.

        LinkedIn tracks navigation patterns; jumping straight to /search/
        after login is a bot signal.  A real user would glance at their feed
        first.
        """
        log.info("Visiting feed briefly (anti-detection)")
        try:
            page.goto(FEED_URL, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(random.randint(2000, 5000))
            # Scroll down a bit like a human glancing at their feed
            page.evaluate("window.scrollBy(0, 400)")
            page.wait_for_timeout(random.randint(1000, 3000))
        except Exception as e:
            log.debug("Feed browse failed (non-critical): %s", e)

    def _scrape_posts(
        self, page, scraper_input: ScraperInput
    ) -> list[JobPost]:
        """Navigate to content search and extract posts via JS."""
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

        # Wait for content to appear (look for the post header text pattern)
        try:
            page.wait_for_function(
                "() => document.body.innerText.length > 1000",
                timeout=15000,
            )
        except Exception:
            log.warning("Page content didn't load in time")
            _debug_screenshot(page, "11_no_content", "scrape")
            return []

        # Scroll to load more posts (infinite scroll)
        # Slow, human-like scrolling — LinkedIn monitors scroll velocity.
        scroll_count = 4
        for i in range(scroll_count):
            prev_len = page.evaluate("document.body.innerText.length")
            # Scroll in smaller increments instead of jumping to bottom
            page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
            page.wait_for_timeout(random.randint(3000, 6000))
            new_len = page.evaluate("document.body.innerText.length")
            log.info("Scroll %d/%d — text length %d→%d", i + 1, scroll_count, prev_len, new_len)
            if new_len >= prev_len * 3:
                break

        _debug_screenshot(page, "12_after_scroll", "scrape")

        # Extract posts via JavaScript — LinkedIn uses hashed CSS class names
        # that change on each build, so we find the repeating post container
        # structurally: walk down from <main> to find a parent whose children
        # repeat with the same tag+first-class pattern (≥2 siblings).
        raw_posts = page.evaluate(_EXTRACT_POSTS_JS)
        log.info("JS extraction returned %d raw posts", len(raw_posts))

        jobs: list[JobPost] = []
        for raw in raw_posts:
            if len(jobs) >= limit:
                break
            job = self._parse_raw_post(raw)
            if job:
                jobs.append(job)

        return jobs

    def _parse_raw_post(self, raw: dict) -> JobPost | None:
        """Convert a raw JS-extracted post dict into a JobPost."""
        text = raw.get("text", "")
        if len(text) < MIN_POST_LENGTH:
            return None

        author = raw.get("author", "")
        author_subtitle = raw.get("subtitle", "")
        post_url = raw.get("url", "")
        time_text = raw.get("time", "")

        title = self._extract_title(text)
        if not title:
            return None

        company = self._extract_company(text, author, author_subtitle)
        date_posted = _parse_relative_time(time_text) if time_text else None
        location = self._extract_location(text, author_subtitle)
        remote = _is_remote(text)
        job_types = extract_job_type(text)

        return JobPost(
            title=title,
            company_name=company,
            job_url=post_url,
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
            # "HIRING: PHP Developer – $50–120/hour" → "PHP Developer – $50–120/hour"
            # Stop at sentence-ending punctuation, emoji blocks, or "Employment"
            r"\*{0,2}(?:hiring|looking for|we need|now hiring)[:\s*]+(.+?)(?:\*{2}|\n|[.!](?:\s|$)|🏢|📍|💰|Employment|$)",
            r"(?:open position|job opening)[:\s]+(.+?)(?:\n|$)",
            r"(?:role|position)[:\s]+(.+?)(?:\n|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                title = match.group(1).strip()
                # Strip markdown bold markers, hashtags, trailing punctuation
                title = re.sub(r"\*{2}", "", title)
                title = re.sub(r"\s*#\S+", "", title).strip()
                title = title.rstrip("*:").strip()
                if len(title) > 5:
                    return title[:150]

        # Fallback: first non-empty line (stripped of emojis and symbols)
        for line in text.split("\n"):
            line = line.strip()
            if len(line) > 10:
                cleaned = re.sub(r"^[\U0001F300-\U0001FAFF#@*\s]+", "", line).strip()
                if cleaned and len(cleaned) > 5:
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
    def _extract_location(text: str, author_subtitle: str = "") -> Location | None:
        """Extract location and country from post text.

        Uses LinkedIn-specific patterns first (📍, "Remote locations:", etc.),
        then falls back to the Twitter module's country detection which scans
        for flag emojis, country/city names, currency symbols, phone prefixes,
        and TLDs.
        """
        city = None

        # LinkedIn-specific location patterns
        patterns = [
            r"(?:location|based in|located in|office in|remote locations?)[:\s]+(.+?)(?:\n|$|\||🏢|💰|💻)",
            r"\U0001F4CD\s*(.+?)(?:\n|$)",  # 📍 pin emoji
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                city = match.group(1).strip().rstrip(",. ")
                city = re.sub(r"\*+", "", city).strip()
                if city:
                    city = city[:100]
                    break

        # Fallback: use Twitter's location extractor (same regex patterns)
        if not city:
            city = extract_location_from_tweet(text)

        # Country detection — reuse Twitter's multi-signal detector
        # (flag emojis, country names, city names, currency, TLDs, etc.)
        country = extract_country_from_tweet(text, author_subtitle)

        if city or country:
            return Location(city=city, country=country)
        return None


def _is_remote(text: str) -> bool:
    """Check if the post indicates a remote position."""
    text_lower = text.lower()
    keywords = ["remote", "wfh", "work from home", "work-from-home", "fully remote", "100% remote"]
    return any(kw in text_lower for kw in keywords)


def _parse_relative_time(text: str) -> date | None:
    """Parse LinkedIn relative timestamps into a date.

    Handles both English ('3d', '1w', '2h') and Russian ('5 ч.', '3 д.', '1 н.')
    formats that LinkedIn uses depending on locale.
    """
    text = text.strip().lower().rstrip(".")
    # Match patterns like "5 ч", "3d", "1 н", "2w", "1mo"
    match = re.match(r"(\d+)\s*(\S+)", text)
    if not match:
        return None
    value = int(match.group(1))
    unit = match.group(2).rstrip(".")
    now = datetime.now(timezone.utc)
    # Hours: h, hr, hrs, hour, hours, ч, час
    if unit in ("h", "hr", "hrs", "hour", "hours", "ч", "час"):
        return now.date()
    # Days: d, day, days, д, дн, дня, дней, день
    elif unit in ("d", "day", "days", "д", "дн", "дня", "дней", "день"):
        return (now - timedelta(days=value)).date()
    # Weeks: w, wk, week, weeks, н, нед, нед
    elif unit in ("w", "wk", "week", "weeks", "н", "нед"):
        return (now - timedelta(weeks=value)).date()
    # Months: m, mo, month, months, мес
    elif unit in ("m", "mo", "month", "months", "мес"):
        return (now - timedelta(days=value * 30)).date()
    # Minutes: min, мин
    elif unit in ("min", "мин", "минут"):
        return now.date()
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
