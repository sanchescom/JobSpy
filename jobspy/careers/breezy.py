from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location
from jobspy.util import extract_emails_from_text, extract_job_type, markdown_converter

logger = logging.getLogger("JobSpy:careers:breezy")

# Breezy HR boards (<company>.breezy.hr) publish every open position as JSON at
# /json: title, place, type, date — no description. Each posting's page
# (/p/<friendly_id>, ~23 KB) carries a JSON-LD JobPosting with the full text.
JOBS_LIST_URL = "https://{host}/json"


class BreezyParser(BaseATSParser):
    platform = "breezy"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        host = urlparse(career_url).hostname or ""
        resp = self.session.get(JOBS_LIST_URL.format(host=host), timeout=30)
        if not resp.ok:
            logger.warning("Breezy %d for %s", resp.status_code, host)
            return []
        results = []
        for job in resp.json():
            try:
                post = self._parse_list_job(job, company_name)
                self._enrich_description(post)
                results.append(post)
            except Exception as e:
                logger.debug("Failed to parse Breezy job: %s", e)
        logger.info("Breezy: %d jobs from %s", len(results), host)
        return results

    def _enrich_description(self, post: JobPost) -> None:
        try:
            resp = self.session.get(post.job_url, timeout=15)
            if resp.ok:
                post.description = parse_jsonld_description(resp.text)
        except Exception as e:
            logger.debug("Breezy posting page failed for %s: %s", post.job_url, e)
        if post.description:
            post.emails = extract_emails_from_text(post.description)
            post.job_type = post.job_type or extract_job_type(post.description)

    @staticmethod
    def _parse_list_job(job: dict, company_name: str) -> JobPost:
        loc = job.get("location") or {}
        date_posted = None
        if job.get("published_date"):
            try:
                date_posted = datetime.fromisoformat(job["published_date"].replace("Z", "+00:00")).date()
            except ValueError:
                pass
        return JobPost(
            id=f"breezy:{job['id']}",
            title=job.get("name", ""),
            company_name=company_name,
            job_url=job["url"],
            location=Location(
                city=loc.get("city"),
                state=(loc.get("state") or {}).get("name"),
                country=(loc.get("country") or {}).get("id"),
            ),
            description=None,  # enriched from the posting page
            is_remote=bool(loc.get("is_remote")),
            date_posted=date_posted,
        )


def parse_jsonld_description(page: str) -> str | None:
    """Markdown description from the page's JSON-LD JobPosting, or None."""
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, flags=re.S):
        try:
            data = json.loads(block, strict=False)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting" and item.get("description"):
                return markdown_converter(item["description"])
    return None
