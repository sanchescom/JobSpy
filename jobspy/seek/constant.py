headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-AU,en;q=0.9",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}

SEEK_SITES = {
    "australia": {
        "base_url": "https://www.seek.com.au",
        "site_key": "AU-Main",
        "locale": "en-AU",
    },
    "new zealand": {
        "base_url": "https://www.seek.co.nz",
        "site_key": "NZ-Main",
        "locale": "en-NZ",
    },
}

SEARCH_API_PATH = "/api/jobsearch/v5/search"

WORK_TYPE_MAP = {
    "full time": "fulltime",
    "part time": "parttime",
    "contract": "contract",
    "casual": "parttime",
    "temporary": "temporary",
}
