from __future__ import annotations

import base64
import random
import time
from typing import Optional

from jobspy.exception import ReedException
from jobspy.reed.constant import SEARCH_URL, DETAILS_URL, RESULTS_PER_PAGE
from jobspy.reed.util import (
    parse_location,
    parse_date,
    parse_salary_from_details,
    parse_salary_from_search,
    map_job_type_from_details,
    map_job_type_from_search,
)
from jobspy.model import (
    JobPost,
    JobResponse,
    Scraper,
    ScraperInput,
    Site,
    DescriptionFormat,
)
from jobspy.util import create_session, create_logger, markdown_converter

log = create_logger("Reed")


class Reed(Scraper):
    base_url = "https://www.reed.co.uk"
    delay = 1
    band_delay = 2

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        reed_api_key: str | None = None,
    ):
        super().__init__(Site.REED, proxies=proxies, ca_cert=ca_cert)
        # Official API — no proxy needed
        self.session = create_session(
            is_tls=False,
            has_retry=True,
            delay=3,
            clear_cookies=True,
        )
        self.api_key = reed_api_key
        self.scraper_input = None

    def _get_auth_header(self) -> dict:
        """Build Basic Auth header. API key as username, empty password."""
        if not self.api_key:
            raise ReedException("Reed API key is required. Pass reed_api_key parameter.")
        encoded = base64.b64encode(f"{self.api_key}:".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        self.scraper_input = scraper_input

        job_list: list[JobPost] = []
        seen_ids = set()
        offset = 0

        while len(job_list) < scraper_input.results_wanted:
            log.info(f"Scraping Reed offset={offset}")

            try:
                jobs_on_page, total_count = self._scrape_page(scraper_input, offset)
            except ReedException as e:
                log.error(f"Reed API error: {e}")
                break
            except Exception as e:
                log.error(f"Error scraping Reed offset={offset}: {e}")
                break

            if not jobs_on_page:
                log.info("No more results from Reed")
                break

            for job_data in jobs_on_page:
                job_id = str(job_data.get("jobId", ""))
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                try:
                    details = self._get_job_details(job_id)
                    job = self._process_job(job_data, details)
                    if job:
                        job_list.append(job)
                        if len(job_list) >= scraper_input.results_wanted:
                            break
                except Exception as e:
                    log.warning(f"Error processing Reed job {job_id}: {e}")

                # Small delay between detail requests
                time.sleep(random.uniform(0.3, 0.8))

            # Check if we've exhausted results
            if total_count and offset + RESULTS_PER_PAGE >= total_count:
                break

            offset += RESULTS_PER_PAGE
            time.sleep(random.uniform(self.delay, self.delay + self.band_delay))

        job_list = job_list[: scraper_input.results_wanted]
        return JobResponse(jobs=job_list)

    def _scrape_page(
        self, scraper_input: ScraperInput, offset: int
    ) -> tuple[list[dict], int | None]:
        """Fetch a page of search results from Reed API."""
        params = {
            "resultsToTake": RESULTS_PER_PAGE,
            "resultsToSkip": offset,
        }

        if scraper_input.search_term:
            params["keywords"] = scraper_input.search_term

        if scraper_input.location:
            params["locationName"] = scraper_input.location

        if scraper_input.distance:
            params["distanceFromLocation"] = scraper_input.distance

        # Job type filters
        if scraper_input.job_type:
            from jobspy.model import JobType
            type_map = {
                JobType.FULL_TIME: "fullTime",
                JobType.PART_TIME: "partTime",
                JobType.CONTRACT: "contract",
                JobType.TEMPORARY: "temp",
            }
            param_name = type_map.get(scraper_input.job_type)
            if param_name:
                params[param_name] = "true"

        headers = self._get_auth_header()

        response = self.session.get(
            SEARCH_URL,
            params=params,
            headers=headers,
            timeout=scraper_input.request_timeout,
        )

        if response.status_code == 401:
            raise ReedException("Reed API authentication failed. Check your API key.")

        if response.status_code != 200:
            raise ReedException(
                f"Reed API returned status {response.status_code}"
            )

        try:
            data = response.json()
        except Exception:
            raise ReedException("Failed to parse JSON from Reed API response")

        results = data.get("results", [])
        total_count = data.get("totalResults")

        log.info(
            f"Reed offset={offset}: {len(results)} jobs"
            + (f" (total: {total_count})" if total_count else "")
        )
        return results, total_count

    def _get_job_details(self, job_id: str) -> dict:
        """Fetch job details from Reed API."""
        url = f"{DETAILS_URL}/{job_id}"
        headers = self._get_auth_header()

        try:
            response = self.session.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                return response.json()
            else:
                log.debug(f"Could not fetch details for Reed job {job_id}: HTTP {response.status_code}")
                return {}
        except Exception as e:
            log.debug(f"Error fetching Reed job details {job_id}: {e}")
            return {}

    def _process_job(self, job_data: dict, details: dict) -> Optional[JobPost]:
        """Convert Reed API data to JobPost."""
        job_id = str(job_data.get("jobId", ""))
        title = job_data.get("jobTitle", "")
        if not job_id or not title:
            return None

        company_name = job_data.get("employerName")

        # Location — details may have more precise data
        location_name = details.get("locationName") or job_data.get("locationName", "")
        location = parse_location(location_name)

        # Date
        date_str = job_data.get("date")
        date_posted = parse_date(date_str)

        # Salary — prefer details (has salaryType, currency, yearly fields)
        if details:
            compensation = parse_salary_from_details(details)
        else:
            compensation = parse_salary_from_search(
                job_data.get("minimumSalary"),
                job_data.get("maximumSalary"),
                job_data.get("currency"),
            )

        # Job type — prefer details (has jobType + contractType fields)
        if details:
            job_types = map_job_type_from_details(details)
        else:
            job_types = map_job_type_from_search(job_data)

        # URL — detail endpoint has the canonical URL
        job_url = details.get("jobUrl") or f"{self.base_url}/jobs/{job_id}"

        # External URL for jobs hosted on external sites
        job_url_direct = details.get("externalUrl") or None

        # Description from details endpoint (HTML)
        description = details.get("jobDescription", "")
        if description:
            if (
                self.scraper_input
                and self.scraper_input.description_format == DescriptionFormat.MARKDOWN
            ):
                description = markdown_converter(description)

        # Remote check
        is_remote = self._check_remote(title, location_name, description)

        return JobPost(
            id=job_id,
            title=title,
            company_name=company_name,
            location=location,
            date_posted=date_posted.date() if date_posted else None,
            compensation=compensation,
            job_type=job_types or None,
            job_url=job_url,
            job_url_direct=job_url_direct,
            description=description or None,
            is_remote=is_remote,
        )

    @staticmethod
    def _check_remote(title: str, location: str, description: str) -> bool:
        """Check if job is remote based on title, location, and description."""
        remote_keywords = ["remote", "work from home", "wfh"]
        text = f"{title} {location}".lower()
        if description:
            text += f" {description[:500].lower()}"
        return any(kw in text for kw in remote_keywords)
