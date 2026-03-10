from __future__ import annotations

import random
import re
import time
from typing import Optional
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

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
    # Avoid appending twice
    if f"_country-{country_code}" in parsed.password:
        return proxy_url
    new_password = f"{parsed.password}_country-{country_code}"
    netloc = f"{parsed.username}:{new_password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


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

    def _get_detail_session(self):
        """Create a session for detail page fetches with geo-targeted proxy."""
        country_str = "australia"
        if self.country:
            country_str = self.country.value[0].split(",")[0].lower()
        geo_code = _COUNTRY_PROXY_GEO.get(country_str, "au")

        proxy = self.proxies
        if proxy:
            if isinstance(proxy, list):
                proxy = proxy[0]
            proxy = _add_proxy_geo(proxy, geo_code)

        session = create_session(
            proxies=proxy,
            is_tls=False,
            has_retry=True,
            delay=5,
            clear_cookies=True,
        )
        session.headers.update(headers)
        return session

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

        detail_session = self._get_detail_session()

        jobs = []
        for item in job_data_list:
            try:
                job = self._process_job(item, detail_session)
                if job:
                    jobs.append(job)
            except Exception as e:
                log.warning(f"Error processing job: {e}")

        log.info(
            f"Page {page}: {len(jobs)} jobs parsed"
            + (f" (total available: {total_count})" if total_count else "")
        )
        return jobs, total_count

    def _process_job(self, job_data: dict, detail_session) -> Optional[JobPost]:
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

        # Fetch full description from detail page
        full_description = self._get_job_description(job_id, detail_session)

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

    def _get_job_description(self, job_id: str, detail_session) -> Optional[str]:
        """Fetch job detail page and extract description."""
        url = f"{self.base_url}/job/{job_id}"
        try:
            time.sleep(random.uniform(1, 2))
            resp = detail_session.get(url, timeout=30)
            if resp.status_code != 200:
                log.debug(f"Could not fetch description for job {job_id}: HTTP {resp.status_code}")
                return None

            # Check for Cloudflare challenge
            if "Just a moment" in resp.text[:500]:
                log.debug(f"Cloudflare challenge for job {job_id}")
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            desc_elem = soup.find(
                "span", attrs={"data-automation": "jobAdDetails"}
            )
            if not desc_elem:
                desc_elem = soup.find(
                    "div", attrs={"data-automation": "jobAdDetails"}
                )

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

        except Exception as e:
            log.debug(f"Error fetching description for job {job_id}: {e}")
            return None

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
