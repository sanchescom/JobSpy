"""Twitter/X job scraper.

Scrapes X search results from a logged-in browser DOM (patchright). The old
twscrape HTTP path is gone: X's mid-2026 ``/x-web/x-web/`` frontend migration
broke ``x-client-transaction-id`` signing, and logged-out search is redirected
to onboarding — so the only reliable path is a real browser session. The
browser logs in once into a persistent profile (see ``browser_login``) and we
read tweets straight out of the rendered timeline.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime

from jobspy.model import (
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.twitter.browser_search import search_tweets_via_browser
from jobspy.twitter.constant import HASHTAG_GROUPS, MIN_TWEET_LENGTH
from jobspy.twitter.util import (
    extract_company_from_tweet,
    extract_country_from_tweet,
    extract_job_url_from_tweet,
    extract_location_from_tweet,
    extract_title_from_tweet,
    is_remote_job,
)
from jobspy.util import extract_job_type

log = logging.getLogger("JobSpy:Twitter")


class Twitter(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        twitter_accounts: str | list[dict] | None = None,
        twitter_db_path: str | None = None,  # accepted for backward-compat; unused
    ):
        super().__init__(Site.TWITTER, proxies=proxies, ca_cert=ca_cert)

        # Parse accounts: accept JSON string or list of dicts
        if isinstance(twitter_accounts, str):
            try:
                self.accounts = json.loads(twitter_accounts) if twitter_accounts else []
            except json.JSONDecodeError:
                log.error("Invalid twitter_accounts JSON: %s", twitter_accounts)
                self.accounts = []
        elif isinstance(twitter_accounts, list):
            self.accounts = twitter_accounts
        else:
            self.accounts = []

        # Resolve proxy string (browser relay expects "http://..." format)
        self.proxy_url = None
        if proxies:
            self.proxy_url = proxies[0] if isinstance(proxies, list) else proxies

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        query = self._build_query(scraper_input)
        limit = scraper_input.results_wanted

        if not self.accounts:
            log.warning("No Twitter accounts configured — cannot scrape X search")
            return JobResponse(jobs=[])

        # Use the first account. The persistent browser profile keeps it logged
        # in across runs; login only re-fires when the session expires.
        account = self.accounts[0]

        tweets = search_tweets_via_browser(
            query=query,
            limit=limit,
            account=account,
            proxy=self.proxy_url,
        )

        jobs: list[JobPost] = []
        for t in tweets:
            job = self._parse_tweet(t)
            if job:
                jobs.append(job)
                if len(jobs) >= limit:
                    break

        log.info("Parsed %d job posts from Twitter for query: %s", len(jobs), query)
        return JobResponse(jobs=jobs)

    def _build_query(self, scraper_input: ScraperInput) -> str:
        """Build an X search query from ScraperInput (X search operators)."""
        parts = []

        if scraper_input.search_term:
            parts.append(scraper_input.search_term)

        general_tags = " OR ".join(HASHTAG_GROUPS["general"])
        parts.append(f"({general_tags})")

        if scraper_input.location:
            parts.append(scraper_input.location)

        if scraper_input.job_type:
            jt = scraper_input.job_type
            if jt == JobType.FULL_TIME:
                parts.append("#fulltime")
            elif jt == JobType.PART_TIME:
                parts.append("#parttime")
            elif jt == JobType.CONTRACT:
                parts.append("#contract")
            elif jt == JobType.INTERNSHIP:
                parts.append("#internship")

        parts.append("-is:retweet lang:en")
        return " ".join(parts)

    @staticmethod
    def _resolve_links(links: list[dict]) -> list[str]:
        """Turn DOM link dicts into candidate URL strings.

        X renders external links with a ``t.co`` shim href but the *real* URL
        as the visible text. Prefer the URL-looking text, fall back to href.
        """
        urls: list[str] = []
        for link in links or []:
            text = (link.get("text") or "").strip()
            href = (link.get("href") or "").strip()
            if text and ("://" in text or "." in text.split("/")[0]):
                url = text if text.startswith("http") else f"https://{text}"
                urls.append(url)
            elif href:
                urls.append(href)
        return urls

    def _parse_tweet(self, t: dict) -> JobPost | None:
        """Convert a DOM tweet dict into a JobPost."""
        text = t.get("text") or ""
        if len(text) < MIN_TWEET_LENGTH:
            return None

        title = extract_title_from_tweet(text)
        if not title:
            return None

        handle = t.get("handle") or ""
        name = t.get("name") or handle
        company = extract_company_from_tweet(text, name)

        tweet_url = t.get("permalink") or (
            f"https://x.com/{handle}/status/{t['id']}" if handle and t.get("id") else ""
        )
        job_url = extract_job_url_from_tweet(self._resolve_links(t.get("links")), tweet_url)

        location_str = extract_location_from_tweet(text)
        country_code = extract_country_from_tweet(text)
        if location_str or country_code:
            location = Location(city=location_str, country=country_code)
        else:
            location = None

        date_posted = self._parse_iso_date(t.get("iso"))
        remote = is_remote_job(text)
        job_types = extract_job_type(text)

        return JobPost(
            title=title,
            company_name=company,
            job_url=job_url,
            location=location,
            description=text,
            date_posted=date_posted,
            is_remote=remote if remote else None,
            job_type=job_types if job_types else None,
        )

    @staticmethod
    def _parse_iso_date(iso: str | None) -> date | None:
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            return None
