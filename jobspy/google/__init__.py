from __future__ import annotations

import asyncio
import math
import re
import json
import random
import threading
from typing import Tuple
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Location,
    JobType,
)
from jobspy.util import extract_emails_from_text, extract_job_type, create_session
from jobspy.google.util import log, find_job_info_initial_page, find_job_info
from jobspy.google.constant import headers_jobs, async_param
from jobspy.google.proxy_relay import ProxyRelay


class Google(Scraper):
    def __init__(
        self, proxies: list[str] | str | None = None, ca_cert: str | None = None, user_agent: str | None = None
    ):
        site = Site(Site.GOOGLE)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert)

        self.country = None
        self.scraper_input = None
        self.jobs_per_page = 10
        self.seen_urls = set()
        self.url = "https://www.google.com/search"
        self.jobs_url = "https://www.google.com/async/callback:550"
        self._relay = None
        self._session = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        self.scraper_input.results_wanted = min(900, scraper_input.results_wanted)

        try:
            return self._scrape_with_playwright()
        except Exception as e:
            log.warning(f"Playwright scrape failed ({e}), falling back to HTTP")
            return self._scrape_with_http()
        finally:
            if self._relay:
                self._relay.stop()

    @staticmethod
    def _launch_browser(p, launch_kwargs: dict):
        """Try Chrome, then system Chromium, then Playwright Chromium."""
        import shutil

        # 1. Try real Google Chrome (best for avoiding detection)
        try:
            browser = p.chromium.launch(channel="chrome", **launch_kwargs)
            log.info("Using Google Chrome")
            return browser
        except Exception:
            pass

        # 2. Try system Chromium (e.g. /usr/bin/chromium in Docker)
        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        if chromium_path:
            try:
                browser = p.chromium.launch(executable_path=chromium_path, **launch_kwargs)
                log.info(f"Using system Chromium: {chromium_path}")
                return browser
            except Exception:
                pass

        # 3. Fall back to Playwright bundled Chromium
        log.info("Using Playwright bundled Chromium (may be detected by Google)")
        return p.chromium.launch(**launch_kwargs)

    def _is_captcha_page(self, page) -> bool:
        """Detect CAPTCHA by checking URL and page content."""
        url = page.url
        if "sorry" in url or "/sorry/" in url:
            return True
        # Check page content for CAPTCHA indicators
        try:
            has_captcha = page.locator("#captcha-form, .g-recaptcha, #recaptcha").count() > 0
            if has_captcha:
                return True
        except Exception:
            pass
        return False

    # ── Playwright path (headed browser) ───────────────────────────

    def _scrape_with_playwright(self) -> JobResponse:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("playwright is not installed – run: pip install playwright && playwright install chromium")

        proxy_arg = None
        if self.proxies:
            proxy_url = self.proxies if isinstance(self.proxies, str) else self.proxies[0]
            # Chromium can't do HTTP proxy auth for HTTPS sites,
            # so we spin up a local relay that injects the credentials.
            self._relay = ProxyRelay(upstream_proxy=proxy_url)
            self._relay.start()
            proxy_arg = {"server": f"http://127.0.0.1:{self._relay.port}"}

        query = self._build_query()
        params = f"q={quote_plus(query)}&udm=8&hl=en"
        url = f"{self.url}?{params}"

        with sync_playwright() as p:
            launch_kwargs = {
                "headless": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1920,1080",
                ],
            }
            if proxy_arg:
                launch_kwargs["proxy"] = proxy_arg

            browser = self._launch_browser(p, launch_kwargs)

            try:
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                )
                # Stealth JS removed — overriding navigator.webdriver via JS is
                # itself detectable by Google. --disable-blink-features=AutomationControlled
                # Chrome flag is sufficient.
                page = context.new_page()

                # Navigate to Google homepage with English locale
                page.goto("https://www.google.com/?hl=en", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(random.randint(2000, 4000))

                # Accept cookies consent overlay if shown (various button texts and selectors)
                try:
                    for selector in [
                        "button:has-text('Accept all')",
                        "button:has-text('I agree')",
                        "button:has-text('Aceptar todo')",
                        "button:has-text('Tout accepter')",
                        "#L2AGLb",  # Google consent button ID
                        "button[id='L2AGLb']",
                    ]:
                        btn = page.locator(selector)
                        if btn.count() > 0:
                            btn.first.click()
                            page.wait_for_timeout(1500)
                            break
                except Exception:
                    pass

                # Type search query naturally into the search box
                log.info(f"Searching Google Jobs for: {query}")
                search_box = page.locator("textarea[name='q'], input[name='q']").first
                search_box.click(force=True)  # force to bypass any remaining overlays
                page.wait_for_timeout(random.randint(300, 700))

                # Type with human-like delays
                for char in query:
                    search_box.press(char)
                    page.wait_for_timeout(random.randint(30, 120))
                page.wait_for_timeout(random.randint(500, 1200))

                # Press Enter to search
                search_box.press("Enter")
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(random.randint(2000, 4000))

                if self._is_captcha_page(page):
                    log.warning("CAPTCHA after initial search, aborting")
                    raise RuntimeError("CAPTCHA detected")

                # Now switch to Jobs tab by adding udm=8
                current_url = page.url
                if "udm=8" not in current_url:
                    separator = "&" if "?" in current_url else "?"
                    jobs_url = f"{current_url}{separator}udm=8"
                    page.goto(jobs_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(random.randint(3000, 6000))

                if self._is_captcha_page(page):
                    log.warning("CAPTCHA on Jobs page, aborting")
                    raise RuntimeError("CAPTCHA detected")

                html = page.content()
                log.debug(f"Got page HTML: {len(html)} chars, URL: {page.url}")

                # Parse initial page
                forward_cursor, job_list = self._parse_initial_html(html)
                if forward_cursor is None:
                    if job_list:
                        log.info(f"No pagination cursor, returning {len(job_list)} jobs from first page")
                    else:
                        log.warning("No cursor and no jobs found on initial page")
                    return JobResponse(jobs=job_list)

                log.info(f"Found {len(job_list)} jobs on initial page, cursor present — paginating")

                # Pagination via HTTP (cursor-based requests don't need JS)
                self._session = create_session(
                    proxies=self.proxies, ca_cert=self.ca_cert, is_tls=False, has_retry=True
                )
                scraper_input = self.scraper_input
                page_num = 1
                while (
                    len(self.seen_urls) < scraper_input.results_wanted + scraper_input.offset
                    and forward_cursor
                ):
                    log.info(f"search page: {page_num} / {math.ceil(scraper_input.results_wanted / self.jobs_per_page)}")
                    try:
                        jobs, forward_cursor = self._get_jobs_next_page(forward_cursor)
                    except Exception as e:
                        log.error(f"failed to get jobs on page: {page_num}, {e}")
                        break
                    if not jobs:
                        break
                    job_list += jobs
                    page_num += 1

            finally:
                browser.close()

        return JobResponse(
            jobs=job_list[scraper_input.offset : scraper_input.offset + scraper_input.results_wanted]
        )

    # ── HTTP fallback (original approach) ──────────────────────────

    def _scrape_with_http(self) -> JobResponse:
        self._session = create_session(
            proxies=self.proxies, ca_cert=self.ca_cert, is_tls=False, has_retry=True
        )
        forward_cursor, job_list = self._get_initial_cursor_and_jobs()
        if forward_cursor is None:
            log.warning("initial cursor not found via HTTP fallback")
            return JobResponse(jobs=job_list)

        page = 1
        while (
            len(self.seen_urls) < self.scraper_input.results_wanted + self.scraper_input.offset
            and forward_cursor
        ):
            log.info(f"search page: {page} / {math.ceil(self.scraper_input.results_wanted / self.jobs_per_page)}")
            try:
                jobs, forward_cursor = self._get_jobs_next_page(forward_cursor)
            except Exception as e:
                log.error(f"failed to get jobs on page: {page}, {e}")
                break
            if not jobs:
                break
            job_list += jobs
            page += 1

        return JobResponse(
            jobs=job_list[self.scraper_input.offset : self.scraper_input.offset + self.scraper_input.results_wanted]
        )

    # ── Query building ─────────────────────────────────────────────

    def _build_query(self) -> str:
        query = f"{self.scraper_input.search_term} jobs"

        job_type_mapping = {
            JobType.FULL_TIME: "Full time",
            JobType.PART_TIME: "Part time",
            JobType.INTERNSHIP: "Internship",
            JobType.CONTRACT: "Contract",
        }
        if self.scraper_input.job_type in job_type_mapping:
            query += f" {job_type_mapping[self.scraper_input.job_type]}"
        if self.scraper_input.location:
            query += f" near {self.scraper_input.location}"
        if self.scraper_input.hours_old:
            if self.scraper_input.hours_old <= 24:
                query += " since yesterday"
            elif self.scraper_input.hours_old <= 72:
                query += " in the last 3 days"
            elif self.scraper_input.hours_old <= 168:
                query += " in the last week"
            else:
                query += " in the last month"
        if self.scraper_input.is_remote:
            query += " remote"
        if self.scraper_input.google_search_term:
            query = self.scraper_input.google_search_term

        return query

    # ── Parsing ────────────────────────────────────────────────────

    def _parse_initial_html(self, html: str) -> Tuple[str | None, list[JobPost]]:
        pattern_fc = r'<div jsname="Yust4d"[^>]+data-async-fc="([^"]+)"'
        match_fc = re.search(pattern_fc, html)
        data_async_fc = match_fc.group(1) if match_fc else None

        jobs_raw = find_job_info_initial_page(html)
        jobs = []
        for job_raw in jobs_raw:
            job_post = self._parse_job(job_raw)
            if job_post:
                jobs.append(job_post)
        return data_async_fc, jobs

    def _get_initial_cursor_and_jobs(self) -> Tuple[str | None, list[JobPost]]:
        """HTTP-based initial page fetch (fallback)."""
        from jobspy.google.constant import headers_initial
        query = self._build_query()
        params = {"q": query, "udm": "8", "hl": "en"}
        response = self._session.get(self.url, headers=headers_initial, params=params)
        return self._parse_initial_html(response.text)

    def _get_jobs_next_page(self, forward_cursor: str) -> Tuple[list[JobPost], str]:
        params = {"fc": [forward_cursor], "fcv": ["3"], "async": [async_param]}
        response = self._session.get(self.jobs_url, headers=headers_jobs, params=params)
        return self._parse_jobs(response.text)

    def _parse_jobs(self, job_data: str) -> Tuple[list[JobPost], str]:
        start_idx = job_data.find("[[[")
        end_idx = job_data.rindex("]]]") + 3
        s = job_data[start_idx:end_idx]
        parsed = json.loads(s)[0]

        pattern_fc = r'data-async-fc="([^"]+)"'
        match_fc = re.search(pattern_fc, job_data)
        data_async_fc = match_fc.group(1) if match_fc else None
        jobs_on_page = []
        for array in parsed:
            _, job_data = array
            if not job_data.startswith("[[["):
                continue
            job_d = json.loads(job_data)
            job_info = find_job_info(job_d)
            job_post = self._parse_job(job_info)
            if job_post:
                jobs_on_page.append(job_post)
        return jobs_on_page, data_async_fc

    def _parse_job(self, job_info: list):
        try:
            job_url = job_info[3][0][0] if job_info[3] and job_info[3][0] else None
        except (IndexError, TypeError):
            return None
        if job_url in self.seen_urls:
            return
        self.seen_urls.add(job_url)

        title = job_info[0]
        company_name = job_info[1]
        location = city = job_info[2]
        state = country = date_posted = None
        if location and "," in location:
            city, state, *country = [*map(lambda x: x.strip(), location.split(","))]

        try:
            days_ago_str = job_info[12]
            if isinstance(days_ago_str, str):
                match = re.search(r"\d+", days_ago_str)
                days_ago = int(match.group()) if match else None
                if days_ago is not None:
                    date_posted = (datetime.now() - timedelta(days=days_ago)).date()
        except (IndexError, TypeError):
            pass

        try:
            description = job_info[19]
        except IndexError:
            description = ""

        try:
            job_id = f"go-{job_info[28]}"
        except IndexError:
            job_id = f"go-{hash(job_url)}"

        job_post = JobPost(
            id=job_id,
            title=title,
            company_name=company_name,
            location=Location(
                city=city, state=state, country=country[0] if country else None
            ),
            job_url=job_url,
            date_posted=date_posted,
            is_remote="remote" in (description or "").lower() or "wfh" in (description or "").lower(),
            description=description,
            emails=extract_emails_from_text(description) if description else None,
            job_type=extract_job_type(description) if description else None,
        )
        return job_post
