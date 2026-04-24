from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:bamboohr")

# BambooHR APIs:
#   List:   GET https://{subdomain}.bamboohr.com/careers/list  (Accept: application/json)
#   Detail: GET https://{subdomain}.bamboohr.com/careers/{id}/detail  (Accept: application/json)
LIST_API_URL = "https://{subdomain}.bamboohr.com/careers/list"
DETAIL_API_URL = "https://{subdomain}.bamboohr.com/careers/{job_id}/detail"
ENRICHMENT_WORKERS = 5
ENRICHMENT_DELAY = 0.15


class BambooHRParser(BaseATSParser):
    platform = "bamboohr"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        subdomain = self._extract_subdomain(career_url)

        # Fetch job list via JSON API
        results = self._fetch_list(subdomain, company_name)
        if results is None:
            return []

        # Enrich with descriptions via detail endpoint
        if results:
            self._enrich_descriptions(results, subdomain)

        logger.info("BambooHR: %d jobs from %s", len(results), subdomain)
        return results

    @staticmethod
    def _extract_subdomain(career_url: str) -> str:
        """Extract subdomain from https://{company}.bamboohr.com/careers URLs."""
        parsed = urlparse(career_url)
        hostname = parsed.hostname or ""
        return hostname.replace(".bamboohr.com", "")

    def _fetch_list(self, subdomain: str, company_name: str) -> list[JobPost] | None:
        """Fetch job listing via BambooHR JSON API."""
        url = LIST_API_URL.format(subdomain=subdomain)
        try:
            resp = self.session.get(url, headers={"Accept": "application/json"}, timeout=30)
            if not resp.ok:
                logger.warning("BambooHR list API %d for %s", resp.status_code, subdomain)
                return None

            data = resp.json()
            if not isinstance(data, dict):
                return None

            results = []
            for job in data.get("result", []):
                try:
                    results.append(self._parse_list_job(job, subdomain, company_name))
                except Exception as e:
                    logger.debug("Failed to parse BambooHR job: %s", e)

            return results
        except Exception:
            return None

    def _enrich_descriptions(self, results: list[JobPost], subdomain: str) -> None:
        """Fetch descriptions concurrently via /careers/{id}/detail endpoint."""
        enriched = 0

        def _fetch_detail(job_post: JobPost) -> tuple[JobPost, dict | None]:
            time.sleep(ENRICHMENT_DELAY)
            job_id = job_post.id.split(":")[-1]
            url = DETAIL_API_URL.format(subdomain=subdomain, job_id=job_id)
            try:
                resp = self.session.get(
                    url,
                    headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                    timeout=15,
                )
                if resp.ok:
                    return job_post, resp.json()
            except Exception:
                pass
            return job_post, None

        with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as pool:
            futures = [pool.submit(_fetch_detail, post) for post in results]
            for future in as_completed(futures):
                post, data = future.result()
                if not data:
                    continue

                job = data.get("result", {}).get("jobOpening", {})
                if not job:
                    continue

                # Description (HTML)
                desc_html = job.get("description", "")
                if desc_html:
                    post.description = markdown_converter(desc_html)
                    post.emails = extract_emails_from_text(post.description)
                    post.job_type = extract_job_type(post.description)
                    enriched += 1

                # Date posted (more reliable from detail)
                date_str = job.get("datePosted")
                if date_str and not post.date_posted:
                    try:
                        post.date_posted = datetime.fromisoformat(
                            str(date_str).replace("Z", "+00:00")
                        ).date()
                    except (ValueError, AttributeError):
                        try:
                            post.date_posted = datetime.strptime(str(date_str), "%Y-%m-%d").date()
                        except (ValueError, AttributeError):
                            pass

                # Location enrichment (detail has addressCountry)
                loc = job.get("location", {})
                if isinstance(loc, dict):
                    country = loc.get("addressCountry") or loc.get("country")
                    if country and post.location and not post.location.country:
                        post.location = Location(
                            city=post.location.city,
                            state=post.location.state,
                            country=country,
                        )

        logger.info("BambooHR: enriched %d/%d jobs with descriptions", enriched, len(results))

    @staticmethod
    def _parse_list_job(job: dict, subdomain: str, company_name: str) -> JobPost:
        """Parse a job from the list API response."""
        job_id = str(job.get("id", ""))
        title = job.get("jobOpeningName", "") or job.get("title", "")

        job_url = f"https://{subdomain}.bamboohr.com/careers/{job_id}"

        loc = job.get("location", {})
        if isinstance(loc, dict):
            location = Location(
                city=loc.get("city") or None,
                state=loc.get("state") or None,
                country=loc.get("country") or None,
            )
        else:
            location = _parse_location_str(str(loc))

        is_remote = bool(job.get("isRemote", False))

        return JobPost(
            id=f"bamboohr:{subdomain}:{job_id}",
            title=title,
            company_name=company_name,
            job_url=job_url,
            location=location,
            is_remote=is_remote,
        )


def _parse_location_str(loc_str: str) -> Location:
    if not loc_str:
        return Location()
    parts = [p.strip() for p in loc_str.split(",")]
    if len(parts) >= 2:
        return Location(city=parts[0], country=parts[-1])
    return Location(city=parts[0])
