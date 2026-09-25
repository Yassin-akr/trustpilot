from urllib.parse import parse_qs, urlparse

import pytest

from trustpilot_scraper import exporters
from trustpilot_scraper.amazon import (
    AMAZON_FIELDS,
    AmazonScraper,
    CaptchaRequired,
    LoginRequired,
    extract_product,
    extract_reviews,
    page_kind,
    parse_country,
    parse_date,
    parse_product,
)
from trustpilot_scraper.browser import BrowserResponse
from trustpilot_scraper.scraper import ScraperError, summarize

STARS_TXT = {1: "1,0 sur 5 étoiles", 2: "2,0 sur 5 étoiles", 3: "3,0 sur 5 étoiles",
             4: "4,0 sur 5 étoiles", 5: "5,0 sur 5 étoiles"}


def review_html(rid, rating, day=3, helpful="", verified=True, variant="Taille: M | Couleur: Noir"):
    return f"""
<div id="{rid}" data-hook="review" class="a-section review aok-relative">
  <div class="a-profile-content"><span class="a-profile-name">Client {rid}</span></div>
  <a data-hook="review-title" class="a-link-normal review-title" href="/gp/customer-reviews/{rid}">
    <i data-hook="review-star-rating" class="a-icon a-icon-star"><span class="a-icon-alt">{STARS_TXT[rating]}</span></i>
    <span class="a-letter-space"></span>
    <span>Titre de l'avis {rid}</span>
  </a>
  <span data-hook="review-date" class="review-date">Commenté en France le {day} mars 2024</span>
  <a data-hook="format-strip" class="a-link-normal">{variant}</a>
  {'<span data-hook="avp-badge" class="a-size-mini">Achat vérifié</span>' if verified else ''}
  <span data-hook="review-body" class="review-text"><span>Très bon produit.<br>Je recommande {rid}.</span>
    <script>var x = 1;</script></span>
  {f'<span data-hook="helpful-vote-statement">{helpful}</span>' if helpful else ''}
</div>"""


def reviews_page(reviews_html, total="1 234 évaluations globales"):
    return f"""<html><body>
<a data-hook="product-link" href="/dp/B0C1234567">Chaussures de running Stryde X</a>
<span data-hook="rating-out-of-text">4,3 sur 5</span>
<div data-hook="total-review-count"><span>{total}</span></div>
<div id="cm_cr-review_list">{''.join(reviews_html)}</div>
</body></html>"""


LOGIN_PAGE = '<html><form name="signIn" method="post"><input id="ap_email"></form></html>'
CAPTCHA_PAGE = '<html><form method="get" action="/errors/validateCaptcha"></form></html>'


# ---------- parsing ----------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("B0C1234567", ("B0C1234567", "amazon.fr")),
        ("b0c1234567", ("B0C1234567", "amazon.fr")),
        ("https://www.amazon.fr/Chaussures-Stryde/dp/B0C1234567/ref=sr_1_1?th=1", ("B0C1234567", "amazon.fr")),
        ("https://www.amazon.com/product-reviews/B0C1234567/", ("B0C1234567", "amazon.com")),
        ("amazon.co.uk/gp/product/B0C1234567", ("B0C1234567", "amazon.co.uk")),
    ],
)
def test_parse_product(value, expected):
    assert parse_product(value) == expected


@pytest.mark.parametrize("value", ["", "getstryde.co", "https://www.amazon.fr/s?k=chaussures"])
def test_parse_product_invalid(value):
    with pytest.raises(ScraperError):
        parse_product(value)


@pytest.mark.parametrize(
    "text,date,country",
    [
        ("Commenté en France le 3 mars 2024", "2024-03-03", "France"),
        ("Commenté en Belgique le 1 février 2023", "2023-02-01", "Belgique"),
        ("Reviewed in the United States on March 3, 2024", "2024-03-03", "United States"),
        ("Rezension aus Deutschland vom 3. März 2024", "2024-03-03", "Deutschland"),
        ("Revisado en España el 12 de agosto de 2022", "2022-08-12", "España"),
        ("Recensito in Italia il 25 dicembre 2021", "2021-12-25", "Italia"),
        ("texte inconnu", "", ""),
    ],
)
def test_parse_date_and_country(text, date, country):
    assert parse_date(text) == date
    assert parse_country(text) == country


def test_extract_reviews_fields():
    html = reviews_page([review_html("R1ABC", 4, helpful="12 personnes ont trouvé cela utile"),
                         review_html("R2DEF", 1, helpful="Une personne a trouvé cela utile", verified=False)])
    r1, r2 = extract_reviews(html, "amazon.fr")
    assert r1 == {
        "id": "R1ABC",
        "date": "2024-03-03",
        "rating": 4,
        "title": "Titre de l'avis R1ABC",
        "text": "Très bon produit. Je recommande R1ABC.",
        "name": "Client R1ABC",
        "country": "France",
        "verified": True,
        "likes": 12,
        "variant": "Taille: M | Couleur: Noir",
        "date_text": "Commenté en France le 3 mars 2024",
        "url": "https://www.amazon.fr/gp/customer-reviews/R1ABC",
    }
    assert r2["rating"] == 1 and r2["likes"] == 1 and r2["verified"] is False


def test_extract_product():
    p = extract_product(reviews_page([]), "B0C1234567", "amazon.fr")
    assert p["name"] == "Chaussures de running Stryde X"
    assert p["trust_score"] == 4.3
    assert p["total_reviews"] == 1234


