import csv
import io
import json
import time

import pytest

from trustpilot_scraper import exporters, scraper as sc
from trustpilot_scraper.scraper import (
    NotFound,
    ScrapeOptions,
    ScraperError,
    TrustpilotScraper,
    normalize_domain,
    summarize,
)


def make_review(i, rating=5, reply=None):
    return {
        "id": f"r{i}",
        "rating": rating,
        "title": f"Titre {i}",
        "text": f"Ligne 1\nLigne 2 avis {i}",
        "language": "fr",
        "likes": 0,
        "dates": {"publishedDate": f"2024-01-{i:02d}T10:00:00.000Z", "experiencedDate": None},
        "consumer": {"displayName": f"Client {i}", "countryCode": "FR", "numberOfReviews": 2},
        "labels": {"verification": {"isVerified": i % 2 == 0}},
        "reply": {"message": reply, "publishedDate": "2024-02-01T00:00:00Z"} if reply else None,
    }


def make_page(reviews, total_pages=3):
    data = {
        "props": {
            "pageProps": {
                "businessUnit": {
                    "displayName": "Stryde",
                    "identifyingName": "getstryde.co",
                    "trustScore": 4.3,
                    "stars": 4.5,
                    "numberOfReviews": 5,
                },
                "reviews": reviews,
                "filters": {"pagination": {"totalPages": total_pages}},
            }
        }
    }
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sc.requests.HTTPError(str(self.status_code))


class FakeSession:
    """Sert des pages pré-définies ; ``pages[n]`` = (status, html)."""

    def __init__(self, pages):
        self.pages = pages
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None):
        from urllib.parse import parse_qs, urlparse

        params = {k: v if k == "stars" else v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.calls.append((url, params))
        page = int(params.get("page", 1))
        resp = self.pages.get(page, (404, ""))
        if isinstance(resp, list):  # réponses successives (pour tester les retries)
            resp = resp.pop(0)
        return FakeResponse(*resp)


PAGES = {
    1: (200, make_page([make_review(1, 5), make_review(2, 1, reply="Désolé")])),
    # r2 en double sur la page 2 : doit être dédupliqué
    2: (200, make_page([make_review(2, 1, reply="Désolé"), make_review(3, 4)])),
    3: (200, make_page([make_review(4, 3), make_review(5, 5)])),
}


def fast_opts(**kw):
    kw.setdefault("engine", "http")
    return ScrapeOptions(delay=0, **kw)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("getstryde.co", ("getstryde.co", "www.trustpilot.com")),
        ("https://www.getstryde.co/", ("getstryde.co", "www.trustpilot.com")),
        ("https://fr.trustpilot.com/review/getstryde.co?page=2", ("getstryde.co", "fr.trustpilot.com")),
        ("trustpilot.com/review/www.amazon.fr", ("www.amazon.fr", "trustpilot.com")),
        ("  GetStryde.CO  ", ("getstryde.co", "www.trustpilot.com")),
    ],
)
def test_normalize_domain(value, expected):
    assert normalize_domain(value) == expected


@pytest.mark.parametrize("value", ["", "pas un domaine", "https://www.trustpilot.com/categories/x"])
def test_normalize_domain_invalid(value):
    with pytest.raises(ScraperError):
        normalize_domain(value)


def test_run_all_pages_dedup_and_sort():
    progress = []
    s = TrustpilotScraper("getstryde.co", fast_opts(), session=FakeSession(PAGES), on_progress=progress.append)
    reviews = s.run()
    assert [r["id"] for r in reviews] == ["r5", "r4", "r3", "r2", "r1"]
    assert s.business["name"] == "Stryde"
    assert [p["page"] for p in progress] == [1, 2, 3]
    r2 = next(r for r in reviews if r["id"] == "r2")
    assert r2["text"] == "Ligne 1 Ligne 2 avis 2"
    assert r2["reply"] == "Désolé"
    assert r2["verified"] is True
    assert r2["url"] == "https://www.trustpilot.com/reviews/r2"


def test_query_params_filters():
    session = FakeSession(PAGES)
    TrustpilotScraper("getstryde.co", fast_opts(stars=[5, 1], languages="fr", max_pages=2), session=session).run()
    assert len(session.calls) == 2
    assert session.calls[0][1] == {"languages": "fr", "stars": ["1", "5"]}
    assert session.calls[1][1]["page"] == "2"


def test_default_request_matches_original_script():
    session = FakeSession(PAGES)
    TrustpilotScraper("getstryde.co", fast_opts(max_pages=2), session=session).run()
    assert [u for u, _ in session.calls] == [
        "https://www.trustpilot.com/review/getstryde.co",
        "https://www.trustpilot.com/review/getstryde.co?page=2",
    ]


