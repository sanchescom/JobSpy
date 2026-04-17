"""Helper functions for extracting structured job data from tweets."""
from __future__ import annotations

import re

from jobspy.twitter.constant import (
    TITLE_PATTERNS,
    LOCATION_PATTERNS,
    COMPANY_PATTERNS,
    REMOTE_KEYWORDS,
    FLAG_EMOJI_TO_CC,
    COUNTRY_NAME_TO_CC,
    CITY_TO_CC,
    CURRENCY_TO_CC,
    PHONE_PREFIX_TO_CC,
    TLD_TO_CC,
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


# Word-boundary regex for country aliases. Precompiled at module load.
_COUNTRY_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(COUNTRY_NAME_TO_CC, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
# Cities can be multi-word; still match on word boundaries.
_CITY_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(CITY_TO_CC, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
# Phone prefix: +<digits> with optional separators, anchored so "+44 20 ..." matches "+44".
# Sort longest-first so "+353" wins over "+3".
_PHONE_PREFIX_RE = re.compile(
    r"(?<![\d])(" + "|".join(re.escape(p) for p in sorted(PHONE_PREFIX_TO_CC, key=len, reverse=True)) + r")(?=[\d\s().-])"
)


def _scan_flag_emojis(text: str) -> str | None:
    for emoji, cc in FLAG_EMOJI_TO_CC.items():
        if emoji in text:
            return cc
    return None


def _scan_country_names(text: str) -> str | None:
    m = _COUNTRY_NAME_RE.search(text)
    if m:
        return COUNTRY_NAME_TO_CC.get(m.group(1).lower())
    return None


def _scan_cities(text: str) -> str | None:
    m = _CITY_RE.search(text)
    if m:
        return CITY_TO_CC.get(m.group(1).lower())
    return None


def _scan_phone_prefix(text: str) -> str | None:
    m = _PHONE_PREFIX_RE.search(text)
    if not m:
        return None
    return PHONE_PREFIX_TO_CC.get(m.group(1))


def _scan_tlds(text: str) -> str | None:
    # Longest suffix wins — ".co.uk" > ".uk".
    lowered = text.lower()
    for tld in sorted(TLD_TO_CC, key=len, reverse=True):
        # require tld followed by non-letter boundary (/, ?, space, end, digit)
        for m in re.finditer(re.escape(tld) + r"(?![a-z])", lowered):
            return TLD_TO_CC[tld]
    return None


def _scan_currency(text: str) -> str | None:
    lowered = text.lower()
    for token, cc in CURRENCY_TO_CC.items():
        if cc == "EU":
            continue
        # Alphabetic tokens (e.g. "gbp", "inr") must match on word boundaries,
        # otherwise "inr" hits "engineering", "sek" hits "besieged", etc.
        if token.isalpha():
            if re.search(r"\b" + re.escape(token) + r"\b", lowered):
                return cc
        elif token in lowered:
            return cc
    # Weak EUR signal: only return if nothing else was found and "€" appears.
    if "€" in text:
        return "EU"
    return None


# Twitter's own URL shorteners & profile links — they pollute TLD/city scans
# (e.g. ``t.co/...`` would hit Colombia, ``x.com/tesla`` would hit nothing but
# adds noise). Strip them before running detectors.
_TWITTER_URL_RE = re.compile(
    r"https?://(?:t\.co|x\.com|twitter\.com|mobile\.twitter\.com)/\S*",
    re.IGNORECASE,
)


def _strip_twitter_urls(text: str) -> str:
    return _TWITTER_URL_RE.sub(" ", text)


def extract_country_from_tweet(
    text: str,
    user_location: str | None = None,
    place=None,
) -> str | None:
    """Detect the country the job is in from a tweet. Returns ISO-2 (e.g. "DE", "GB")
    or ``None`` when no confident signal is found.

    Signals tried in order of reliability:
      1. tweet.place metadata (if Twitter attached one)
      2. flag emojis in tweet text (🇩🇪 🇺🇸 …)
      3. explicit country names / aliases in tweet text
      4. major city names
      5. phone calling-code prefix (+91, +44, …)
      6. TLDs in links embedded in the tweet
      7. strong currency signals (£, ₹)
      8. user profile location (same detectors, as a last-resort fallback)
    """
    # (1) structured place
    if place is not None:
        country_code = getattr(place, "country_code", None) or getattr(place, "countryCode", None)
        if country_code:
            code = str(country_code).upper()
            if len(code) == 2:
                return code

    cleaned = _strip_twitter_urls(text)

    scanners = (
        _scan_flag_emojis,
        _scan_country_names,
        _scan_cities,
        _scan_phone_prefix,
        _scan_tlds,
        _scan_currency,
    )
    for scan in scanners:
        cc = scan(cleaned)
        if cc and cc != "EU":
            return cc

    # Fallback to user's profile location (same signals, narrower content).
    if user_location:
        for scan in (_scan_flag_emojis, _scan_country_names, _scan_cities):
            cc = scan(user_location)
            if cc and cc != "EU":
                return cc

    return None
