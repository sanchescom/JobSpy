"""A page with no jobs must say why (each strategy's note), not just return []."""
from jobspy.careers.generic import GenericCareerParser


def test_no_jobs_reports_reason_per_strategy():
    class R:  # fake 403 response
        ok=False; status_code=403; text=""
    p=GenericCareerParser()
    p.session.get=lambda *a,**k: R()
    p._fetch_sitemap_jobs=lambda *a,**k: []
    p._get_sitemaps_from_robots=lambda *a,**k: []
    p._try_browser=lambda u,c: (p._note("browser: rendered 120 KB, 3 JSON responses, no jobs, no jobs link — SPA or empty board?"), [])[1]
    assert p.fetch_jobs("https://example.com/careers","X")==[]
    assert p.last_reason.startswith("http 403; sitemap: no job URLs; browser: rendered")
