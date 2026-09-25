"""
Extraction des avis d'un produit Amazon.

Amazon réserve la liste complète des avis aux utilisateurs connectés : on passe
par un vrai navigateur avec un profil persistant, dans lequel l'utilisateur se
connecte une fois (``python -m trustpilot_scraper amazon-login``).

Amazon limite chaque recherche à 10 pages de 10 avis. Au-delà, on parcourt les
avis note par note, puis par ordre « utiles » en plus de « récents ».
"""

from __future__ import annotations

import re
import threading
from typing import Callable
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from .scraper import Cancelled, ScraperError

AMAZON_FIELDS = [
    "id",
    "date",
    "rating",
    "title",
    "text",
    "name",
    "country",
    "verified",
    "likes",
    "variant",
    "date_text",
    "url",
]

AMAZON_DOMAINS = [
    "amazon.fr",
    "amazon.com",
    "amazon.co.uk",
    "amazon.de",
    "amazon.es",
    "amazon.it",
    "amazon.nl",
    "amazon.be",
    "amazon.ca",
]

STAR_FILTERS = {1: "one_star", 2: "two_star", 3: "three_star", 4: "four_star", 5: "five_star"}
MAX_PAGES = 10  # limite imposée par Amazon pour une recherche
PAGE_SIZE = 10

ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
ASIN_IN_URL_RE = re.compile(r"/(?:dp|gp/product|product-reviews|gp/aw/d|ASIN)/([A-Z0-9]{10})", re.I)

MONTHS = {
    # fr
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
    # en
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    # de
    "januar": 1, "februar": 2, "märz": 3, "juni": 6, "juli": 7, "oktober": 10, "dezember": 12,
    # es
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # it
    "gennaio": 1, "febbraio": 2, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7,
    "settembre": 9, "ottobre": 10, "dicembre": 12,
    # nl
    "januari": 1, "februari": 2, "maart": 3, "mei": 5, "augustus": 8,
}

COUNTRY_RE = re.compile(
    r"(?:Commenté (?:en|au|aux)|Reviewed in|Rezension aus|Revisado en|Recensito in|"
    r"Beoordeeld in)\s+(?:the\s+|l[ae']\s*|el\s+|den\s+)?(.+?)\s+(?:le|on|vom|el|il|op)\s",
    re.I,
)


# Cookie posé par Amazon une fois connecté : at-main (.com), at-acbuk (.co.uk), at-acbfr (.fr)...
AUTH_COOKIE_RE = re.compile(r"^at-(main|acb[a-z]+)$")


def is_logged_in(url: str, cookies: list[dict], domain: str) -> bool:
    """Connecté = cookie d'authentification présent pour ce site, et plus sur une page /ap/
    (connexion, double authentification...)."""
    if "/ap/" in url:
        return False
    return any(
        AUTH_COOKIE_RE.match(c.get("name", "")) and c.get("domain", "").lstrip(".").endswith(domain)
        for c in cookies
    )


class LoginRequired(ScraperError):
    pass


class CaptchaRequired(ScraperError):
    pass


def parse_product(value: str, default_domain: str = "amazon.fr") -> tuple[str, str]:
    """
    Accepte un lien produit Amazon ou un ASIN. Retourne ``(asin, domaine_amazon)``.
    """
    value = (value or "").strip()
    if ASIN_RE.match(value.upper()):
        return value.upper(), default_domain
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower()
    m = ASIN_IN_URL_RE.search(parsed.path)
    if "amazon." not in host or not m:
        raise ScraperError(
            "Produit Amazon invalide : colle le lien de la fiche produit ou son ASIN (ex. B0C1234567)."
        )
    domain = host.split("amazon.", 1)[1]
    return m.group(1).upper(), f"amazon.{domain}"


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _number(text: str) -> float | None:
    m = re.search(r"\d+(?:[.,]\d+)?", text or "")
    return float(m.group(0).replace(",", ".")) if m else None


