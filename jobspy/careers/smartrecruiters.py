from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location, Compensation, CompensationInterval
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:smartrecruiters")

# SmartRecruiters public API with offset+limit pagination (max 100 per page)
POSTINGS_URL = "https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
PAGE_SIZE = 100
ENRICHMENT_WORKERS = 5
ENRICHMENT_DELAY = 0.15  # seconds between requests per worker


class SmartRecruitersParser(BaseATSParser):
    platform = "smartrecruiters"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        company_id = self._extract_company_id(career_url)
        results = []
        ref_map: dict[str, str] = {}  # job_id -> ref URL
        offset = 0

        while True:
            url = f"{POSTINGS_URL.format(company_id=company_id)}?offset={offset}&limit={PAGE_SIZE}"
            resp = self.session.get(url, timeout=30)
            if not resp.ok:
                logger.warning("SmartRecruiters API %d for %s", resp.status_code, company_id)
                break

            data = resp.json()
            content = data.get("content", [])
            if not content:
                break

            for job in content:
                try:
                    post = self._parse_job(job, company_id, company_name)
                    results.append(post)
                    ref = job.get("ref", "")
                    if ref:
                        ref_map[post.id] = ref
                except Exception as e:
                    logger.debug("Failed to parse SmartRecruiters job: %s", e)

            total = data.get("totalFound", 0)
            offset += PAGE_SIZE
            if offset >= total:
                break

        # Enrich with descriptions via detail endpoint (concurrent)
        if results and ref_map:
            self._enrich_descriptions(results, ref_map)

        logger.info("SmartRecruiters: %d jobs from %s", len(results), company_id)
        return results

    def _enrich_descriptions(self, results: list[JobPost], ref_map: dict[str, str]) -> None:
        """Fetch descriptions concurrently via detail endpoint."""
        post_by_id = {p.id: p for p in results}
        enriched = 0

        def _fetch_detail(job_id: str, ref_url: str) -> tuple[str, dict | None]:
            time.sleep(ENRICHMENT_DELAY)
            try:
                resp = self.session.get(ref_url, timeout=15)
                if resp.ok:
                    return job_id, resp.json()
            except Exception:
                pass
            return job_id, None

        with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as pool:
            futures = {
                pool.submit(_fetch_detail, jid, ref): jid
                for jid, ref in ref_map.items()
            }
            for future in as_completed(futures):
                job_id, detail = future.result()
                if not detail:
                    continue
                post = post_by_id.get(job_id)
                if not post:
                    continue

                # Use canonical postingUrl if available
                posting_url = detail.get("postingUrl", "")
                if posting_url:
                    post.job_url = posting_url

                # Build description from jobAd sections
                job_ad = detail.get("jobAd", {})
                sections = job_ad.get("sections", {})
                parts = []
                for key in ("jobDescription", "qualifications", "additionalInformation"):
                    html = sections.get(key, {}).get("text", "")
                    if html:
                        converted = markdown_converter(html)
                        if converted:
                            parts.append(converted)
                if parts:
                    post.description = "\n\n".join(parts)
                    post.emails = extract_emails_from_text(post.description)
                    if not post.job_type:
                        post.job_type = extract_job_type(post.description)
                    enriched += 1

        logger.info("SmartRecruiters: enriched %d/%d jobs with descriptions", enriched, len(results))

    @staticmethod
    def _extract_company_id(career_url: str) -> str:
        """Extract company ID from SmartRecruiters career URL."""
        parsed = urlparse(career_url)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        return parts[0] if parts else ""

    @staticmethod
    def _parse_job(job: dict, company_id: str, company_name: str) -> JobPost:
        job_id = str(job.get("id", "") or job.get("uuid", ""))
        title = job.get("name", "")

        # Fallback URL (will be replaced by postingUrl from detail if available)
        job_url = f"https://jobs.smartrecruiters.com/{company_id}/{job_id}"

        # Location
        loc_data = job.get("location", {})
        city = loc_data.get("city", "")
        region = loc_data.get("region", "")
        country = loc_data.get("country", "")
        location = Location(
            city=city or None,
            state=region or None,
            country=country or None,
        )

        # Remote
        is_remote = bool(loc_data.get("remote", False))

        # Compensation
        compensation = None
        comp = job.get("compensation", {})
        if comp:
            min_val = comp.get("min")
            max_val = comp.get("max")
            currency = comp.get("currency", "USD")
            if min_val is not None or max_val is not None:
                compensation = Compensation(
                    interval=CompensationInterval.YEARLY,
                    min_amount=float(min_val) if min_val is not None else None,
                    max_amount=float(max_val) if max_val is not None else None,
                    currency=currency,
                )

        # Date
        date_posted = None
        released = job.get("releasedDate") or job.get("updatedOn")
        if released:
            try:
                date_posted = datetime.fromisoformat(released.replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                pass

        # Employment type
        job_types = None
        type_of_employment = job.get("typeOfEmployment", {})
        if isinstance(type_of_employment, dict):
            emp_label = type_of_employment.get("label", "")
            if emp_label:
                job_types = extract_job_type(emp_label)

        return JobPost(
            id=f"smartrecruiters:{company_id}:{job_id}",
            title=title,
            company_name=company_name,
            job_url=job_url,
            location=location,
            description=None,  # enriched after listing
            is_remote=is_remote,
            date_posted=date_posted,
            compensation=compensation,
            job_type=job_types,
        )
