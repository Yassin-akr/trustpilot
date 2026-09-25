"""
Moteur d'extraction des avis Trustpilot.

Trustpilot embarque les avis de chaque page dans un blob JSON ``__NEXT_DATA__``.
On le lit page par page, en dédupliquant par identifiant d'avis.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urlencode, urlparse

import requests

try:  # curl_cffi reproduit l'empreinte TLS d'un vrai Chrome : beaucoup moins de 403
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - dépend de l'installation
    curl_requests = None

HEADERS = {
    # Un vrai User-Agent est indispensable, sinon Trustpilot bloque.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

NETWORK_ERRORS: tuple[type[Exception], ...] = (requests.RequestException, OSError)
if curl_requests is not None:
    NETWORK_ERRORS += (curl_requests.RequestsError,)


def make_session():
    """Session HTTP : curl_cffi (imite Chrome) si disponible, sinon requests."""
    if curl_requests is not None:
        # Pas de User-Agent forcé : curl_cffi envoie celui qui correspond à son empreinte.
        return curl_requests.Session(
            impersonate="chrome", headers={"Accept-Language": HEADERS["Accept-Language"]}
        )
    session = requests.Session()
    session.headers.update(HEADERS)
    return session

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

FIELDS = [
    "id",
    "date",
    "experience_date",
    "rating",
    "title",
    "text",
    "name",
    "country",
    "reviews_count",
    "verified",
    "language",
    "likes",
    "reply",
    "reply_date",
    "url",
]


class ScraperError(Exception):
    pass


class NotFound(ScraperError):
    pass


class Blocked(ScraperError):
    """Trustpilot refuse la requête (HTTP 403, protection anti-robots)."""


class Cancelled(Exception):
    pass


def normalize_domain(value: str) -> tuple[str, str]:
    """
    Accepte un domaine (``getstryde.co``) ou une URL Trustpilot
    (``https://fr.trustpilot.com/review/getstryde.co?page=2``).
    Retourne ``(domaine, hôte_trustpilot)``.
    """
    value = (value or "").strip()
    if not value:
        raise ScraperError("Aucune marque indiquée.")

    host = "www.trustpilot.com"
    if "trustpilot." in value:
        parsed = urlparse(value if "://" in value else f"https://{value}")
        host = parsed.netloc or host
        m = re.search(r"/review/([^/?#]+)", parsed.path)
        if not m:
            raise ScraperError("URL Trustpilot invalide : il faut une URL de type /review/<domaine>.")
        domain = m.group(1)
    else:
        parsed = urlparse(value if "://" in value else f"https://{value}")
        domain = parsed.netloc or parsed.path.split("/")[0]

    domain = domain.lower().strip().strip("/")
    if domain.startswith("www.") and "trustpilot." not in value:
        domain = domain[4:]
    if not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", domain):
        raise ScraperError(f"Domaine invalide : {domain!r}")
    return domain, host


def parse_next_data(html: str) -> dict:
    m = NEXT_DATA_RE.search(html)
    if not m:
        raise ScraperError("Bloc __NEXT_DATA__ introuvable (page bloquée ou format modifié).")
    return json.loads(m.group(1))


def _get(d: dict | None, *keys, default=""):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def extract_reviews(data: dict, host: str = "www.trustpilot.com") -> list[dict]:
    """Transforme le JSON d'une page en liste d'avis à plat."""
    props = _get(data, "props", "pageProps", default={})
    out = []
    for rev in props.get("reviews", []) or []:
        consumer = rev.get("consumer") or {}
        dates = rev.get("dates") or {}
        reply = rev.get("reply") or {}
        rid = rev.get("id", "")
        out.append(
            {
                "id": rid,
                "date": dates.get("publishedDate", "") or "",
                "experience_date": dates.get("experiencedDate", "") or "",
                "rating": rev.get("rating", ""),
                "title": (rev.get("title") or "").strip(),
                "text": (rev.get("text") or "").replace("\r", " ").replace("\n", " ").strip(),
                "name": consumer.get("displayName", "") or "",
                "country": consumer.get("countryCode", "") or "",
                "reviews_count": consumer.get("numberOfReviews", ""),
                "verified": bool(_get(rev, "labels", "verification", "isVerified", default=False)),
                "language": rev.get("language", "") or "",
                "likes": rev.get("likes", 0) or 0,
                "reply": (reply.get("message") or "").replace("\n", " ").strip(),
                "reply_date": reply.get("publishedDate", "") or "",
                "url": f"https://{host}/reviews/{rid}" if rid else "",
            }
        )
    return out


def extract_business(data: dict) -> dict:
    """Infos générales sur l'entreprise (nom, TrustScore, nombre d'avis...)."""
    props = _get(data, "props", "pageProps", default={})
    bu = props.get("businessUnit") or {}
    return {
        "name": bu.get("displayName", ""),
        "domain": bu.get("identifyingName", ""),
        "trust_score": bu.get("trustScore"),
        "stars": bu.get("stars"),
        "total_reviews": bu.get("numberOfReviews"),
        "website": bu.get("websiteUrl", ""),
        "logo": _get(bu, "profileImageUrl", default=""),
    }


def total_pages(data: dict) -> int:
    props = _get(data, "props", "pageProps", default={})
    fp = _get(props, "filters", "pagination", default={})
    return int((fp or {}).get("totalPages") or props.get("totalPages") or 1)


@dataclass
class ScrapeOptions:
    stars: list[int] = field(default_factory=list)  # vide = toutes les notes
    languages: str = ""  # "" = comportement par défaut du site, "all" ou code langue ("fr"...)
    max_pages: int | None = None  # None = toutes
    delay: float = 1.5  # secondes entre deux pages (politesse)
    retries: int = 3
    timeout: int = 30
    engine: str = "auto"  # "auto" (HTTP, puis navigateur si bloqué), "http" ou "browser"
    show_browser: bool = False  # afficher la fenêtre du navigateur (mode navigateur)

    def query(self, page: int, stars: list[int] | None = None) -> dict:
        stars = self.stars if stars is None else stars
        q: dict = {}
        if page > 1:
            q["page"] = page
        if self.languages:
            q["languages"] = self.languages
        if stars:
            q["stars"] = [str(s) for s in sorted(set(stars))]
        return q


ProgressCallback = Callable[[dict], None]


class TrustpilotScraper:
    def __init__(
        self,
        brand: str,
        options: ScrapeOptions | None = None,
        session=None,
        on_progress: ProgressCallback | None = None,
        cancel_event: threading.Event | None = None,
        browser_factory: Callable[[], object] | None = None,
    ):
        self.domain, self.host = normalize_domain(brand)
        self.options = options or ScrapeOptions()
        self._session = session
        self._browser_factory = browser_factory or self._default_browser
        self.engine_used: str | None = None
        self.on_progress = on_progress or (lambda _: None)
        self.cancel_event = cancel_event or threading.Event()
        self.business: dict = {}
        self.warnings: list[str] = []
        self.collected: dict[str, dict] = {}  # accessible même si on annule en cours

    def _default_browser(self):
        from .browser import BrowserSession

        return BrowserSession(headless=not self.options.show_browser)

    def _can_use_browser(self) -> bool:
        from .browser import playwright_available

        return self._browser_factory != self._default_browser or playwright_available()

    def _open(self, engine: str):
        self.close()
        if engine == "browser":
            if not self._can_use_browser():
                raise ScraperError(
                    "Le mode navigateur nécessite Playwright : pip install playwright"
                )
            try:
                self.session = self._browser_factory()
            except RuntimeError as e:
                raise ScraperError(str(e))
        else:
            self.session = self._session or make_session()
        self.engine_used = engine

    def close(self):
        s = getattr(self, "session", None)
        if s is not None and s is not self._session and hasattr(s, "close"):
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        self.session = None

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/review/{self.domain}"

    def page_url(self, page: int, stars: list[int] | None = None) -> str:
        q = urlencode(self.options.query(page, stars), doseq=True)
        return f"{self.base_url}?{q}" if q else self.base_url

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def _sleep(self, seconds: float):
        # Attente interruptible par l'annulation
        if self.cancel_event.wait(seconds):
            raise Cancelled()

    def fetch_page(
        self, page: int, retries: int | None = None, stars: list[int] | None = None
    ) -> dict | None:
        """Retourne le JSON de la page, ou None si la page n'existe pas (404)."""
        retries = retries or self.options.retries
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            self._check_cancel()
            try:
                r = self.session.get(self.page_url(page, stars), timeout=self.options.timeout)
                if r.status_code == 404:
                    if page == 1:
                        raise NotFound(f"Marque introuvable sur Trustpilot : {self.domain}")
                    return None
                if r.status_code == 403:
                    raise Blocked("HTTP 403")
                if r.status_code == 429 or r.status_code >= 500:
                    raise ScraperError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return parse_next_data(r.text)
            except NotFound:
                raise
            except (*NETWORK_ERRORS, ScraperError, json.JSONDecodeError) as e:
                last_err = e
                if attempt < retries:
                    self._sleep(min(2**attempt * 2, 30))  # 4s, 8s, 16s...
        msg = f"Page {page} : échec après {retries} essai(s) ({last_err})"
        if isinstance(last_err, Blocked):
            msg += ". Trustpilot bloque la requête (protection anti-robots)."
            if self.engine_used == "http":
                msg += " Essaie le mode navigateur (--engine browser)."
            msg += " Sinon, attends quelques minutes ou change de connexion (4G, VPN)."
            raise Blocked(msg)
        raise ScraperError(msg)

    def run(self) -> list[dict]:
        """Récupère tous les avis, triés du plus récent au plus ancien."""
        try:
            return self._run()
        finally:
            self.close()

    def _fetch_first(self) -> dict:
        engine = self.options.engine
        self._open("browser" if engine == "browser" else "http")
        if engine != "auto" or not self._can_use_browser():
            return self.fetch_page(1)
        try:
            # En auto, inutile d'insister en HTTP : un seul essai avant le navigateur
            return self.fetch_page(1, retries=1)
        except Blocked:
            self.warnings.append("Requêtes HTTP bloquées : passage en mode navigateur.")
            self._open("browser")
            return self.fetch_page(1)

    def _run(self) -> list[dict]:
        first = self._fetch_first()
        self.business = extract_business(first)
        stars = sorted(set(self.options.stars) or range(1, 6), reverse=True)
        capped = self._scrape_pass(first, self.options.stars, label="")

        # Trustpilot n'affiche que 10 pages : au-delà, il renvoie des avis déjà vus.
        # On contourne en parcourant chaque note séparément (10 pages chacune).
        if capped and not self.options.max_pages:
            if len(stars) > 1:
                self.warnings.append(
                    "Trustpilot limite l'affichage à 10 pages : récupération note par note "
                    "pour obtenir tous les avis."
                )
                for star in stars:
                    self._sleep(self.options.delay)
                    try:
                        data = self.fetch_page(1, stars=[star])
                    except ScraperError as e:
                        self.warnings.append(f"{star}★ : {e}")
                        continue
                    if data and self._scrape_pass(data, [star], label=f"{star}★"):
                        self.warnings.append(
                            f"Plus de 10 pages d'avis {star}★ : certains peuvent manquer "
                            "(filtre par langue pour les obtenir)."
                        )
            else:
                self.warnings.append(
                    "Trustpilot limite l'affichage à 10 pages : certains avis peuvent manquer."
                )
        return self.results()

    def _scrape_pass(self, first: dict, stars: list[int], label: str) -> bool:
        """Parcourt les pages d'une recherche. Retourne True si la limite de pages est atteinte."""
        pages = total_pages(first)
        if self.options.max_pages:
            pages = min(pages, self.options.max_pages)

        items = extract_reviews(first, self.host)
        seen = {r["id"] for r in items}
        self._add(self.collected, items)
        self._progress(1, pages, label)

        for p in range(2, pages + 1):
            self._sleep(self.options.delay)
            try:
                data = self.fetch_page(p, stars=stars)
            except ScraperError as e:
                self.warnings.append(str(e))
                continue
            if data is None:
                self.warnings.append(f"Page {p} inaccessible (fin de pagination).")
                return True
            items = extract_reviews(data, self.host)
            ids = {r["id"] for r in items}
            if not ids or ids <= seen:
                return True  # page vide ou déjà vue : limite de pagination atteinte
            seen |= ids
            self._add(self.collected, items)
            self._progress(p, pages, label)
        return False

    def results(self) -> list[dict]:
        return sorted(self.collected.values(), key=lambda r: r["date"], reverse=True)

    @staticmethod
    def _add(store: dict, items: Iterable[dict]):
        for r in items:
            store[r["id"] or f"_{len(store)}"] = r

    def _progress(self, page: int, pages: int, label: str = ""):
        self.on_progress(
            {
                "page": page,
                "pages": pages,
                "label": label,
                "count": len(self.collected),
                "engine": self.engine_used,
                "business": self.business,
            }
        )


def summarize(reviews: list[dict]) -> dict:
    """Statistiques simples sur un lot d'avis."""
    dist = {str(i): 0 for i in range(1, 6)}
    total = 0
    for r in reviews:
        try:
            n = int(r["rating"])
        except (TypeError, ValueError):
            continue
        dist[str(n)] = dist.get(str(n), 0) + 1
        total += n
    rated = sum(dist.values())
    return {
        "count": len(reviews),
        "average": round(total / rated, 2) if rated else None,
        "distribution": dist,
        "replied": sum(1 for r in reviews if r.get("reply")),
        "verified": sum(1 for r in reviews if r.get("verified")),
    }