def test_page_kind():
    assert page_kind(LOGIN_PAGE) == "login"
    assert page_kind("<html></html>", "https://www.amazon.fr/ap/signin?x") == "login"
    assert page_kind(CAPTCHA_PAGE) == "captcha"
    assert page_kind(reviews_page([])) == "reviews"


# ---------- extraction complète ----------

class FakeAmazon:
    """Simule Amazon : 10 avis par page, 10 pages max par recherche,
    filtre par note (filterByStar) et tri (sortBy)."""

    FILTERS = {"one_star": 1, "two_star": 2, "three_star": 3, "four_star": 4, "five_star": 5}

    def __init__(self, ratings, first=None):
        self.reviews = [(f"R{i:04d}", r, i % 28 + 1) for i, r in enumerate(ratings)]
        self.calls = []
        self.first = list(first or [])  # réponses forcées (login, captcha...)

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self.first:
            return BrowserResponse(200, self.first.pop(0), url)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        star = self.FILTERS.get(q.get("filterByStar"))
        items = [r for r in self.reviews if not star or r[1] == star]
        if q.get("sortBy") == "helpful":
            items = items[::-1]
        page = int(q.get("pageNumber", 1))
        chunk = items[(page - 1) * 10 : page * 10] if page <= 10 else []
        return BrowserResponse(200, reviews_page([review_html(i, r, d) for i, r, d in chunk]), url)


def scraper(session, **kw):
    kw.setdefault("delay", 0)
    return AmazonScraper("B0C1234567", session=session, **kw)


def test_small_product_single_pass():
    session = FakeAmazon([5] * 35 + [2] * 10)
    s = scraper(session)
    reviews = s.run()
    assert len(reviews) == 45
    assert len(session.calls) == 5  # 45 avis = 5 pages, la dernière incomplète
    assert s.business["name"] == "Chaussures de running Stryde X"
    assert s.warnings == []


def test_ten_page_limit_split_by_star_and_sort():
    ratings = [5] * 180 + [4] * 60 + [3] * 15 + [2] * 5 + [1] * 40  # 300 avis
    progress = []
    s = scraper(FakeAmazon(ratings), on_progress=progress.append)
    reviews = s.run()
    assert len(reviews) == 300
    assert "note par note" in s.warnings[0]
    assert "5★ utiles" in {p["label"] for p in progress}  # 180 avis 5★ : tri « utiles » en plus
    assert "4★ utiles" not in {p["label"] for p in progress}  # 60 avis 4★ : 6 pages suffisent


def test_more_than_200_for_one_star_warns():
    s = scraper(FakeAmazon([5] * 250))
    assert len(s.run()) == 200
    assert any("Plus de 200 avis 5★" in w for w in s.warnings)


def test_star_filter():
    session = FakeAmazon([5] * 30 + [1] * 12)
    reviews = scraper(session, stars=[1]).run()
    assert {r["rating"] for r in reviews} == {1}
    assert len(reviews) == 12
    assert all("filterByStar=one_star" in u for u in session.calls)


def test_login_required():
    with pytest.raises(LoginRequired, match="amazon-login"):
        scraper(FakeAmazon([5], first=[LOGIN_PAGE])).run()


def test_captcha_without_browser_window():
    with pytest.raises(CaptchaRequired, match="--show-browser"):
        scraper(FakeAmazon([5], first=[CAPTCHA_PAGE])).run()


def test_amazon_exports():
    s = scraper(FakeAmazon([5] * 3 + [1] * 2))
    reviews = s.run()
    csv_text = exporters.to_csv(reviews, fields=AMAZON_FIELDS).decode("utf-8-sig")
    assert csv_text.splitlines()[0] == ",".join(AMAZON_FIELDS)
    assert exporters.to_xlsx(reviews, s.business, AMAZON_FIELDS)[:2] == b"PK"
    assert summarize(reviews)["distribution"] == {"1": 2, "2": 0, "3": 0, "4": 0, "5": 3}
    assert s.slug == "amazon_B0C1234567"


def test_web_amazon_job(monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from trustpilot_scraper.web import app as webapp

    orig = AmazonScraper.__init__

    def fake_init(self, product, **kw):
        kw["session"] = FakeAmazon([5] * 15 + [3] * 5)
        kw["delay"] = 0
        orig(self, product, **kw)

    monkeypatch.setattr(webapp.AmazonScraper, "__init__", fake_init)
    client = TestClient(webapp.app)

    bad = client.post("/api/jobs", json={"source": "amazon", "brand": "pas un produit"})
    assert bad.status_code == 400

    job = client.post(
        "/api/jobs",
        json={"source": "amazon", "brand": "https://www.amazon.fr/dp/B0C1234567", "delay": 1},
    ).json()
    for _ in range(50):
        state = client.get(f"/api/jobs/{job['id']}").json()
        if state["status"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert state["status"] == "done", state
    assert state["summary"]["count"] == 20
    assert state["business"]["score_label"] == "Note Amazon"
    r = client.get(f"/api/jobs/{job['id']}/export/csv")
    assert "amazon_B0C1234567_reviews.csv" in r.headers["content-disposition"]
    assert r.content.decode("utf-8-sig").startswith(",".join(AMAZON_FIELDS))
