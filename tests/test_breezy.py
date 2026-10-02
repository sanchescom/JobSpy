"""Breezy: list from /json, description from the posting page's JSON-LD."""
from jobspy.careers import get_parser
from jobspy.careers.breezy import BreezyParser, parse_jsonld_description


def test_breezy_board_is_detected_and_parsed():
    assert isinstance(get_parser("https://hendriks-greenhouses.breezy.hr"), BreezyParser)
    post = BreezyParser._parse_list_job({
        "id": "ba3", "name": "AZ Driver", "url": "https://x.breezy.hr/p/ba3-az-driver",
        "published_date": "2026-09-17T17:16:09.736Z",
        "location": {"city": "Lincoln", "state": {"name": "Ontario"}, "country": {"id": "CA"}},
    }, "Hendriks")
    assert post.id == "breezy:ba3" and post.location.city == "Lincoln" and post.date_posted.day == 17
    page = '<script type="application/ld+json">{"@type":"JobPosting","description":"<p>Drive &amp; deliver</p>"}</script>'
    assert "Drive & deliver" in parse_jsonld_description(page)
    assert parse_jsonld_description("<html></html>") is None
