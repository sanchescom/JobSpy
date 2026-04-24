from __future__ import annotations

from urllib.parse import urlparse

from jobspy.careers.base import BaseATSParser


def _lazy_import(parser_class_name: str, module_name: str):
    """Lazy import to avoid loading all parsers at module level."""
    def factory():
        import importlib
        mod = importlib.import_module(f"jobspy.careers.{module_name}")
        return getattr(mod, parser_class_name)
    return factory


_PARSER_FACTORIES = {
    "apply.workable.com": _lazy_import("WorkableParser", "workable"),
    "boards-api.greenhouse.io": _lazy_import("GreenhouseParser", "greenhouse"),
    "job-boards.greenhouse.io": _lazy_import("GreenhouseParser", "greenhouse"),
    "boards.greenhouse.io": _lazy_import("GreenhouseParser", "greenhouse"),
    "jobs.lever.co": _lazy_import("LeverParser", "lever"),
    "jobs.ashbyhq.com": _lazy_import("AshbyParser", "ashby"),
    "careers.smartrecruiters.com": _lazy_import("SmartRecruitersParser", "smartrecruiters"),
    ".bamboohr.com": _lazy_import("BambooHRParser", "bamboohr"),
    ".applytojob.com": _lazy_import("JazzHRParser", "jazzhr"),
}

# Domain → platform name for detection without instantiating
_PLATFORM_MAP = {
    "apply.workable.com": "workable",
    "boards-api.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
    "careers.smartrecruiters.com": "smartrecruiters",
    ".bamboohr.com": "bamboohr",
    ".applytojob.com": "jazzhr",
}


def _match_domain(hostname: str) -> str | None:
    """Match hostname against registry keys (exact or suffix match)."""
    for key in _PARSER_FACTORIES:
        if key.startswith("."):
            if hostname.endswith(key):
                return key
        elif hostname == key:
            return key
    return None


def get_parser(career_url: str, **kwargs) -> BaseATSParser | None:
    """Return an ATS parser instance for the given career URL, or None."""
    hostname = urlparse(career_url).hostname or ""
    key = _match_domain(hostname)
    if key is None:
        return None
    cls = _PARSER_FACTORIES[key]()
    return cls(**kwargs)


def detect_platform(career_url: str) -> str:
    """Detect the ATS platform from a career URL. Returns 'custom' if unknown."""
    hostname = urlparse(career_url).hostname or ""
    key = _match_domain(hostname)
    if key is not None:
        return _PLATFORM_MAP[key]
    return "custom"
