from __future__ import annotations

import json
import random
import time
import re
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Location,
    Country,
)
from jobspy.util import (
    create_session,
    extract_emails_from_text,
    markdown_converter,
)
from jobspy.irishjobs.constant import BASE_URL, SEARCH_URL, SELECTORS
from jobspy.irishjobs.util import (
    log,
    parse_salary,
    parse_date,
    parse_location,
    map_job_type,
    slugify,
)

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ],
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
"""


class IrishJobs(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        site = Site(Site.IRISH_JOBS)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert)
        self.scraper_input = None
        self.seen_urls: set[str] = set()

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        try:
            return self._scrape_with_playwright()
        except Exception as e:
            log.warning(f"Playwright scrape failed ({e}), falling back to HTTP")
            return self._scrape_with_http()

    # ── Playwright path ───────────────────────────────────────────

    @staticmethod
    def _launch_browser(p, launch_kwargs: dict):
        """Try Chrome, then system Chromium, then Playwright Chromium."""
        import shutil

        try:
            browser = p.chromium.launch(channel="chrome", **launch_kwargs)
            log.info("Using Google Chrome")
            return browser
        except Exception:
            pass

        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        if chromium_path:
            try:
                browser = p.chromium.launch(executable_path=chromium_path, **launch_kwargs)
                log.info(f"Using system Chromium: {chromium_path}")
                return browser
            except Exception:
                pass

        log.info("Using Playwright bundled Chromium")
        return p.chromium.launch(**launch_kwargs)

    @staticmethod
    def _accept_cookies(page) -> None:
        """Dismiss GDPR cookie consent popup if present."""
        try:
            consent = page.query_selector(
                "#ccmgt_explicit_accept, "
                "button:has-text('Accept All'), "
                "button:has-text('Accept all'), "
                "button:has-text('I agree')"
            )
            if consent:
                consent.click()
                page.wait_for_timeout(1000)
                log.info("Accepted cookie consent")
        except Exception:
            pass

    def _scrape_with_playwright(self) -> JobResponse:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "playwright is not installed – run: pip install playwright && playwright install chromium"
            )

        job_list: list[JobPost] = []

        with sync_playwright() as p:
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=1920,1080",
                ],
            }

            browser = self._launch_browser(p, launch_kwargs)

            try:
                context = browser.new_context(
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="Europe/Dublin",
                )
                context.add_init_script(_STEALTH_JS)
                page = context.new_page()

                current_page_num = 1
                while len(job_list) < self.scraper_input.results_wanted:
                    url = self._build_search_url(page=current_page_num)
                    log.info(f"Fetching search page {current_page_num}: {url}")

                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(random.randint(3000, 6000))

                    # Dismiss cookie consent on first page
                    if current_page_num == 1:
                        self._accept_cookies(page)

                    # Wait for job cards to render
                    try:
                        page.wait_for_selector(
                            SELECTORS["job_card"],
                            timeout=15000,
                        )
                    except Exception:
                        log.warning(f"No job cards found on page {current_page_num}")
                        break

                    html = page.content()
                    jobs_on_page = self._parse_search_results(html)

                    if not jobs_on_page:
                        log.info(f"No jobs parsed from page {current_page_num}, stopping")
                        break

                    for job_data in jobs_on_page:
                        if len(job_list) >= self.scraper_input.results_wanted:
                            break
                        try:
                            job = self._process_job_playwright(job_data, page)
                            if job:
                                job_list.append(job)
                        except Exception as e:
                            log.warning(f"Error processing job: {e}")

                    # Check if there's a next page
                    has_next = self._has_next_page(html)
                    if not has_next:
                        log.info("No next page found, stopping pagination")
                        break

                    current_page_num += 1
                    page.wait_for_timeout(random.randint(2000, 4000))

            finally:
                browser.close()

        return JobResponse(jobs=job_list[: self.scraper_input.results_wanted])

    # ── HTTP fallback ─────────────────────────────────────────────

    def _scrape_with_http(self) -> JobResponse:
        """Fallback: try HTTP request and parse whatever SSR content is available."""
        session = create_session(
            proxies=self.proxies, ca_cert=self.ca_cert, is_tls=False, has_retry=True, delay=3
        )

        job_list: list[JobPost] = []
        current_page = 1

        while len(job_list) < self.scraper_input.results_wanted:
            url = self._build_search_url(page=current_page)
            log.info(f"HTTP fallback - fetching page {current_page}: {url}")

            try:
                response = session.get(url, timeout=30)
                if response.status_code != 200:
                    log.warning(f"HTTP {response.status_code} for {url}")
                    break
            except Exception as e:
                log.error(f"HTTP request failed: {e}")
                break

            jobs_on_page = self._parse_search_results(response.text)
            if not jobs_on_page:
                log.info("No jobs found via HTTP fallback")
                break

            for job_data in jobs_on_page:
                if len(job_list) >= self.scraper_input.results_wanted:
                    break
                try:
                    job = self._build_job_post(job_data, None)
                    if job:
                        job_list.append(job)
                except Exception as e:
                    log.warning(f"Error processing job (HTTP): {e}")

            if not self._has_next_page(response.text):
                break

            current_page += 1
            time.sleep(random.uniform(2, 5))

        return JobResponse(jobs=job_list[: self.scraper_input.results_wanted])

    # ── URL building ──────────────────────────────────────────────

    def _build_search_url(self, page: int = 1) -> str:
        """Build IrishJobs search URL.

        Pattern: https://www.irishjobs.ie/jobs/{search-term}/in-{location}?page=N
        """
        search_term = self.scraper_input.search_term or "software engineer"
        slug = slugify(search_term)
        url = f"{SEARCH_URL}/{slug}"

        if self.scraper_input.location:
            location_slug = slugify(self.scraper_input.location)
            url += f"/in-{location_slug}"

        if page > 1:
            url += f"?page={page}"

        return url

    # ── Parsing ───────────────────────────────────────────────────

    def _parse_search_results(self, html: str) -> list[dict]:
        """Parse job cards from rendered HTML."""
        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        job_cards = soup.select(SELECTORS["job_card"])
        if job_cards:
            log.info(f"Found {len(job_cards)} job cards")
        else:
            # Fallback: look for JSON-LD structured data
            jobs = self._extract_jobs_from_jsonld(soup)
            if jobs:
                return jobs

            log.warning("No job cards found in HTML")
            return []

        for card in job_cards:
            job_data = self._extract_from_card(card)
            if job_data and job_data.get("title") and job_data.get("url"):
                jobs.append(job_data)

        return jobs

    def _extract_from_card(self, card) -> dict | None:
        """Extract job data from a BeautifulSoup job card element."""

        def _find_text(selectors_key: str) -> str | None:
            for sel in SELECTORS[selectors_key].split(", "):
                el = card.select_one(sel)
                if el:
                    return el.get_text(strip=True)
            return None

        def _find_link(selectors_key: str) -> tuple[str | None, str | None]:
            for sel in SELECTORS[selectors_key].split(", "):
                el = card.select_one(sel)
                if el:
                    text = el.get_text(strip=True)
                    href = el.get("href", "")
                    if href and not href.startswith("http"):
                        href = urljoin(BASE_URL, href)
                    return text, href
            return None, None

        title, url = _find_link("job_title")

        if not title or not url:
            return None

        if url in self.seen_urls:
            return None
        self.seen_urls.add(url)

        company = _find_text("company")
        location_text = _find_text("location")
        salary_text = _find_text("salary")
        date_text = _find_text("date")
        job_type_text = _find_text("job_type")

        return {
            "title": title,
            "url": url,
            "company": company,
            "location": location_text,
            "salary": salary_text,
            "date": date_text,
            "job_type": job_type_text,
        }

    def _extract_jobs_from_jsonld(self, soup) -> list[dict]:
        """Extract jobs from JSON-LD structured data in the page."""
        jobs = []
        ld_json = soup.find_all("script", type="application/ld+json")
        for script in ld_json:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and data.get("@type") == "ItemList":
                    for item in data.get("itemListElement", []):
                        if isinstance(item, dict):
                            jobs.append(self._extract_from_jsonld(item))
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("@type") == "JobPosting":
                            jobs.append(self._extract_from_jsonld(item))
            except Exception:
                continue
        return jobs

    def _extract_from_jsonld(self, item: dict) -> dict:
        """Extract job data from JSON-LD structured data."""
        job_posting = item.get("item", item)

        title = job_posting.get("title", "")
        url = job_posting.get("url", "")
        if url and not url.startswith("http"):
            url = urljoin(BASE_URL, url)

        company = ""
        hiring_org = job_posting.get("hiringOrganization", {})
        if isinstance(hiring_org, dict):
            company = hiring_org.get("name", "")

        location_text = ""
        job_location = job_posting.get("jobLocation", {})
        if isinstance(job_location, dict):
            address = job_location.get("address", {})
            if isinstance(address, dict):
                location_text = address.get("addressLocality", "")

        salary_text = None
        base_salary = job_posting.get("baseSalary", {})
        if isinstance(base_salary, dict):
            value = base_salary.get("value", {})
            if isinstance(value, dict):
                min_val = value.get("minValue", "")
                max_val = value.get("maxValue", "")
                currency = base_salary.get("currency", "EUR")
                if min_val or max_val:
                    salary_text = f"{currency} {min_val} - {max_val}"

        date_text = job_posting.get("datePosted", "")

        return {
            "title": title,
            "url": url,
            "company": company,
            "location": location_text,
            "salary": salary_text,
            "date": date_text,
            "job_type": job_posting.get("employmentType", ""),
            "description": job_posting.get("description", ""),
        }

    def _has_next_page(self, html: str) -> bool:
        """Check if there's a next page of results."""
        soup = BeautifulSoup(html, "html.parser")
        for selector in SELECTORS["next_page"].split(", "):
            el = soup.select_one(selector)
            if el:
                return True
        return False

    # ── Job processing ────────────────────────────────────────────

    def _process_job_playwright(self, job_data: dict, page) -> Optional[JobPost]:
        """Convert raw job data to JobPost, fetching description via in-page navigation."""
        description = job_data.get("description")

        if not description and job_data.get("url"):
            description = self._get_description_playwright(job_data["url"], page)

        return self._build_job_post(job_data, description)

    def _build_job_post(self, job_data: dict, description: str | None) -> JobPost:
        """Build a JobPost from parsed job data."""
        city, state = parse_location(job_data.get("location"))
        compensation = parse_salary(job_data.get("salary"))
        date_posted = parse_date(job_data.get("date"))
        job_types = map_job_type(job_data.get("job_type"))

        # Detect remote from description or title
        is_remote = False
        for text in [description, job_data.get("title", ""), job_data.get("location", "")]:
            if text and re.search(r"\b(remote|wfh|work from home)\b", text, re.IGNORECASE):
                is_remote = True
                break

        # If no job types from card, try extracting from description
        if not job_types and description:
            from jobspy.util import extract_job_type
            job_types = extract_job_type(description)

        job_url = job_data.get("url", "")

        # Extract job ID from URL: /...-job106721422 -> ij-106721422
        id_match = re.search(r"job(\d+)", job_url)
        if id_match:
            job_id = f"ij-{id_match.group(1)}"
        else:
            job_id = f"ij-{abs(hash(job_url))}"

        # Convert HTML description to markdown if needed
        if description and self.scraper_input and self.scraper_input.description_format:
            from jobspy.model import DescriptionFormat
            if self.scraper_input.description_format == DescriptionFormat.MARKDOWN and "<" in description:
                description = markdown_converter(description)

        return JobPost(
            id=job_id,
            title=job_data.get("title", ""),
            company_name=job_data.get("company"),
            job_url=job_url,
            location=Location(
                city=city,
                state=state,
                country=Country.IRELAND,
            ),
            description=description,
            compensation=compensation,
            date_posted=date_posted,
            job_type=job_types,
            is_remote=is_remote,
            emails=extract_emails_from_text(description) if description else None,
        )

    def _get_description_playwright(self, url: str, page) -> str | None:
        """Navigate to job detail page using in-page JS navigation, extract description from JSON-LD."""
        try:
            # Use JS navigation (avoids ERR_HTTP2_PROTOCOL_ERROR from page.goto)
            href = url.replace(BASE_URL, "") if url.startswith(BASE_URL) else url
            page.evaluate(f'window.location.href = "{href}"')
            page.wait_for_timeout(random.randint(2500, 4500))

            # Wait for page to load
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass

            html = page.content()
            soup = BeautifulSoup(html, "html.parser")

            # Primary: extract description from JSON-LD (most reliable)
            ld_scripts = soup.find_all("script", type="application/ld+json")
            for script in ld_scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict) and data.get("description"):
                        return data["description"]
                except Exception:
                    continue

            # Fallback: CSS selectors
            for selector in SELECTORS["description"].split(", "):
                el = soup.select_one(selector)
                if el and len(el.get_text(strip=True)) > 50:
                    return str(el)

            return None

        except Exception as e:
            log.warning(f"Failed to get description from {url}: {e}")
            return None
        finally:
            # Navigate back to search results
            try:
                page.go_back(wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(random.randint(1500, 3000))
            except Exception:
                pass
