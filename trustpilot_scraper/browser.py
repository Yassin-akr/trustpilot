"""
Mode navigateur : charge les pages dans un vrai Chrome / Edge piloté par Playwright.

Utile quand Trustpilot (pare-feu CloudFront) refuse les requêtes HTTP simples :
un vrai navigateur exécute le défi JavaScript anti-robots et garde le cookie obtenu
pour les pages suivantes. Pour Amazon, un profil persistant conserve la connexion
au compte d'une fois sur l'autre.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

NEXT_DATA_SELECTOR = "script#__NEXT_DATA__"


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def profile_dir(name: str) -> Path:
    """Dossier de profil navigateur persistant (cookies, connexion)."""
    base = os.environ.get("TRUSTPILOT_SCRAPER_HOME") or Path.home() / ".trustpilot_scraper"
    path = Path(base) / f"{name}-profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class BrowserResponse:
    status_code: int
    text: str
    url: str = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


class BrowserSession:
    """Même interface que ``requests.Session.get`` (url, timeout) -> réponse."""

    # Navigateurs essayés dans l'ordre : Chrome installé, Edge installé, Chromium de Playwright.
    CHANNELS = ("chrome", "msedge", None)

    def __init__(
        self,
        headless: bool = True,
        ready_selector: str = NEXT_DATA_SELECTOR,
        require_ready: bool = True,
        user_data_dir: Path | None = None,
        offscreen: bool = False,
        locale: str = "en-GB",
    ):
        """
        ready_selector : élément attendu avant de rendre la page.
        require_ready : si l'élément n'arrive pas, renvoyer 403 (True) ou la page telle quelle.
        user_data_dir : profil persistant (garde les cookies / la connexion).
        offscreen : fenêtre visible mais placée hors de l'écran (moins détectable que headless).
        """
        from playwright.sync_api import sync_playwright

        self.ready_selector = ready_selector
        self.require_ready = require_ready
        # Les cookies « de session » (sans date d'expiration) ne survivent pas à la fermeture
        # du navigateur, même avec un profil persistant : on les sauvegarde nous-mêmes.
        self._cookie_file = Path(user_data_dir) / "cookies.json" if user_data_dir else None
        self._pw = sync_playwright().start()
        self._browser = None
        try:
            self._context = self._launch(headless, user_data_dir, offscreen, locale)
        except Exception:
            self._pw.stop()
            raise
        self._restore_cookies()
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()

    def _restore_cookies(self):
        if not (self._cookie_file and self._cookie_file.exists()):
            return
        try:
            self._context.add_cookies(json.loads(self._cookie_file.read_text("utf-8")))
        except Exception:  # noqa: BLE001 - fichier corrompu : on repart sans
            pass

    def _save_cookies(self):
        if not self._cookie_file:
            return
        try:
            self._cookie_file.write_text(json.dumps(self._context.cookies()), "utf-8")
        except Exception:  # noqa: BLE001 - navigateur déjà fermé par l'utilisateur
            pass

    def _launch(self, headless, user_data_dir, offscreen, locale):
        args = ["--disable-blink-features=AutomationControlled"]
        if offscreen:
            headless = False
            args += ["--window-position=-32000,-32000", "--window-size=1280,900"]

        def launch(**kw):
            kw.update(headless=headless, args=args)
            if user_data_dir:
                return self._pw.chromium.launch_persistent_context(
                    str(user_data_dir), locale=locale, **kw
                )
            self._browser = self._pw.chromium.launch(**kw)
            return self._browser.new_context(locale=locale)

        # Permet de forcer un exécutable précis (ex. : Chromium portable)
        path = os.environ.get("TRUSTPILOT_BROWSER_PATH")
        if path:
            return launch(executable_path=path)
        errors = []
        for channel in self.CHANNELS:
            try:
                return launch(channel=channel)
            except Exception as e:  # noqa: BLE001 - on essaie le suivant
                errors.append(f"{channel or 'chromium'} : {str(e).splitlines()[0]}")
        raise RuntimeError(
            "Aucun navigateur trouvé (Chrome, Edge ou Chromium). Installe Chrome, ou lance "
            "« python -m playwright install chromium ». Détails : " + " | ".join(errors)
        )

    @property
    def page(self):
        return self._page

    def get(self, url: str, timeout: float = 30) -> BrowserResponse:
        ms = int(timeout * 1000)
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=ms)
        except Exception as e:  # noqa: BLE001 - erreur réseau du navigateur
            raise OSError(str(e).splitlines()[0]) from e
        status = resp.status if resp else 0
        if status == 404:
            return BrowserResponse(404, self._page.content(), self._page.url)
        try:
            # Si une page de défi anti-robots s'affiche, elle se recharge d'elle-même
            # une fois le défi résolu : on attend l'arrivée des données.
            self._page.wait_for_selector(self.ready_selector, state="attached", timeout=ms)
        except Exception:  # noqa: BLE001 - élément absent
            if self.require_ready:
                return BrowserResponse(
                    status if status >= 400 else 403, self._page.content(), self._page.url
                )
        return BrowserResponse(200, self._page.content(), self._page.url)

    def wait_until(self, predicate, timeout: float, interval: float = 1.0) -> bool:
        """Attend (ex. : qu'un humain se connecte ou résolve un captcha)."""
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._page.is_closed():
                return False
            try:
                if predicate(self._page):
                    return True
            except Exception:  # noqa: BLE001 - page en cours de navigation
                pass
            try:
                # wait_for_timeout laisse Playwright traiter les événements (≠ time.sleep)
                self._page.wait_for_timeout(interval * 1000)
            except Exception:  # noqa: BLE001 - fenêtre fermée par l'utilisateur
                return False
        return False

    def close(self):
        try:
            self._save_cookies()
            self._context.close()
            if self._browser:
                self._browser.close()
        finally:
            self._pw.stop()
