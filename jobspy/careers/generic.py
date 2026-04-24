from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urlparse, urljoin

from jobspy.careers.base import BaseATSParser
from jobspy.model import JobPost, Location, Compensation, CompensationInterval
from jobspy.util import markdown_converter, extract_emails_from_text, extract_job_type

logger = logging.getLogger("JobSpy:careers:generic")

# Domains that indicate an embedded ATS
EMBEDDED_ATS_DOMAINS = {
    "apply.workable.com": "workable",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "careers.smartrecruiters.com": "smartrecruiters",
}


class GenericCareerParser(BaseATSParser):
    platform = "custom"

    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        # Strategy 1-2: HTTP fetch → JSON-LD / embedded ATS detection
        jobs = self._try_http(career_url, company_name)
        if jobs:
            return jobs

        # Strategy 3: Sitemap.xml parsing
        jobs = self._try_sitemap(career_url, company_name)
        if jobs:
            return jobs

        # Strategy 4: Headless browser for JS-rendered pages
        jobs = self._try_browser(career_url, company_name)
        if jobs:
            return jobs

        logger.info("Generic: no jobs found at %s (needs_review)", career_url)
        return []

    def _try_http(self, career_url: str, company_name: str) -> list[JobPost]:
        """Try plain HTTP fetch with JSON-LD and embedded ATS detection."""
        try:
            resp = self.session.get(career_url, timeout=30)
            if not resp.ok:
                logger.warning("Generic parser HTTP %d for %s", resp.status_code, career_url)
                return []

            return self._extract_from_html(resp.text, career_url, company_name)
        except Exception as e:
            logger.warning("Generic HTTP fetch failed for %s: %s", career_url, e)
            return []

    def _try_sitemap(self, career_url: str, company_name: str) -> list[JobPost]:
        """Parse sitemap.xml to find job URLs."""
        parsed = urlparse(career_url)
        base = f"{parsed.scheme}://{parsed.netloc}"

        # Collect candidate sitemap URLs from robots.txt and common paths
        sitemap_urls: list[str] = []

        # Try robots.txt first to find declared sitemaps
        robots_urls = self._get_sitemaps_from_robots(base)
        sitemap_urls.extend(robots_urls)

        # Common sitemap locations as fallback
        sitemap_urls.extend([
            f"{base}/sitemap.xml",
            f"{base}/sitemap_index.xml",
        ])

        # Also try parent domain if career URL is on a subdomain (e.g. jobs.netflix.com -> netflix.com)
        # Avoid multi-part TLDs like co.uk, com.au, edu.au etc.
        _MULTI_TLDS = {"co.uk", "org.uk", "ac.uk", "com.au", "org.au", "edu.au",
                        "co.nz", "co.jp", "co.in", "com.br", "com.mx", "co.za",
                        "com.ph", "co.kr", "com.sg", "com.hk", "com.tw", "com.cn",
                        "co.il", "co.th", "com.ar", "com.co", "com.tr", "com.my"}
        hostname = parsed.hostname or ""
        parts = hostname.split(".")
        if len(parts) > 2:
            candidate = ".".join(parts[-2:])
            if candidate in _MULTI_TLDS:
                # e.g. jobs.example.co.uk -> example.co.uk
                parent = f"{parsed.scheme}://{'.'.join(parts[-3:])}" if len(parts) > 3 else None
            else:
                parent = f"{parsed.scheme}://{candidate}"
            if parent and parent != base:
                parent_robots = self._get_sitemaps_from_robots(parent)
                sitemap_urls.extend(parent_robots)
                sitemap_urls.append(f"{parent}/sitemap.xml")

        # Deduplicate while preserving order
        seen_sitemap: set[str] = set()
        unique_sitemaps: list[str] = []
        for u in sitemap_urls:
            if u not in seen_sitemap:
                seen_sitemap.add(u)
                unique_sitemaps.append(u)

        all_job_urls: list[tuple[str, str | None]] = []  # (url, lastmod)

        for sitemap_url in unique_sitemaps:
            job_urls = self._fetch_sitemap_jobs(sitemap_url, base, depth=0)
            all_job_urls.extend(job_urls)
            if all_job_urls:
                break

        if not all_job_urls:
            return []

        # Deduplicate
        seen: set[str] = set()
        unique: list[tuple[str, str | None]] = []
        for url, lastmod in all_job_urls:
            normalized = url.rstrip("/").split("?")[0].split("#")[0]
            if normalized not in seen:
                seen.add(normalized)
                unique.append((url, lastmod))

        jobs: list[JobPost] = []
        for url, lastmod in unique:
            path = urlparse(url).path.rstrip("/")

            # Skip /apply pages (duplicates of job listings)
            if path.endswith("/apply"):
                continue

            if _is_category_url(url):
                continue

            # Extract title from URL slug — use the job name segment, not numeric ID
            segments = path.rsplit("/", 1)
            slug = segments[-1]
            # If last segment is numeric, try the one before it
            if slug.isdigit() and len(segments) > 1:
                parent = segments[0].rsplit("/", 1)[-1]
                if not parent.isdigit():
                    slug = parent

            title = slug.replace("-", " ").replace("_", " ").strip()
            # Remove trailing UUIDs (e.g. "..._79ddaec9 d32b 46c5 822b f439074d896c")
            title = re.sub(
                r'\s+[0-9a-f]{8}\s+[0-9a-f]{4}\s+[0-9a-f]{4}\s+[0-9a-f]{4}\s+[0-9a-f]{12}$',
                '', title, flags=re.IGNORECASE,
            )
            title = title.strip().title()

            if not title or len(title) < 5:
                continue
            # Skip single-word slugs (likely categories)
            if " " not in title.strip() and len(title) < 15:
                continue

            date_posted = None
            if lastmod:
                try:
                    date_posted = datetime.fromisoformat(
                        lastmod.replace("Z", "+00:00")
                    ).date()
                except (ValueError, AttributeError):
                    pass

            job_id = str(hash(url))[:12]
            jobs.append(JobPost(
                id=f"custom:sitemap:{job_id}",
                title=title,
                company_name=company_name,
                job_url=url,
                location=Location(),
                description=None,
                is_remote=False,
                date_posted=date_posted,
            ))

        if len(jobs) >= 2:
            logger.info("Generic (sitemap): %d jobs from %s", len(jobs), career_url)
            return jobs
        return []

    def _fetch_sitemap_jobs(
        self, sitemap_url: str, base: str, depth: int
    ) -> list[tuple[str, str | None]]:
        """Fetch and parse a sitemap, returning job URLs with lastmod dates."""
        if depth > 2:
            return []

        try:
            resp = self.session.get(sitemap_url, timeout=15)
            if not resp.ok:
                return []
        except Exception:
            return []

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            return []

        # Strip namespace for easier parsing
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        results: list[tuple[str, str | None]] = []

        # Check if this is a sitemap index
        child_sitemaps = []
        for sitemap_el in root.findall(f"{ns}sitemap"):
            loc_el = sitemap_el.find(f"{ns}loc")
            if loc_el is not None and loc_el.text:
                child_sitemaps.append(loc_el.text.strip())

        if child_sitemaps:
            # Prefer sitemaps with job-related keywords
            job_sitemaps = [u for u in child_sitemaps if any(
                kw in u.lower() for kw in (
                    "job", "career", "position", "opening", "vacanc",
                    "posting", "hiring",
                )
            )]
            # If none have keywords, follow all (up to 10 to avoid huge sites)
            to_follow = job_sitemaps if job_sitemaps else child_sitemaps[:10]
            for child_url in to_follow:
                results.extend(
                    self._fetch_sitemap_jobs(child_url, base, depth + 1)
                )
                # Stop early if we already found enough
                if len(results) >= 5:
                    break

        # Parse URL entries
        for url_el in root.findall(f"{ns}url"):
            loc_el = url_el.find(f"{ns}loc")
            if loc_el is None or not loc_el.text:
                continue
            url = loc_el.text.strip()

            # Must match job URL pattern
            if not _JOB_URL_PATTERN.search(url):
                continue

            lastmod_el = url_el.find(f"{ns}lastmod")
            lastmod = lastmod_el.text.strip() if lastmod_el is not None and lastmod_el.text else None

            results.append((url, lastmod))

        return results

    def _get_sitemaps_from_robots(self, base_url: str) -> list[str]:
        """Extract sitemap URLs from robots.txt."""
        try:
            resp = self.session.get(f"{base_url}/robots.txt", timeout=10)
            if not resp.ok:
                return []
            urls = []
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    url = line.split(":", 1)[1].strip()
                    if url.startswith("http"):
                        urls.append(url)
            return urls
        except Exception:
            return []

    def _try_browser(self, career_url: str, company_name: str) -> list[JobPost]:
        """Render page with headless browser, intercept XHR, and extract jobs."""
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                logger.debug("No browser engine available for %s", career_url)
                return []

        proxy_arg = None
        relay = None

        if self.proxies:
            proxy_url = self.proxies if isinstance(self.proxies, str) else self.proxies[0]
            parsed = urlparse(proxy_url)
            if parsed.username:
                try:
                    from jobspy.google.proxy_relay import ProxyRelay
                    relay = ProxyRelay(upstream_proxy=proxy_url)
                    relay.start()
                    proxy_arg = {"server": f"http://127.0.0.1:{relay.port}"}
                except Exception as e:
                    logger.debug("ProxyRelay failed: %s, trying direct proxy", e)
                    proxy_arg = {"server": proxy_url}
            else:
                proxy_arg = {"server": proxy_url}

        try:
            with sync_playwright() as p:
                launch_kwargs = {
                    "headless": True,
                    "args": [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                }
                if proxy_arg:
                    launch_kwargs["proxy"] = proxy_arg

                browser = self._launch_browser(p, launch_kwargs)
                try:
                    context = browser.new_context(
                        viewport={"width": 1920, "height": 1080},
                        locale="en-US",
                    )
                    page = context.new_page()

                    # Set up XHR interception to capture JSON API responses
                    captured_json: list[dict] = []
                    page.on("response", lambda resp: _capture_json_response(resp, captured_json))

                    page.goto(career_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass

                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2000)

                    html = page.content()
                    logger.info("Generic browser: rendered %d chars, %d JSON responses from %s",
                                len(html), len(captured_json), career_url)

                    # Strategy 1: JSON-LD / embedded ATS from rendered HTML
                    jobs = self._extract_from_html(html, career_url, company_name)
                    if jobs:
                        return jobs

                    # Strategy 2: Parse captured XHR responses for job data
                    jobs = self._extract_from_xhr(captured_json, career_url, company_name)
                    if jobs:
                        logger.info("Generic (XHR): %d jobs from %s", len(jobs), career_url)
                        return jobs

                    # Strategy 3: Extract job links from current page
                    jobs = _extract_job_links(page, career_url, company_name)
                    if jobs:
                        logger.info("Generic (links): %d jobs from %s", len(jobs), career_url)
                        return jobs

                    # Strategy 4: Follow "all jobs" link and retry
                    jobs_url = _find_jobs_link(page, career_url)
                    if jobs_url:
                        logger.info("Generic: following jobs link %s", jobs_url)
                        captured_json.clear()
                        page.goto(jobs_url, wait_until="domcontentloaded", timeout=30000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(2000)

                        html2 = page.content()
                        jobs = self._extract_from_html(html2, jobs_url, company_name)
                        if jobs:
                            return jobs
                        jobs = self._extract_from_xhr(captured_json, jobs_url, company_name)
                        if jobs:
                            logger.info("Generic (XHR after nav): %d jobs from %s", len(jobs), jobs_url)
                            return jobs
                        jobs = _extract_job_links(page, jobs_url, company_name)
                        if jobs:
                            logger.info("Generic (links after nav): %d jobs from %s", len(jobs), jobs_url)
                            return jobs

                    return []
                finally:
                    browser.close()
        except Exception as e:
            logger.warning("Generic browser failed for %s: %s", career_url, e)
            return []
        finally:
            if relay:
                relay.stop()

    def _extract_from_xhr(self, captured: list[dict], career_url: str, company_name: str) -> list[JobPost]:
        """Parse captured XHR JSON responses for job-like data."""
        best_jobs: list[JobPost] = []

        for entry in captured:
            data = entry["data"]
            arrays = _find_job_arrays(data)
            for path, arr in arrays:
                jobs = []
                for item in arr:
                    try:
                        jobs.append(_parse_xhr_job(item, career_url, company_name))
                    except Exception:
                        continue
                if len(jobs) > len(best_jobs):
                    best_jobs = jobs
                    logger.debug("Generic XHR: %d jobs at path '%s' from %s",
                                 len(jobs), path, entry["url"][:100])

        return best_jobs

    @staticmethod
    def _launch_browser(p, launch_kwargs):
        """Try real Chrome first, then bundled Chromium."""
        try:
            return p.chromium.launch(channel="chrome", **launch_kwargs)
        except Exception:
            pass
        try:
            return p.chromium.launch(channel="chromium", **launch_kwargs)
        except Exception:
            pass
        # Remove channel to use bundled browser
        launch_kwargs.pop("channel", None)
        return p.chromium.launch(**launch_kwargs)

    def _extract_from_html(self, html: str, career_url: str, company_name: str) -> list[JobPost]:
        """Extract jobs from HTML using __NEXT_DATA__, JSON-LD, and embedded ATS detection."""
        # __NEXT_DATA__ (Next.js server-side props)
        jobs = self._extract_next_data(html, career_url, company_name)
        if jobs:
            logger.info("Generic (__NEXT_DATA__): %d jobs from %s", len(jobs), career_url)
            return jobs

        # JSON-LD
        jobs = self._extract_jsonld(html, career_url, company_name)
        if jobs:
            logger.info("Generic (JSON-LD): %d jobs from %s", len(jobs), career_url)
            return jobs

        # Embedded ATS
        ats_url = self._detect_embedded_ats(html, career_url)
        if ats_url:
            logger.info("Generic: detected embedded ATS at %s", ats_url)
            from jobspy.careers import get_parser
            parser = get_parser(ats_url)
            if parser:
                return parser.fetch_jobs(ats_url, company_name)

        return []

    def _extract_jsonld(self, html: str, career_url: str, company_name: str) -> list[JobPost]:
        """Extract jobs from JSON-LD schema.org/JobPosting data."""
        results = []
        pattern = re.compile(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        )

        for match in pattern.finditer(html):
            try:
                data = json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                continue

            job_postings = self._find_job_postings(data)
            for jp in job_postings:
                try:
                    results.append(self._parse_jsonld_job(jp, career_url, company_name))
                except Exception as e:
                    logger.debug("Failed to parse JSON-LD job: %s", e)

        return results

    @staticmethod
    def _find_job_postings(data) -> list[dict]:
        """Recursively find JobPosting objects in JSON-LD data."""
        postings = []
        if isinstance(data, list):
            for item in data:
                postings.extend(GenericCareerParser._find_job_postings(item))
        elif isinstance(data, dict):
            type_val = data.get("@type", "")
            if isinstance(type_val, list):
                type_val = " ".join(type_val)
            if "JobPosting" in str(type_val):
                postings.append(data)
            graph = data.get("@graph", [])
            if isinstance(graph, list):
                for item in graph:
                    postings.extend(GenericCareerParser._find_job_postings(item))
        return postings

    @staticmethod
    def _parse_jsonld_job(jp: dict, career_url: str, company_name: str) -> JobPost:
        """Parse a schema.org/JobPosting JSON-LD object."""
        title = jp.get("title", "")
        job_url = jp.get("url", "") or career_url
        description = jp.get("description", "")
        if description and "<" in description:
            description = markdown_converter(description)

        # Location
        location = Location()
        loc_data = jp.get("jobLocation", {})
        if isinstance(loc_data, list):
            loc_data = loc_data[0] if loc_data else {}
        if isinstance(loc_data, dict):
            address = loc_data.get("address", {})
            if isinstance(address, dict):
                location = Location(
                    city=address.get("addressLocality"),
                    state=address.get("addressRegion"),
                    country=address.get("addressCountry"),
                )

        # Compensation
        compensation = None
        salary = jp.get("baseSalary", {})
        if isinstance(salary, dict):
            currency = salary.get("currency", "USD")
            value = salary.get("value", {})
            if isinstance(value, dict):
                min_val = value.get("minValue")
                max_val = value.get("maxValue")
                unit = value.get("unitText", "YEAR").upper()
                interval = {
                    "YEAR": CompensationInterval.YEARLY,
                    "MONTH": CompensationInterval.MONTHLY,
                    "HOUR": CompensationInterval.HOURLY,
                    "WEEK": CompensationInterval.WEEKLY,
                    "DAY": CompensationInterval.DAILY,
                }.get(unit, CompensationInterval.YEARLY)
                if min_val is not None or max_val is not None:
                    compensation = Compensation(
                        interval=interval,
                        min_amount=float(min_val) if min_val is not None else None,
                        max_amount=float(max_val) if max_val is not None else None,
                        currency=currency,
                    )

        # Date
        date_posted = None
        date_str = jp.get("datePosted")
        if date_str:
            try:
                date_posted = datetime.fromisoformat(str(date_str).replace("Z", "+00:00")).date()
            except (ValueError, AttributeError):
                pass

        # Remote
        remote_vals = jp.get("jobLocationType", "")
        if isinstance(remote_vals, list):
            remote_vals = " ".join(str(v) for v in remote_vals)
        is_remote = "TELECOMMUTE" in str(remote_vals).upper()

        # Employment type
        emp_type = jp.get("employmentType", "")
        if isinstance(emp_type, list):
            emp_type = " ".join(str(e) for e in emp_type)
        job_types = extract_job_type(str(emp_type)) or None

        # Company from hiring org
        hiring_org = jp.get("hiringOrganization", {})
        if isinstance(hiring_org, dict):
            org_name = hiring_org.get("name", "")
            if org_name:
                company_name = org_name

        # ID
        identifier = jp.get("identifier", {})
        if isinstance(identifier, dict):
            job_id = identifier.get("value", "")
        else:
            job_id = str(hash(job_url))[:12]

        emails = extract_emails_from_text(description) if description else None

        return JobPost(
            id=f"custom:jsonld:{job_id}",
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

    @staticmethod
    def _detect_embedded_ats(html: str, career_url: str) -> str | None:
        """Detect embedded ATS iframes or links in HTML."""
        iframe_pattern = re.compile(
            r'<iframe[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE
        )
        for match in iframe_pattern.finditer(html):
            src = match.group(1)
            parsed = urlparse(src)
            hostname = parsed.hostname or ""
            for domain in EMBEDDED_ATS_DOMAINS:
                if hostname == domain or hostname.endswith(f".{domain}"):
                    return src

        link_pattern = re.compile(
            r'(?:href|src|data-src)=["\']([^"\']*(?:'
            + "|".join(re.escape(d) for d in EMBEDDED_ATS_DOMAINS)
            + r')[^"\']*)["\']',
            re.IGNORECASE,
        )
        for match in link_pattern.finditer(html):
            url = match.group(1)
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http"):
                return url

        return None

    def _extract_next_data(self, html: str, career_url: str, company_name: str) -> list[JobPost]:
        """Extract jobs from Next.js __NEXT_DATA__ embedded props."""
        pattern = re.compile(
            r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            re.DOTALL | re.IGNORECASE,
        )
        match = pattern.search(html)
        if not match:
            return []

        try:
            data = json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            return []

        props = data.get("props", {}).get("pageProps", {})
        if not props:
            return []

        # Reuse XHR job array detection on the props data
        arrays = _find_job_arrays(props)
        best_jobs: list[JobPost] = []
        for path, arr in arrays:
            jobs = []
            for item in arr:
                try:
                    jobs.append(_parse_xhr_job(item, career_url, company_name))
                except Exception:
                    continue
            if len(jobs) > len(best_jobs):
                best_jobs = jobs

        return best_jobs


# --- XHR interception helpers ---

# Field names that indicate a "title" in job data (case-insensitive matching)
_TITLE_FIELDS = {"title", "name", "position", "role", "job_title", "jobtitle",
                 "jobpostingtitle", "positiontitle", "jobtitle", "job_name"}
_LOCATION_FIELDS = {"location", "city", "office", "country", "region", "area",
                     "locations", "alllocation", "alllocations"}
_ID_FIELDS = {"id", "job_id", "jobid", "requisitionid", "positionid", "slug",
              "external_id", "greenhouseid", "uid"}
_DESC_FIELDS = {"description", "desc", "summary", "content", "jobdescription",
                "job_description", "shortdescription"}
_URL_FIELDS = {"url", "job_url", "joburl", "apply_url", "applyurl", "link", "href",
               "canonical_url", "absoluteurl"}

# Domains to ignore (analytics, tracking, consent, etc.)
_IGNORE_DOMAINS = {"google", "facebook", "segment", "sentry", "onetrust", "cookielaw",
                   "hotjar", "amplitude", "mixpanel", "newrelic", "datadoghq",
                   "go-mpulse", "boomerang", "transcend", "cdn.cookielaw"}


def _capture_json_response(response, captured: list[dict]) -> None:
    """Capture JSON responses from XHR/fetch calls."""
    try:
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        url = response.url
        # Skip analytics/tracking domains
        hostname = urlparse(url).hostname or ""
        if any(d in hostname for d in _IGNORE_DOMAINS):
            return
        body = response.text()
        if len(body) < 200:
            return
        data = json.loads(body)
        captured.append({"url": url, "data": data})
    except Exception:
        pass


def _find_job_arrays(data, path: str = "", depth: int = 0) -> list[tuple[str, list[dict]]]:
    """Recursively find arrays of job-like objects in parsed JSON."""
    if depth > 5:
        return []
    results = []
    if isinstance(data, list) and len(data) >= 3:
        if isinstance(data[0], dict):
            keys_lower = {k.lower() for k in data[0].keys()}
            has_title = bool(keys_lower & _TITLE_FIELDS)
            has_loc_or_id = bool(keys_lower & (_LOCATION_FIELDS | _ID_FIELDS))
            if has_title and has_loc_or_id:
                results.append((path, data))
    elif isinstance(data, dict):
        for k, v in data.items():
            child_path = f"{path}.{k}" if path else k
            results.extend(_find_job_arrays(v, child_path, depth + 1))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            if isinstance(v, (dict, list)):
                results.extend(_find_job_arrays(v, f"{path}[{i}]", depth + 1))
    return results


def _get_nested(item: dict, fields: set[str]) -> str | None:
    """Get first matching field value from a dict (case-insensitive)."""
    for k, v in item.items():
        if k.lower() in fields:
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                # e.g. location: {city: "...", country: "..."}
                return ", ".join(str(x) for x in v.values() if x)
            if isinstance(v, list):
                return ", ".join(str(x) for x in v if isinstance(x, str))
    return None


def _parse_xhr_job(item: dict, career_url: str, company_name: str) -> JobPost:
    """Parse a job object from an intercepted XHR response."""
    title = _get_nested(item, _TITLE_FIELDS) or ""
    if not title or len(title) < 3:
        raise ValueError("No title field")
    # Filter obvious non-job entries
    title_lower = title.lower().strip()
    _NON_JOB_TITLES = {
        "about us", "contact", "home", "careers", "about", "blog", "news",
        "faq", "privacy", "terms", "login", "sign up", "facebook", "twitter",
        "linkedin", "linked in", "instagram", "youtube", "tiktok", "glassdoor",
        "indeed", "x", "administrative", "engineering", "marketing", "sales",
        "finance", "operations", "legal", "design", "product", "data",
        "north america", "south america", "europe", "asia", "remote",
    }
    if title_lower in _NON_JOB_TITLES:
        raise ValueError(f"Non-job title: {title}")
    # Titles must be at least 2 words (single-word entries are usually categories)
    if " " not in title.strip() and len(title) < 20:
        raise ValueError(f"Single-word title: {title}")

    job_url = _get_nested(item, _URL_FIELDS) or career_url
    if job_url and not job_url.startswith("http"):
        parsed_base = urlparse(career_url)
        job_url = f"{parsed_base.scheme}://{parsed_base.netloc}{job_url}"

    description = _get_nested(item, _DESC_FIELDS)
    if description and "<" in description:
        description = markdown_converter(description)

    loc_str = _get_nested(item, _LOCATION_FIELDS)
    location = Location()
    if loc_str:
        parts = [p.strip() for p in loc_str.split(",") if p.strip()]
        if len(parts) >= 3:
            location = Location(city=parts[0], state=parts[1], country=parts[2])
        elif len(parts) == 2:
            location = Location(city=parts[0], country=parts[1])
        elif len(parts) == 1:
            location = Location(city=parts[0])

    job_id = _get_nested(item, _ID_FIELDS) or str(hash(title + job_url))[:12]

    is_remote = False
    for k, v in item.items():
        sv = str(v).lower()
        if "remote" in sv and k.lower() in ("location", "type", "worktype",
                                              "remotetype", "locationType"):
            is_remote = True
            break

    date_posted = None
    for k, v in item.items():
        if k.lower() in ("date", "dateposted", "date_posted", "creationdate",
                          "created_at", "publisheddate", "jobpostingstartdate",
                          "posted_date", "posteddate"):
            if isinstance(v, str) and v:
                try:
                    date_posted = datetime.fromisoformat(v.replace("Z", "+00:00")).date()
                except (ValueError, AttributeError):
                    pass
            break

    emails = extract_emails_from_text(description) if description else None
    job_types = extract_job_type(description or title) if (description or title) else None

    return JobPost(
        id=f"custom:xhr:{job_id}",
        title=title,
        company_name=company_name,
        job_url=job_url,
        location=location,
        description=description,
        is_remote=is_remote,
        date_posted=date_posted,
        emails=emails,
        job_type=job_types,
    )


# Patterns for links that lead to job search/listing pages
_JOBS_LINK_PATTERNS = re.compile(
    r'/(?:jobs|positions|openings|search|results|all-jobs|open-roles|vacancies)'
    r'(?:/|$|\?)',
    re.IGNORECASE,
)
_JOBS_LINK_TEXT = re.compile(
    r'\b(?:all\s+jobs|view\s+(?:all\s+)?(?:jobs|positions|openings|roles)'
    r'|search\s+(?:jobs|positions|openings)|open\s+(?:positions|roles)'
    r'|see\s+(?:all\s+)?(?:jobs|positions|openings|roles)'
    r'|browse\s+(?:jobs|positions|openings|roles))\b',
    re.IGNORECASE,
)


def _find_jobs_link(page, career_url: str) -> str | None:
    """Find a link to the job search/listing page on a career landing page."""
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(el => ({
                href: el.href,
                text: (el.textContent || '').trim().substring(0, 100),
                visible: el.offsetParent !== null
            })).filter(l => l.visible && l.href && l.href.startsWith('http'))"""
        )
    except Exception:
        return None

    base_host = urlparse(career_url).hostname or ""

    for link in links:
        href = link.get("href", "")
        text = link.get("text", "")
        link_host = urlparse(href).hostname or ""

        # Must be same domain or subdomain
        if not (link_host == base_host or link_host.endswith(f".{base_host}")
                or base_host.endswith(f".{link_host}")):
            continue

        # Skip if it's the same page
        if href.rstrip("/") == career_url.rstrip("/"):
            continue

        # Match by URL pattern or link text
        if _JOBS_LINK_PATTERNS.search(href) or _JOBS_LINK_TEXT.search(text):
            return href

    return None


# Pattern for individual job posting URLs
_JOB_URL_PATTERN = re.compile(
    r'/(?:careers|jobs|positions|openings|vacancies|job|listing|o|role|opportunity)'
    r'/[a-z0-9][\w-]{5,}',  # slug must be 6+ chars, start with alphanumeric
    re.IGNORECASE,
)

# URLs that look like category/landing pages rather than individual job posts
# Exact path segments (must end with / or end of path)
_CATEGORY_EXACT = re.compile(
    r'/(?:teams?|departments?|disciplines?|locations?|benefits?|culture'
    r'|about|faq|students?|campus|intern(?:ship)?s?|inclusion|diversity|values'
    r'|search|results|all-jobs|open-roles|blog|stories?|news|press'
    r'|principles?|extraordinary|university|people|mission|impact'
    r'|sustainability|contact|login|sign-?up|apply-?now|subscribe|newsletter)'
    r'(?:/|$|\?)',
    re.IGNORECASE,
)
# Prefix patterns (match start of path segment)
_CATEGORY_PREFIX = re.compile(
    r'/(?:life-at|why-|how-we|our-|how-to|work-at)',
    re.IGNORECASE,
)


def _is_category_url(url: str) -> bool:
    return bool(_CATEGORY_EXACT.search(url) or _CATEGORY_PREFIX.search(url))


def _extract_job_links(page, career_url: str, company_name: str) -> list[JobPost]:
    """Extract job posts from visible links on the page (last-resort strategy).

    Looks for links matching career/job URL patterns and creates basic
    JobPost objects with title extracted from link text.
    """
    try:
        links = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(el => ({
                href: el.href,
                text: (el.textContent || '').trim().substring(0, 200),
                visible: el.offsetParent !== null
            })).filter(l => l.visible && l.href && l.href.startsWith('http')
                         && l.text.length >= 5 && l.text.length <= 150)"""
        )
    except Exception:
        return []

    base_host = urlparse(career_url).hostname or ""
    seen_urls: set[str] = set()
    jobs: list[JobPost] = []

    for link in links:
        href = link.get("href", "")
        text = link.get("text", "").strip()
        link_host = urlparse(href).hostname or ""

        # Must be same domain or subdomain
        if not (link_host == base_host or link_host.endswith(f".{base_host}")
                or base_host.endswith(f".{link_host}")):
            continue

        # Must match job URL pattern
        if not _JOB_URL_PATTERN.search(href):
            continue

        # Skip category/landing pages
        if _is_category_url(href):
            continue

        # Skip duplicates
        url_normalized = href.rstrip("/").split("?")[0].split("#")[0]
        if url_normalized in seen_urls:
            continue
        seen_urls.add(url_normalized)

        # Clean up title text (remove extra whitespace, newlines)
        title = " ".join(text.split())
        if not title or len(title) < 5:
            continue

        # Skip obvious non-job titles
        title_lower = title.lower()
        _SKIP_TITLES = {
            "read more", "learn more", "apply now", "see details",
            "view more", "click here", "see all", "show more",
            "diversity and inclusion", "university recruiting",
            "meet more teams", "applicant privacy policy",
            "privacy policy", "terms of service", "cookie policy",
            "product principles", "work life philosophy",
        }
        if title_lower in _SKIP_TITLES:
            continue
        # Skip CTA-like text and navigation items
        if any(kw in title_lower for kw in (
            "skip to", "back to", "sign up", "log in", "cookie",
            "privacy", "terms of", "subscribe",
        )):
            continue

        job_id = str(hash(href))[:12]
        jobs.append(JobPost(
            id=f"custom:link:{job_id}",
            title=title,
            company_name=company_name,
            job_url=href,
            location=Location(),
            description=None,
            is_remote=False,
        ))

    # Only return if we found a meaningful number of links (>= 2)
    # to avoid false positives from random career-like URLs
    if len(jobs) < 2:
        return []

    return jobs
