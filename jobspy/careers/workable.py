from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:workable")

# Workable v3 per-company API (POST, paginated via token).
# Requires residential proxy to bypass Cloudflare Turnstile in production.
JOBS_LIST_URL = "https://apply.workable.com/api/v3/accounts/{slug}/jobs"
JOB_DETAIL_URL = "https://apply.workable.com/api/v2/accounts/{slug}/jobs/{shortcode}"
MAX_PAGES = 30  # 10 jobs/page = 300 jobs max


class WorkableParser(BaseATSParser):
    platform = "workable"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        slug = self._extract_slug(career_url)
        results = []
        token = None

        for page in range(MAX_PAGES):
            body = {}
            if token:
                body["token"] = token

            resp = self.session.post(
                JOBS_LIST_URL.format(slug=slug),
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if not resp.ok:
                logger.warning("Workable API %d for %s (page %d)", resp.status_code, slug, page)
                break

            data = resp.json()
            jobs_data = data.get("results", [])
            if not jobs_data:
                break

            for job in jobs_data:
                try:
                    results.append(self._parse_list_job(job, slug, company_name))
                except Exception as e:
                    logger.debug("Failed to parse Workable job: %s", e)

            token = data.get("nextPage")
            if not token:
                break

        # Fetch descriptions via v2 detail endpoint
        for job_post in results:
            self._enrich_description(job_post, slug)

        logger.info("Workable: %d jobs from %s", len(results), slug)
        return results

    def _enrich_description(self, job_post: JobPost, slug: str) -> None:
        shortcode = (job_post.id or "").split(":")[-1]
        if not shortcode:
            return
        try:
            resp = self.session.get(
                JOB_DETAIL_URL.format(slug=slug, shortcode=shortcode),
                timeout=15,
            )
            if not resp.ok:
                return
            detail = resp.json()
            parts = []
            for field in ("description", "requirements", "benefits"):
                html = detail.get(field, "")
                if html:
                    converted = markdown_converter(html)
                    if converted:
                        parts.append(converted)
            if parts:
                job_post.description = "\n\n".join(parts)
                job_post.emails = extract_emails_from_text(job_post.description)
                job_post.job_type = extract_job_type(job_post.description)
        except Exception as e:
            logger.debug("Failed to fetch Workable detail for %s: %s", shortcode, e)

    @staticmethod
    def _extract_slug(career_url: str) -> str:
        parsed = urlparse(career_url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        return parts[0] if parts else parsed.netloc

    @staticmethod
    def _parse_list_job(job: dict, slug: str, company_name: str) -> JobPost:
        shortcode = job.get("shortcode", "")
        title = job.get("title", "")
        job_url = f"https://apply.workable.com/{slug}/j/{shortcode}/"

        loc = job.get("location", {})
        location = Location(
            city=loc.get("city") if isinstance(loc, dict) else None,
            state=loc.get("region") if isinstance(loc, dict) else None,
            country=loc.get("countryCode") or (loc.get("country") if isinstance(loc, dict) else None),
        )

        is_remote = bool(job.get("remote", False))
        workplace = job.get("workplace", "")
        if isinstance(workplace, str) and "remote" in workplace.lower():
            is_remote = True

        date_posted = None
        published = job.get("published")
        if published:
            try:
                if isinstance(published, (int, float)):
                    date_posted = datetime.fromtimestamp(published / 1000).date()
                else:
                    date_posted = datetime.fromisoformat(str(published).replace("Z", "+00:00")).date()
            except (ValueError, AttributeError, OSError):
                pass

        return JobPost(
            id=f"workable:{slug}:{shortcode}",
            title=title,
            company_name=company_name,
            job_url=job_url,
            location=location,
            description=None,  # enriched later
            is_remote=is_remote,
            date_posted=date_posted,
        )
