from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location, Compensation, CompensationInterval
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:greenhouse")

# Greenhouse boards API: returns all jobs with content in a single response.
BOARDS_API_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


class GreenhouseParser(BaseATSParser):
    platform = "greenhouse"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        token = self._extract_token(career_url)
        url = BOARDS_API_URL.format(token=token)

        resp = self.session.get(url, timeout=30)
        if not resp.ok:
            logger.warning("Greenhouse API %d for %s", resp.status_code, token)
            return []

        data = resp.json()
        jobs_data = data.get("jobs", [])
        results = []

        for job in jobs_data:
            try:
                results.append(self._parse_job(job, token, company_name))
            except Exception as e:
                logger.debug("Failed to parse Greenhouse job: %s", e)

        logger.info("Greenhouse: %d jobs from %s", len(results), token)
        return results

    @staticmethod
    def _extract_token(career_url: str) -> str:
        """Extract board token from Greenhouse URLs.

        Handles:
        - boards-api.greenhouse.io/v1/boards/{token}/...
        - job-boards.greenhouse.io/{token}
        - boards.greenhouse.io/{token}
        - boards.greenhouse.io/embed/job_board/js?for={token}
        """
        from urllib.parse import parse_qs
        parsed = urlparse(career_url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]

        if "boards-api.greenhouse.io" in (parsed.hostname or ""):
            # /v1/boards/{token}/jobs
            try:
                idx = parts.index("boards")
                return parts[idx + 1]
            except (ValueError, IndexError):
                pass

        # Embed script URL: /embed/job_board/js?for={token}
        if parts and parts[0] == "embed":
            qs = parse_qs(parsed.query)
            token = qs.get("for", [""])[0]
            if token:
                return token

        # job-boards.greenhouse.io/{token} or boards.greenhouse.io/{token}
        return parts[0] if parts else ""

    @staticmethod
    def _parse_job(job: dict, token: str, company_name: str) -> JobPost:
        job_id = str(job.get("id", ""))
        title = job.get("title", "")
        absolute_url = job.get("absolute_url", "")
        job_url = absolute_url or f"https://boards.greenhouse.io/{token}/jobs/{job_id}"

        # Location
        loc_str = job.get("location", {}).get("name", "") if isinstance(job.get("location"), dict) else ""
        location = _parse_greenhouse_location(loc_str)

        # Description
        content = job.get("content", "")
        description = markdown_converter(content)

        date_posted = None
        updated = job.get("updated_at") or job.get("created_at")
        if updated:
            try:
                date_posted = datetime.fromisoformat(updated.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                pass

        is_remote = "remote" in loc_str.lower() if loc_str else False
        job_types = extract_job_type(description) if description else None
        emails = extract_emails_from_text(description) if description else None

        return JobPost(
            id=f"greenhouse:{token}:{job_id}",
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


def _parse_greenhouse_location(loc_str: str) -> Location:
    """Parse Greenhouse location string like 'San Francisco, CA' or 'London, UK'."""
    if not loc_str:
        return Location()

    parts = [p.strip() for p in loc_str.split(",")]
    if len(parts) >= 3:
        return Location(city=parts[0], state=parts[1], country=parts[2])
    elif len(parts) == 2:
        # Could be "City, State" or "City, Country"
        second = parts[1]
        if len(second) == 2:
            return Location(city=parts[0], state=second)
        return Location(city=parts[0], country=second)
    return Location(city=parts[0])
