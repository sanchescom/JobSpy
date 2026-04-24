from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from jobspy.model import JobPost
from jobspy.util import create_session

logger = logging.getLogger("JobSpy:careers")


class BaseATSParser(ABC):
    platform: str = ""

    def __init__(self, proxies=None, ca_cert=None):
        self.proxies = proxies
        self.session = create_session(
            proxies=proxies,
            ca_cert=ca_cert,
            is_tls=False,
            has_retry=True,
            delay=2,
        )

    @abstractmethod
    def fetch_jobs(self, career_url: str, company_name: str) -> list[JobPost]:
        """Fetch all jobs from this company's career page."""

    @staticmethod
    def _extract_slug(career_url: str) -> str:
        """Extract company slug from career URL path."""
        parsed = urlparse(career_url)
        path = parsed.path.strip("/")
        # Return last non-empty segment
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else parsed.netloc
