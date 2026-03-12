from __future__ import annotations

import random
import re
import shutil
import time
from typing import Optional
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

from jobspy.google.proxy_relay import ProxyRelay

from jobspy.exception import SeekException
from jobspy.seek.constant import headers, SEEK_SITES, SEARCH_API_PATH
from jobspy.seek.util import parse_location, parse_date, parse_salary, map_work_type
from jobspy.model import (
    JobPost,
    JobResponse,
    Country,
    Scraper,
    ScraperInput,
    Site,
    DescriptionFormat,
)
from jobspy.util import (
    create_session,
    create_logger,
    remove_attributes,
    markdown_converter,
    get_enum_from_value,
)

log = create_logger("Seek")

# Map Seek country to proxy geo-targeting suffix
_COUNTRY_PROXY_GEO = {
    "australia": "au",
    "new zealand": "nz",
}


def _add_proxy_geo(proxy_url: str, country_code: str) -> str:
    """Append _country-XX to proxy password for geo-targeting."""
    if not proxy_url or not country_code:
        return proxy_url
    parsed = urlparse(proxy_url)
    if not parsed.password:
        return proxy_url
    if f"_country-{country_code}" in parsed.password:
        return proxy_url
    new_password = f"{parsed.password}_country-{country_code}"
    netloc = f"{parsed.username}:{new_password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
    ],
});
Object.defineProperty(navigator, 'languages', { get: () => ['en-AU', 'en'] });
window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};
"""


class Seek(Scraper):
    delay = 3
    band_delay = 4

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
    ):
        super().__init__(Site.SEEK, proxies=proxies, ca_cert=ca_cert)
        # Session with proxy for API search requests
        self.session = create_session(
            proxies=self.proxies,
            ca_cert=ca_cert,
            is_tls=False,
            has_retry=True,
            delay=5,
            clear_cookies=True,
        )
        self.session.headers.update(headers)
        self.scraper_input = None
        self.base_url = None
        self.site_key = None
        self.locale = None
        self.country = None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input
        self._configure_site(scraper_input.country)

        job_list: list[JobPost] = []
        seen_ids = set()
        page = 1

        while len(job_list) < scraper_input.results_wanted:
            log.info(f"Scraping page {page} from {self.base_url}")

            try:
                jobs_on_page, total_count = self._scrape_page(
                    scraper_input, page
                )
            except SeekException as e:
                log.error(f"Seek API error: {e}")
                break
            except Exception as e:
                log.error(f"Error scraping page {page}: {e}")
                break

            if not jobs_on_page:
                log.info("No more results")
                break

            for job in jobs_on_page:
                if job.id not in seen_ids:
                    seen_ids.add(job.id)
                    job_list.append(job)
                    if len(job_list) >= scraper_input.results_wanted:
                        break

            # Check if there are more pages
            if total_count and len(seen_ids) >= total_count:
                break

            page += 1
            time.sleep(random.uniform(self.delay, self.delay + self.band_delay))

        job_list = job_list[: scraper_input.results_wanted]
        return JobResponse(jobs=job_list)

    def _configure_site(self, country: Country | None) -> None:
        """Set base_url, site_key, locale based on country."""
        country_str = "australia"
        if country:
            country_str = country.value[0].split(",")[0].lower()

        config = SEEK_SITES.get(country_str, SEEK_SITES["australia"])
        self.base_url = config["base_url"]
        self.site_key = config["site_key"]
        self.locale = config["locale"]
        self.country = country or Country.AUSTRALIA

    @staticmethod
    def _launch_browser(p, launch_kwargs: dict):
        """Launch system Chromium or Playwright bundled Chromium.
        Skips real Chrome — its headless mode is detected by Cloudflare."""
        # 1. Try system Chromium (e.g. /usr/bin/chromium in Docker)
        chromium_path = shutil.which("chromium") or shutil.which("chromium-browser")
        if chromium_path:
            try:
                browser = p.chromium.launch(executable_path=chromium_path, **launch_kwargs)
                log.info(f"Using system Chromium: {chromium_path}")
                return browser
            except Exception:
                pass

        # 2. Fall back to Playwright bundled Chromium
        log.info("Using Playwright bundled Chromium")
        return p.chromium.launch(**launch_kwargs)

    def _scrape_page(
        self, scraper_input: ScraperInput, page: int
    ) -> tuple[list[JobPost], int | None]:
        """Scrape a single page of search results. Returns (jobs, total_count)."""
        params = {
            "siteKey": self.site_key,
            "sourcesystem": "houston",
            "page": page,
            "seekSelectAllPages": "true",
            "locale": self.locale,
        }

        if scraper_input.search_term:
            params["keywords"] = scraper_input.search_term

        if scraper_input.location:
            params["where"] = scraper_input.location

        url = f"{self.base_url}{SEARCH_API_PATH}"
        self.session.headers["Referer"] = f"{self.base_url}/"

        response = self.session.get(
            url,
            params=params,
            timeout=scraper_input.request_timeout,
        )

        if response.status_code == 403:
            raise SeekException(
                f"403 Forbidden — blocked by Seek (page {page}). "
                "Try using a proxy or different user agent."
            )

        if response.status_code != 200:
            raise SeekException(
                f"Seek API returned status {response.status_code}"
            )

        try:
            data = response.json()
        except Exception:
            raise SeekException("Failed to parse JSON from Seek API response")

        job_data_list = data.get("data", [])
        total_count = data.get("totalCount")

        # Fetch descriptions via Playwright (one browser for all jobs on this page)
        job_ids = [str(item.get("id", "")) for item in job_data_list if item.get("id")]
        descriptions = self._fetch_descriptions_playwright(job_ids)

        jobs = []
        for item in job_data_list:
            try:
                job = self._process_job(item, descriptions)
                if job:
                    jobs.append(job)
            except Exception as e:
                log.warning(f"Error processing job: {e}")

        log.info(
            f"Page {page}: {len(jobs)} jobs parsed"
            + (f" (total available: {total_count})" if total_count else "")
        )
        return jobs, total_count

    def _get_geo_proxy(self) -> str | None:
        """Get geo-targeted proxy URL for the current country."""
        if not self.proxies:
            return None
        proxy = self.proxies if isinstance(self.proxies, str) else self.proxies[0]
        country_str = "australia"
        if self.country:
            country_str = self.country.value[0].split(",")[0].lower()
        geo_code = _COUNTRY_PROXY_GEO.get(country_str, "au")
        return _add_proxy_geo(proxy, geo_code)

    def _fetch_descriptions_playwright(self, job_ids: list[str]) -> dict[str, str]:
        """Fetch full descriptions for a batch of jobs using Playwright."""
        if not job_ids:
            return {}

        descriptions = {}
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("Playwright not installed — skipping full descriptions")
            return descriptions

        relay = None
        try:
            # Set up proxy relay for geo-targeted residential proxy
            geo_proxy = self._get_geo_proxy()
            proxy_arg = None
            if geo_proxy:
                relay = ProxyRelay(upstream_proxy=geo_proxy)
                relay.start()
                proxy_arg = {"server": f"http://127.0.0.1:{relay.port}"}

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
                        locale="en-AU",
                        timezone_id="Australia/Sydney",
                    )
                    context.add_init_script(_STEALTH_JS)
                    pw_page = context.new_page()

                    for job_id in job_ids:
                        try:
                            desc = self._get_job_description_pw(pw_page, job_id)
                            if desc:
                                descriptions[job_id] = desc
                        except Exception as e:
                            log.debug(f"Playwright: failed to get description for {job_id}: {e}")
                        time.sleep(random.uniform(0.5, 1.5))

                    context.close()
                finally:
                    browser.close()
        except Exception as e:
            log.warning(f"Playwright browser error: {e}")
        finally:
            if relay:
                relay.stop()

        log.info(f"Playwright: fetched {len(descriptions)}/{len(job_ids)} descriptions")
        return descriptions

    def _get_job_description_pw(self, pw_page, job_id: str) -> Optional[str]:
        """Fetch a single job description using an existing Playwright page."""
        url = f"{self.base_url}/job/{job_id}"
        pw_page.goto(url, wait_until="domcontentloaded", timeout=20000)
        pw_page.wait_for_timeout(3000)

        content = pw_page.content()

        # Check for blocks
        if "Just a moment" in content[:1000]:
            log.debug(f"Cloudflare challenge for job {job_id}")
            return None

        soup = BeautifulSoup(content, "html.parser")
        desc_elem = soup.find("span", attrs={"data-automation": "jobAdDetails"})
        if not desc_elem:
            desc_elem = soup.find("div", attrs={"data-automation": "jobAdDetails"})

        if not desc_elem:
            return None

        desc_elem = remove_attributes(desc_elem)
        description = desc_elem.prettify(formatter="html")

        if (
            self.scraper_input
            and self.scraper_input.description_format == DescriptionFormat.MARKDOWN
        ):
            description = markdown_converter(description)

        return description

    def _process_job(self, job_data: dict, descriptions: dict[str, str]) -> Optional[JobPost]:
        """Convert a single Seek API job object to a JobPost."""
        job_id = str(job_data.get("id", ""))
        if not job_id:
            return None

        title = job_data.get("title", "")
        if not title:
            return None

        # Company
        advertiser = job_data.get("advertiser") or {}
        company_name = advertiser.get("description")

        # Location
        locations = job_data.get("locations") or []
        location_label = locations[0].get("label", "") if locations else ""
        location = parse_location(location_label, self.country)

        # Date
        listing_date = job_data.get("listingDate")
        date_posted = parse_date(listing_date)

        # Salary
        salary_label = job_data.get("salaryLabel")
        compensation = parse_salary(salary_label, country=self.country)

        # Job type
        work_types = job_data.get("workTypes") or []
        job_types = []
        for wt in work_types:
            mapped = map_work_type(wt)
            if mapped:
                try:
                    jt = get_enum_from_value(mapped)
                    job_types.append(jt)
                except Exception:
                    pass

        # URL
        job_url = f"{self.base_url}/job/{job_id}"

        # Remote check
        is_remote = self._check_remote(title, location_label, work_types)

        # Build fallback description from API data (teaser + bulletPoints)
        desc_parts = []
        teaser = job_data.get("teaser")
        if teaser:
            desc_parts.append(teaser)
        bullet_points = job_data.get("bulletPoints") or []
        if bullet_points:
            desc_parts.append("\n".join(f"- {bp}" for bp in bullet_points))
        fallback_description = "\n\n".join(desc_parts) if desc_parts else None

        # Use Playwright description or fallback
        full_description = descriptions.get(job_id)

        job_post = JobPost(
            id=job_id,
            title=title,
            company_name=company_name,
            location=location,
            date_posted=date_posted.date() if date_posted else None,
            compensation=compensation,
            job_type=job_types or None,
            job_url=job_url,
            is_remote=is_remote,
            description=full_description or fallback_description,
        )

        return job_post

    @staticmethod
    def _check_remote(
        title: str, location_label: str, work_types: list[str]
    ) -> bool:
        """Check if job is remote based on title, location, and work types."""
        remote_keywords = ["remote", "work from home", "wfh"]
        text = f"{title} {location_label}".lower()
        for wt in work_types:
            text += f" {wt.lower()}"
        return any(kw in text for kw in remote_keywords)
