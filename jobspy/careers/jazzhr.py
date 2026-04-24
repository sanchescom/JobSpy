from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:jazzhr")

# JazzHR career pages are server-rendered HTML at https://{subdomain}.applytojob.com/apply
# No public JSON API — we scrape the HTML listing page, then detail pages for descriptions.
ENRICHMENT_WORKERS = 5
ENRICHMENT_DELAY = 0.2


class JazzHRParser(BaseATSParser):
    platform = "jazzhr"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        subdomain = self._extract_subdomain(career_url)
        results = self._scrape_listing(career_url, subdomain, company_name)

        # Enrich with descriptions from detail pages
        if results:
            self._enrich_descriptions(results)

        logger.info("JazzHR: %d jobs from %s", len(results), subdomain)
        return results

    @staticmethod
    def _extract_subdomain(career_url: str) -> str:
        """Extract subdomain from https://{company}.applytojob.com/apply URLs."""
        parsed = urlparse(career_url)
        hostname = parsed.hostname or ""
        return hostname.split(".")[0]

    def _scrape_listing(self, career_url: str, subdomain: str, company_name: str) -> list[JobPost]:
        """Scrape job listings from JazzHR HTML career page."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("BeautifulSoup not installed, cannot scrape JazzHR HTML")
            return []

        try:
            resp = self.session.get(career_url, timeout=30)
            if not resp.ok:
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            results = []

            # JazzHR uses Bootstrap list-group structure:
            #   <li class="list-group-item">
            #     <h3 class="list-group-item-heading"><a href="...">Title</a></h3>
            #     <ul class="list-group-item-text">
            #       <li><i class="fa fa-map-marker"></i> Location</li>
            #       <li><i class="fa fa-sitemap"></i> Department</li>
            #     </ul>
            #   </li>
            items = soup.select("li.list-group-item")
            for item in items:
                heading = item.select_one("h3.list-group-item-heading a, .list-group-item-heading a")
                if not heading:
                    continue

                title = heading.get_text(strip=True)
                href = heading.get("href", "")
                if not title or not href:
                    continue

                # Extract job code from URL: /apply/{code}/{slug}
                job_code = _extract_job_code(href)
                if not job_code:
                    continue

                # Normalize URL
                if href.startswith("/"):
                    href = f"https://{subdomain}.applytojob.com{href}"

                # Extract location from fa-map-marker list item
                loc_text = ""
                map_marker = item.select_one("i.fa-map-marker")
                if map_marker and map_marker.parent:
                    loc_text = map_marker.parent.get_text(strip=True)

                # Detect remote from title or location
                is_remote = bool(re.search(r'\bremote\b', (title + " " + loc_text).lower()))

                results.append(JobPost(
                    id=f"jazzhr:{subdomain}:{job_code}",
                    title=title,
                    company_name=company_name,
                    job_url=href,
                    location=_parse_location_str(loc_text),
                    is_remote=is_remote,
                ))

            return results
        except Exception as e:
            logger.warning("JazzHR scrape failed for %s: %s", subdomain, e)
            return []

    def _enrich_descriptions(self, results: list[JobPost]) -> None:
        """Fetch descriptions concurrently from detail pages."""
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return

        enriched = 0

        def _fetch_detail(post: JobPost) -> tuple[JobPost, str | None]:
            time.sleep(ENRICHMENT_DELAY)
            try:
                resp = self.session.get(post.job_url, timeout=15)
                if resp.ok:
                    soup = BeautifulSoup(resp.text, "html.parser")
                    desc_el = soup.select_one("#job-description")
                    if desc_el:
                        return post, str(desc_el)
            except Exception:
                pass
            return post, None

        with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as pool:
            futures = [pool.submit(_fetch_detail, post) for post in results]
            for future in as_completed(futures):
                post, desc_html = future.result()
                if not desc_html:
                    continue
                converted = markdown_converter(desc_html)
                if converted:
                    post.description = converted
                    post.emails = extract_emails_from_text(converted)
                    post.job_type = extract_job_type(converted)
                    enriched += 1

        logger.info("JazzHR: enriched %d/%d jobs with descriptions", enriched, len(results))


def _extract_job_code(url: str) -> str:
    """Extract the job code from a JazzHR URL like /apply/{code}/{slug}."""
    match = re.search(r'/apply/([A-Za-z0-9]+)/', url)
    if match:
        return match.group(1)
    # Fallback: last path segment
    parts = [p for p in url.rstrip("/").split("/") if p]
    return parts[-1] if parts else ""


def _parse_location_str(loc_str: str) -> Location:
    """Parse location string like 'Remote, ITPL Bangalore, India' or 'Bangalore, Karnataka, India'."""
    if not loc_str:
        return Location()
    # Remove 'Remote,' prefix if present
    cleaned = re.sub(r'^remote\s*,?\s*', '', loc_str, flags=re.IGNORECASE).strip()
    # Remove parenthetical notes like '(Hybrid, PHILIPPINES)'
    cleaned = re.sub(r'\([^)]*\)\s*,?\s*', '', cleaned).strip().strip(",").strip()
    if not cleaned:
        return Location()
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    if len(parts) >= 3:
        return Location(city=parts[0], state=parts[1], country=parts[2])
    if len(parts) == 2:
        return Location(city=parts[0], country=parts[1])
    return Location(city=parts[0])