def test_403_explained(monkeypatch):
    s = TrustpilotScraper("getstryde.co", fast_opts(), session=FakeSession({1: (403, "")}))
    monkeypatch.setattr(s, "_sleep", lambda _: None)
    with pytest.raises(sc.Blocked, match="mode navigateur"):
        s.run()


def test_auto_switches_to_browser_when_blocked(monkeypatch):
    http = FakeSession({1: (403, "")})
    browser = FakeSession(PAGES)
    browser.closed = False
    browser.close = lambda: setattr(browser, "closed", True)
    s = TrustpilotScraper(
        "getstryde.co",
        fast_opts(engine="auto"),
        session=http,
        browser_factory=lambda: browser,
    )
    monkeypatch.setattr(s, "_sleep", lambda _: None)
    assert len(s.run()) == 5
    assert len(http.calls) == 1  # un seul essai HTTP avant de basculer
    assert len(browser.calls) == 3
    assert s.engine_used == "browser"
    assert browser.closed
    assert "navigateur" in s.warnings[0]


def test_browser_mode_still_blocked(monkeypatch):
    s = TrustpilotScraper(
        "getstryde.co",
        fast_opts(engine="browser", retries=2),
        browser_factory=lambda: FakeSession({1: (403, "")}),
    )
    monkeypatch.setattr(s, "_sleep", lambda _: None)
    with pytest.raises(sc.Blocked, match="4G, VPN"):
        s.run()


def test_brand_not_found():
    with pytest.raises(NotFound):
        TrustpilotScraper("inconnu.com", fast_opts(), session=FakeSession({})).run()


def test_retry_then_success(monkeypatch):
    pages = dict(PAGES)
    pages[2] = [(429, ""), PAGES[2]]
    s = TrustpilotScraper("getstryde.co", fast_opts(), session=FakeSession(pages))
    monkeypatch.setattr(s, "_sleep", lambda _: None)
    assert len(s.run()) == 5
    assert s.warnings == []


def test_failed_page_is_skipped_with_warning(monkeypatch):
    pages = dict(PAGES)
    pages[2] = (500, "")
    s = TrustpilotScraper("getstryde.co", fast_opts(), session=FakeSession(pages))
    monkeypatch.setattr(s, "_sleep", lambda _: None)
    ids = {r["id"] for r in s.run()}
    assert ids == {"r1", "r2", "r4", "r5"}
    assert "Page 2" in s.warnings[0]


def test_summary_and_exports():
    reviews = TrustpilotScraper("getstryde.co", fast_opts(), session=FakeSession(PAGES)).run()
    s = summarize(reviews)
    assert s["count"] == 5
    assert s["distribution"] == {"1": 1, "2": 0, "3": 1, "4": 1, "5": 2}
    assert s["average"] == 3.6
    assert s["replied"] == 1

    rows = list(csv.DictReader(io.StringIO(exporters.to_csv(reviews).decode("utf-8-sig"))))
    assert len(rows) == 5 and rows[0]["id"] == "r5"
    assert json.loads(exporters.to_json(reviews, {"name": "Stryde"}))["count"] == 5
    assert exporters.to_xlsx(reviews, {"name": "Stryde"})[:2] == b"PK"


def test_web_api(monkeypatch):
    from fastapi.testclient import TestClient

    from trustpilot_scraper.web import app as webapp

    orig_init = TrustpilotScraper.__init__

    def fake_init(self, brand, options=None, session=None, **kw):
        options.engine = "http"
        orig_init(self, brand, options, session=FakeSession(PAGES), **kw)

    monkeypatch.setattr(webapp.TrustpilotScraper, "__init__", fake_init)
    client = TestClient(webapp.app)

    assert client.get("/").status_code == 200
    assert client.post("/api/jobs", json={"brand": "pas valide"}).status_code == 400

    job = client.post("/api/jobs", json={"brand": "getstryde.co", "delay": 0.5}).json()
    for _ in range(50):
        state = client.get(f"/api/jobs/{job['id']}").json()
        if state["status"] not in ("queued", "running"):
            break
        time.sleep(0.2)
    assert state["status"] == "done", state
    assert state["summary"]["count"] == 5

    full = client.get(f"/api/jobs/{job['id']}?reviews=true").json()
    assert len(full["reviews"]) == 5
    r = client.get(f"/api/jobs/{job['id']}/export/csv")
    assert r.status_code == 200
    assert "getstryde_co_reviews.csv" in r.headers["content-disposition"]
    assert client.get(f"/api/jobs/{job['id']}/export/pdf").status_code == 400
