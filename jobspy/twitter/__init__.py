"""Twitter/X job scraper using twscrape."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date

from twscrape import API, AccountsPool

# Patch twscrape's x.com HTML parser before we ever make a request — the upstream
# parser is broken against the April 2026 page layout (IndexError in xclid.py).
from jobspy.twitter._twscrape_patch import apply as _apply_twscrape_patch

_apply_twscrape_patch()

from jobspy.model import (
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.twitter.browser_login import (
    BrowserLoginError,
    cookies_to_header,
    login_via_browser,
)
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
        twitter_db_path: str | None = None,
    ):
        super().__init__(Site.TWITTER, proxies=proxies, ca_cert=ca_cert)
        self.db_path = twitter_db_path or os.path.join(
            os.path.dirname(__file__), "accounts.db"
        )

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

        # Resolve proxy string for twscrape (expects "http://..." format)
        self.proxy_url = None
        if proxies:
            if isinstance(proxies, list):
                self.proxy_url = proxies[0]
            else:
                self.proxy_url = proxies

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """Sync entry point — bridges to async twscrape via asyncio.run()."""
        query = self._build_query(scraper_input)
        limit = scraper_input.results_wanted

        # job-api calls scrape_jobs() inside run_in_executor, so no running loop here
        return asyncio.run(self._scrape(query, limit))

    async def _scrape(self, query: str, limit: int) -> JobResponse:
        pool = AccountsPool(self.db_path)

        # Register all accounts that aren't already in the pool, and make
        # sure each one has valid cookies (obtained via a real browser login
        # because twscrape's HTTP login is blocked by Twitter's anti-bot).
        if self.accounts:
            existing = {a.username: a for a in await pool.get_all()}

            for acc in self.accounts:
                username = acc.get("username", "")
                password = acc.get("password", "")
                email = acc.get("email", "")
                email_password = acc.get("email_password", "")
                proxy = acc.get("proxy") or self.proxy_url

                if not username or not password:
                    log.warning("Skipping Twitter account without username/password")
                    continue

                db_acc = existing.get(username)
                if db_acc and db_acc.active and "ct0" in db_acc.cookies:
                    log.info("Twitter account %s already active, skipping login", username)
                    continue

                try:
                    cookies = await asyncio.to_thread(
                        login_via_browser,
                        username,
                        password,
                        email=email or None,
                        email_password=email_password or None,
                        proxy=proxy,
                    )
                except BrowserLoginError as e:
                    log.error("Browser login failed for %s: %s", username, e)
                    continue

                cookie_header = cookies_to_header(cookies)

                if db_acc:
                    # Reuse existing row but refresh cookies
                    await pool.delete_accounts([username])

                await pool.add_account(
                    username,
                    password,
                    email,
                    email_password,
                    proxy=proxy,
                    cookies=cookie_header,
                )
                log.info("Twitter account %s activated with fresh browser cookies", username)

        api = API(pool)
        jobs: list[JobPost] = []

        async def _collect():
            async for tweet in api.search(query, limit=limit * 3):
                job = self._parse_tweet(tweet)
                if job:
                    jobs.append(job)
                    if len(jobs) >= limit:
                        break

        try:
            # Timeout prevents hanging when the single account is rate-limited
            # and twscrape waits 15+ min for the next API slot.
            await asyncio.wait_for(_collect(), timeout=60)
        except asyncio.TimeoutError:
            log.warning("Twitter search timed out after 60s with %d jobs collected", len(jobs))
        except Exception as e:
            log.error("Twitter search failed: %s", e)

        log.info("Parsed %d job posts from Twitter for query: %s", len(jobs), query)
        return JobResponse(jobs=jobs)

    def _build_query(self, scraper_input: ScraperInput) -> str:
        """Build a Twitter search query from ScraperInput."""
        parts = []

        if scraper_input.search_term:
            parts.append(scraper_input.search_term)

        # Add hiring hashtags
        general_tags = " OR ".join(HASHTAG_GROUPS["general"])
        parts.append(f"({general_tags})")

        # Add location if provided
        if scraper_input.location:
            parts.append(scraper_input.location)

        # Add job type hashtag
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

        # Filter out retweets, require English
        parts.append("-is:retweet lang:en")

        return " ".join(parts)

    def _parse_tweet(self, tweet) -> JobPost | None:
        """Convert a twscrape Tweet object into a JobPost."""
        text = tweet.rawContent if hasattr(tweet, "rawContent") else str(tweet)

        # Skip very short tweets — unlikely to be real job postings
        if len(text) < MIN_TWEET_LENGTH:
            return None

        # Extract fields
        title = extract_title_from_tweet(text)
        if not title:
            return None

        user_displayname = ""
        if hasattr(tweet, "user") and tweet.user:
            user_displayname = tweet.user.displayname or tweet.user.username or ""

        company = extract_company_from_tweet(text, user_displayname)

        tweet_url = ""
        if hasattr(tweet, "user") and tweet.user:
            tweet_url = f"https://x.com/{tweet.user.username}/status/{tweet.id}"

        links = tweet.links if hasattr(tweet, "links") else []
        job_url = extract_job_url_from_tweet(links, tweet_url)

        place = tweet.place if hasattr(tweet, "place") else None
        location_str = extract_location_from_tweet(text, place)

        user_location = ""
        if hasattr(tweet, "user") and tweet.user:
            user_location = getattr(tweet.user, "location", "") or ""
        country_code = extract_country_from_tweet(text, user_location, place)

        if location_str or country_code:
            location = Location(city=location_str, country=country_code)
        else:
            location = None

        date_posted = None
        if hasattr(tweet, "date") and tweet.date:
            date_posted = tweet.date.date() if hasattr(tweet.date, "date") else tweet.date

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
