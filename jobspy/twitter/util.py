"""Helper functions for extracting structured job data from tweets."""
from __future__ import annotations

import re

from jobspy.twitter.constant import (
    TITLE_PATTERNS,
    LOCATION_PATTERNS,
    COMPANY_PATTERNS,
    REMOTE_KEYWORDS,
)


def extract_title_from_tweet(text: str) -> str | None:
    """Extract a job title from tweet text using regex patterns.

    Falls back to the first non-empty line (truncated to 100 chars).
    """
    for pattern in TITLE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            title = match.group(1).strip()
            # Clean trailing hashtags
            title = re.sub(r"\s*#\S+", "", title).strip()
            if len(title) > 10:
                return title[:150]

    # Fallback: first non-empty line
    for line in text.split("\n"):
        line = line.strip()
        if len(line) > 10:
            # Strip leading hashtags/emojis
            cleaned = re.sub(r"^[#@\U0001F300-\U0001FAFF\s]+", "", line).strip()
            if cleaned:
                return cleaned[:100]
    return None


def extract_company_from_tweet(text: str, user_displayname: str) -> str:
    """Extract company name from tweet text, falling back to the author's display name."""
    for pattern in COMPANY_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            company = match.group(1).strip()
            company = re.sub(r"\s*#\S+", "", company).strip()
            if len(company) > 1:
                return company[:100]
    return user_displayname


def extract_location_from_tweet(text: str, place=None) -> str | None:
    """Extract location from tweet text or tweet.place metadata."""
    for pattern in LOCATION_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            loc = match.group(1).strip()
            loc = re.sub(r"\s*#\S+", "", loc).strip()
            if loc:
                return loc[:100]

    if place and hasattr(place, "fullName"):
        return place.fullName
    if isinstance(place, str) and place:
        return place
    return None


def is_remote_job(text: str) -> bool:
    """Check if the tweet indicates a remote position."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in REMOTE_KEYWORDS)


def extract_job_url_from_tweet(links: list | None, tweet_url: str) -> str:
    """Return the first external URL from tweet links, or the tweet URL itself."""
    if links:
        for link in links:
            url = link.url if hasattr(link, "url") else str(link)
            # Skip twitter/x.com internal links
            if url and not any(d in url for d in ("twitter.com", "x.com", "t.co")):
                return url
    return tweet_url
