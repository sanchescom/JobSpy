from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location, Compensation, CompensationInterval
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:ashby")

# Ashby posting API: returns all jobs with optional compensation data
POSTING_API_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"


class AshbyParser(BaseATSParser):
    platform = "ashby"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        slug = self._extract_slug(career_url)
        url = POSTING_API_URL.format(slug=slug)

        resp = self.session.get(url, timeout=30)
        if not resp.ok:
            logger.warning("Ashby API %d for %s", resp.status_code, slug)
            return []

        data = resp.json()
        jobs_data = data.get("jobs", [])
        results = []

        for job in jobs_data:
            try:
                results.append(self._parse_job(job, slug, company_name))
            except Exception as e:
                logger.debug("Failed to parse Ashby job: %s", e)

        logger.info("Ashby: %d jobs from %s", len(results), slug)
        return results

    @staticmethod
    def _extract_slug(career_url: str) -> str:
        """Extract slug from https://jobs.ashbyhq.com/{slug} URLs."""
        parsed = urlparse(career_url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        return parts[0] if parts else ""

    @staticmethod
    def _parse_job(job: dict, slug: str, company_name: str) -> JobPost:
        job_id = job.get("id", "")
        title = job.get("title", "")
        job_url = job.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{job_id}"

        # Location
        loc_str = job.get("location") or ""
        if isinstance(loc_str, dict):
            loc_str = loc_str.get("name", "")
        location = _parse_ashby_location(loc_str)

        # Description
        description_html = job.get("descriptionHtml") or job.get("description", "")
        description = markdown_converter(description_html)

        # Date
        date_posted = None
        published = job.get("publishedAt") or job.get("updatedAt")
        if published:
            try:
                date_posted = datetime.fromisoformat(published.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                pass

        is_remote = job.get("isRemote", False) or ("remote" in loc_str.lower())

        # Compensation
        compensation = None
        comp_data = job.get("compensation")
        if comp_data and isinstance(comp_data, dict):
            comp_range = comp_data.get("compensationTierSummary") or ""
            # Try to parse from tier data
            tiers = comp_data.get("compensationTiers", [])
            if tiers:
                tier = tiers[0]
                min_val = tier.get("min")
                max_val = tier.get("max")
                currency = tier.get("currencyCode", "USD")
                interval_str = tier.get("interval", "").upper()
                interval = _map_interval(interval_str)
                if min_val is not None or max_val is not None:
                    compensation = Compensation(
                        interval=interval,
                        min_amount=float(min_val) if min_val is not None else None,
                        max_amount=float(max_val) if max_val is not None else None,
                        currency=currency,
                    )

        job_types = extract_job_type(description) if description else None
        employment_type = job.get("employmentType", "")
        if employment_type and not job_types:
            job_types = extract_job_type(employment_type)

        emails = extract_emails_from_text(description) if description else None

        return JobPost(
            id=f"ashby:{slug}:{job_id}",
            title=title,
            company_name=company_name,
            job_url=job_url,
            location=location,
            description=description,
            is_remote=is_remote,
            date_posted=date_posted,
            compensation=compensation,
            job_type=job_types,
            emails=emails,
        )


def _map_interval(interval_str: str) -> CompensationInterval:
    mapping = {
        "YEAR": CompensationInterval.YEARLY,
        "YEARLY": CompensationInterval.YEARLY,
        "MONTH": CompensationInterval.MONTHLY,
        "MONTHLY": CompensationInterval.MONTHLY,
        "HOUR": CompensationInterval.HOURLY,
        "HOURLY": CompensationInterval.HOURLY,
        "WEEK": CompensationInterval.WEEKLY,
        "WEEKLY": CompensationInterval.WEEKLY,
    }
    return mapping.get(interval_str, CompensationInterval.YEARLY)


def _parse_ashby_location(loc_str: str) -> Location:
    if not loc_str:
        return Location()
    parts = [p.strip() for p in loc_str.split(",")]
    if len(parts) >= 2:
        return Location(city=parts[0], country=parts[-1])
    return Location(city=parts[0])
