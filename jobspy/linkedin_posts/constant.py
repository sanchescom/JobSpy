"""URLs and CSS selectors for LinkedIn Posts scraper."""

SEARCH_URL = "https://www.linkedin.com/search/results/content/"
LOGIN_URL = "https://www.linkedin.com/login"
FEED_URL = "https://www.linkedin.com/feed/"

MIN_POST_LENGTH = 30

# CSS selectors for post containers and fields.
# LinkedIn's SPA class names are obfuscated; these target semantic attributes
# and stable structural patterns rather than generated class names.
POST_CONTAINER = "div.feed-shared-update-v2"
POST_TEXT = "div.feed-shared-update-v2__description span[dir='ltr']"
POST_AUTHOR = "span.update-components-actor__name span[aria-hidden='true']"
POST_AUTHOR_SUBTITLE = "span.update-components-actor__description span[aria-hidden='true']"
POST_ACTIVITY_LINK = "a.update-components-actor__meta-link"
POST_TIMESTAMP = "time"
