BASE_URL = "https://www.irishjobs.ie"
SEARCH_URL = "https://www.irishjobs.ie/jobs"

RESULTS_PER_PAGE = 25

headers = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
}

# StepStone Group React SPA selectors (discovered via Playwright rendering)
# Uses data-at attributes (StepStone convention), with data-testid fallbacks
SELECTORS = {
    # Job card container on search results page
    "job_card": "[data-testid='job-item']",
    # Title link inside job card
    "job_title": "a[data-testid='job-item-title'], a[data-at='job-item-title']",
    # Company name
    "company": "[data-at='job-item-company-name'], span[data-at='job-item-company-name']",
    # Location
    "location": "[data-at='job-item-location'], span[data-at='job-item-location']",
    # Salary
    "salary": "[data-at='job-item-salary-info'], span[data-at='job-item-salary-info']",
    # Date posted
    "date": "[data-at='job-item-timeago'], span[data-at='job-item-timeago']",
    # Job type (full-time, contract, etc.) — not present in card, extracted from description
    "job_type": "[data-at='job-item-type'], span[class*='job-type']",
    # Pagination
    "next_page": "a[data-testid='pagination-next'], a[rel='next'], a[aria-label='Next page'], button[aria-label='Next page']",
    # Job description on detail page
    "description": "[data-at='job-ad-description'], [data-testid='job-description'], div[class*='job-description'], div[class*='JobDescription']",
}