def _int(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def parse_date(text: str) -> str:
    """« Commenté en France le 3 mars 2024 » -> « 2024-03-03 » (vide si inconnu)."""
    t = (text or "").lower().replace(".", " ").replace(",", " ")
    year = re.search(r"\b(19|20)\d{2}\b", t)
    month = next((MONTHS[w] for w in re.findall(r"[a-zà-ÿ]+", t) if w in MONTHS), None)
    if not (year and month):
        return ""
    rest = t.replace(year.group(0), " ")
    day = re.search(r"\b([0-3]?\d)\b", rest)
    if not day or not 1 <= int(day.group(1)) <= 31:
        return ""
    return f"{year.group(0)}-{month:02d}-{int(day.group(1)):02d}"


def parse_country(text: str) -> str:
    m = COUNTRY_RE.search(text or "")
    return m.group(1).strip() if m else ""


def page_kind(html: str, url: str = "") -> str:
    """'login', 'captcha' ou 'reviews'."""
    if "/ap/signin" in url or 'name="signIn"' in html or 'id="ap_email"' in html:
        return "login"
    if "validateCaptcha" in html or "/errors/validateCaptcha" in url:
        return "captcha"
    return "reviews"


def extract_reviews(html: str, domain: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for rev in soup.select('[data-hook="review"]'):
        rid = rev.get("id", "")
        rating_el = rev.select_one(
            '[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"]'
        )
        rating_text = _text(rating_el)
        rating = _number(rating_text)

        title_el = rev.select_one('[data-hook="review-title"]')
        title = _text(title_el)
        if title_el and title_el.select("span"):
            # Nouvelle mise en page : la note est dans le titre, le vrai titre est le dernier span
            spans = [s for s in title_el.select("span") if _text(s) and "a-icon-alt" not in (s.get("class") or [])]
            if spans:
                title = _text(spans[-1])

        body = rev.select_one('[data-hook="review-body"]')
        if body:
            for junk in body.select("script, style, .video-block, [data-hook='review-body-read-more']"):
                junk.decompose()
        date_text = _text(rev.select_one('[data-hook="review-date"]'))
        helpful = _text(rev.select_one('[data-hook="helpful-vote-statement"]'))

        out.append(
            {
                "id": rid,
                "date": parse_date(date_text),
                "rating": int(rating) if rating else "",
                "title": title,
                "text": _text(body),
                "name": _text(rev.select_one(".a-profile-name")),
                "country": parse_country(date_text),
                "verified": bool(rev.select_one('[data-hook="avp-badge"], [data-hook="avp-badge-linkless"]')),
                "likes": (_int(helpful) or 1) if helpful else 0,
                "variant": _text(rev.select_one('[data-hook="format-strip"], [data-hook="format-strip-linkless"]')),
                "date_text": date_text,
                "url": f"https://www.{domain}/gp/customer-reviews/{rid}" if rid else "",
            }
        )
    return out


def extract_product(html: str, asin: str, domain: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    avg = _text(soup.select_one('[data-hook="rating-out-of-text"], [data-hook="average-star-rating"]'))
    total = _text(soup.select_one('[data-hook="total-review-count"]'))
    return {
        "name": _text(soup.select_one('[data-hook="product-link"]')) or asin,
        "domain": domain,
        "asin": asin,
        "trust_score": _number(avg),
        "score_label": "Note Amazon",
        "total_reviews": _int(total),
        "website": f"https://www.{domain}/dp/{asin}",
    }


class AmazonScraper:
    FIELDS = AMAZON_FIELDS

    def __init__(
        self,
        product: str,
        domain: str = "amazon.fr",
        stars: list[int] | None = None,
        max_pages: int | None = None,
        delay: float = 2.0,
        timeout: int = 30,
        show_browser: bool = False,
        on_progress: Callable[[dict], None] | None = None,
        cancel_event: threading.Event | None = None,
        session=None,
    ):
        self.asin, self.domain = parse_product(product, domain)
        self.stars = sorted({s for s in (stars or []) if 1 <= s <= 5}, reverse=True)
        self.max_pages = min(max_pages or MAX_PAGES, MAX_PAGES)
        self.delay = delay
        self.timeout = timeout
        self.show_browser = show_browser
        self.on_progress = on_progress or (lambda _: None)
        self.cancel_event = cancel_event or threading.Event()
        self._session = session
        self.session = None
        self.engine_used = "browser"
        self.business: dict = {}
        self.warnings: list[str] = []
        self.collected: dict[str, dict] = {}

    # --- interface commune avec TrustpilotScraper ---
    @property
    def base_url(self) -> str:
        return f"https://www.{self.domain}/product-reviews/{self.asin}/"

    @property
    def slug(self) -> str:
        return f"amazon_{self.asin}"

    def results(self) -> list[dict]:
        return sorted(self.collected.values(), key=lambda r: r["date"], reverse=True)

    def run(self) -> list[dict]:
        try:
            self.session = self._session or self._open_browser()
            self._run()
            return self.results()
        finally:
            if self.session is not None and self.session is not self._session:
                self.session.close()
            self.session = None

    # --- interne ---
    def _open_browser(self):
        from .browser import BrowserSession, playwright_available, profile_dir

        if not playwright_available():
            raise ScraperError("Amazon nécessite Playwright : pip install playwright")
        try:
            return BrowserSession(
                ready_selector='[data-hook="review"], #cm_cr-review_list, form[name="signIn"], '
                'form[action*="validateCaptcha"]',
                require_ready=False,
                user_data_dir=profile_dir("amazon"),
                # Fenêtre hors écran plutôt que headless : Amazon repère facilement le headless.
                offscreen=not self.show_browser,
                locale="fr-FR",
            )
        except RuntimeError as e:
            raise ScraperError(str(e))
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "already in use" in msg or "processsingleton" in msg or "singletonlock" in msg:
                raise ScraperError(
                    "Le profil Amazon est déjà utilisé par une autre fenêtre (connexion ou extraction "
                    "en cours). Ferme-la puis réessaie."
                )
            raise

    def page_url(self, page: int, star: int | None = None, sort: str = "recent") -> str:
        q = {
            "reviewerType": "all_reviews",
            "sortBy": sort,
            "pageNumber": page,
            "filterByStar": STAR_FILTERS[star] if star else "all_stars",
        }
        return f"{self.base_url}?{urlencode(q)}"

    def _sleep(self, seconds: float):
        if self.cancel_event.wait(seconds):
            raise Cancelled()

    def fetch(self, url: str) -> str:
        last: Exception | None = None
        for attempt in range(1, 4):
            if self.cancel_event.is_set():
                raise Cancelled()
            try:
                r = self.session.get(url, timeout=self.timeout)
            except OSError as e:
                last = e
                self._sleep(2**attempt * 2)
                continue
            if r.status_code == 404:
                raise ScraperError(f"Produit introuvable sur {self.domain} : {self.asin}")
            kind = page_kind(r.text, getattr(r, "url", ""))
            if kind == "login":
                if self.show_browser and hasattr(self.session, "wait_until"):
                    self.warnings.append("Connexion Amazon demandée : connecte-toi dans la fenêtre.")
                    if self.session.wait_until(
                        lambda p: is_logged_in(p.url, p.context.cookies(), self.domain), timeout=600
                    ):
                        continue
                raise LoginRequired(
                    f"Connexion à Amazon requise. Lance d'abord : python -m trustpilot_scraper "
                    f"amazon-login --domain {self.domain} (ou le bouton « Se connecter à Amazon » "
                    "de l'interface). Tu peux aussi relancer avec --show-browser et te connecter "
                    "directement dans la fenêtre."
                )
            if kind == "captcha":
                if self.show_browser and hasattr(self.session, "wait_until"):
                    self.warnings.append("Captcha Amazon affiché : en attente de sa résolution.")
                    if self.session.wait_until(
                        lambda p: page_kind(p.content(), p.url) != "captcha", timeout=180
                    ):
                        continue
                raise CaptchaRequired(
                    "Amazon demande un captcha. Relance avec « Afficher le navigateur » "
                    "(--show-browser) pour le résoudre, ou attends un peu."
                )
            if r.status_code >= 500 or r.status_code == 429:
                last = ScraperError(f"HTTP {r.status_code}")
                self._sleep(2**attempt * 2)
                continue
            return r.text
        raise ScraperError(f"Échec du chargement ({last})")

    def _run(self):
        if self.stars:
            for star in self.stars:
                self._pass_with_sorts(star)
            return

        capped = self._pass(None, "recent", label="")
        if capped and self.max_pages == MAX_PAGES:
            self.warnings.append(
                "Amazon limite l'affichage à 10 pages : récupération note par note "
                "pour obtenir tous les avis."
            )
            for star in range(5, 0, -1):
                self._pass_with_sorts(star)

    def _pass_with_sorts(self, star: int):
        capped = self._pass(star, "recent", label=f"{star}★")
        if capped and self.max_pages == MAX_PAGES:
            # 10 pages « récents » + 10 pages « utiles » : jusqu'à 200 avis par note
            if self._pass(star, "helpful", label=f"{star}★ utiles"):
                self.warnings.append(
                    f"Plus de 200 avis {star}★ : Amazon n'en affiche pas davantage, "
                    "certains peuvent manquer."
                )

    def _pass(self, star: int | None, sort: str, label: str) -> bool:
        """Parcourt une recherche. Retourne True si la limite de 10 pages est atteinte."""
        seen: set[str] = set()
        for page in range(1, self.max_pages + 1):
            if page > 1 or self.collected:
                self._sleep(self.delay)
            html = self.fetch(self.page_url(page, star, sort))
            if not self.business:
                self.business = extract_product(html, self.asin, self.domain)
            items = extract_reviews(html, self.domain)
            ids = {r["id"] for r in items}
            if not ids or ids <= seen:
                return False  # plus d'avis dans cette recherche
            seen |= ids
            for r in items:
                self.collected[r["id"] or f"_{len(self.collected)}"] = r
            self.on_progress(
                {
                    "page": page,
                    "pages": self.max_pages,
                    "label": label,
                    "count": len(self.collected),
                    "engine": "browser",
                    "business": self.business,
                }
            )
            if len(items) < PAGE_SIZE:
                return False  # dernière page
        return True


def amazon_login(domain: str = "amazon.fr", timeout: float = 600, on_status=None) -> bool:
    """
    Ouvre une fenêtre de navigateur sur la page de connexion Amazon et attend que
    l'utilisateur se connecte. La session est gardée dans le profil persistant.
    """
    from .browser import BrowserSession, playwright_available, profile_dir

    if not playwright_available():
        raise ScraperError("Amazon nécessite Playwright : pip install playwright")
    status = on_status or (lambda _: None)
    try:
        session = BrowserSession(
            ready_selector="body",
            require_ready=False,
            user_data_dir=profile_dir("amazon"),
            headless=False,
            locale="fr-FR",
        )
    except RuntimeError as e:
        raise ScraperError(str(e))
    try:
        # L'historique des commandes exige d'être connecté : sinon, Amazon redirige
        # vers sa page de connexion (la page « Votre compte », elle, s'affiche sans).
        session.get(f"https://www.{domain}/gp/css/order-history", timeout=60)

        def logged_in(page) -> bool:
            return is_logged_in(page.url, page.context.cookies(), domain)

        if logged_in(session.page):
            status("Déjà connecté à Amazon.")
            return True
        status("Connecte-toi à Amazon dans la fenêtre ouverte…")
        return session.wait_until(logged_in, timeout=timeout)
    finally:
        session.close()
