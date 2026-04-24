from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:lever")

# Lever postings API with pagination via skip+limit
POSTINGS_URL = "https://api.lever.co/v0/postings/{company}?mode=json"
PAGE_SIZE = 100


class LeverParser(BaseATSParser):
    platform = "lever"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        slug = self._extract_slug(career_url)
        results = []
        skip = 0

        while True:
            url = f"{POSTINGS_URL.format(company=slug)}&skip={skip}&limit={PAGE_SIZE}"
            resp = self.session.get(url, timeout=30)
            if not resp.ok:
                logger.warning("Lever API %d for %s", resp.status_code, slug)
                break

            jobs_data = resp.json()
            if not jobs_data:
                break

            for job in jobs_data:
                try:
                    results.append(self._parse_job(job, slug, company_name))
                except Exception as e:
                    logger.debug("Failed to parse Lever job: %s", e)

            if len(jobs_data) < PAGE_SIZE:
                break
            skip += PAGE_SIZE

        logger.info("Lever: %d jobs from %s", len(results), slug)
        return results

    @staticmethod
    def _extract_slug(career_url: str) -> str:
        """Extract company slug from https://jobs.lever.co/{company}/... URLs."""
        parsed = urlparse(career_url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        return parts[0] if parts else ""

    @staticmethod
    def _parse_job(job: dict, slug: str, company_name: str) -> JobPost:
        job_id = job.get("id", "")
        title = job.get("text", "")
        job_url = job.get("hostedUrl") or job.get("applyUrl") or ""

        # Location
        loc_str = job.get("categories", {}).get("location", "")
        location = _parse_lever_location(loc_str)

        # Description: Lever returns lists of content blocks
        description_parts = []
        for section in job.get("lists", []):
            heading = section.get("text", "")
            if heading:
                description_parts.append(f"## {heading}")
            content = section.get("content", "")
            if content:
                description_parts.append(markdown_converter(content) or "")

        additional = job.get("additional", "")
        if additional:
            description_parts.append(markdown_converter(additional) or "")

        opening = job.get("descriptionPlain") or job.get("description", "")
        if opening:
            description_parts.insert(0, opening)

        description = "\n\n".join(filter(None, description_parts)) or None

        # Date
        date_posted = None
        created_at = job.get("createdAt")
        if created_at:
            try:
                date_posted = datetime.fromtimestamp(created_at / 1000).date()
            except (ValueError, TypeError, OSError):
                pass

        # Categories
        categories = job.get("categories", {})
        commitment = categories.get("commitment", "")
        is_remote = "remote" in (loc_str + " " + commitment).lower()

        job_types = extract_job_type(commitment or (description or "")) or None
        emails = extract_emails_from_text(description) if description else None

        return JobPost(
            id=f"lever:{slug}:{job_id}",
            title=title,
            company_name=company_name,
            job_url=job_url,
            location=location,
            description=description,
            is_remote=is_remote,
            date_posted=date_posted,
            job_type=job_types,
            emails=emails,
        )


def _parse_lever_location(loc_str: str) -> Location:
    """Parse Lever location string."""
    if not loc_str:
        return Location()
    parts = [p.strip() for p in loc_str.split(",")]
    if len(parts) >= 2:
        return Location(city=parts[0], state=parts[-1] if len(parts[-1]) <= 3 else None,
                        country=parts[-1] if len(parts[-1]) > 3 else None)
    return Location(city=parts[0])
