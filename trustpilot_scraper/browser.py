"""
Mode navigateur : charge les pages dans un vrai Chrome / Edge piloté par Playwright.

Utile quand Trustpilot (pare-feu CloudFront) refuse les requêtes HTTP simples :
un vrai navigateur exécute le défi JavaScript anti-robots et garde le cookie obtenu
pour les pages suivantes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class BrowserResponse:
    status_code: int
    text: str

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f"HTTP {self.status_code}")


class BrowserSession:
    """Même interface que ``requests.Session.get`` (url, timeout) -> réponse."""

    # Navigateurs essayés dans l'ordre : Chrome installé, Edge installé, Chromium de Playwright.
    CHANNELS = ("chrome", "msedge", None)

    def __init__(self, headless: bool = True):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._launch(headless)
        self._context = self._browser.new_context(locale="en-GB")
        self._page = self._context.new_page()

    def _launch(self, headless: bool):
        # Permet de forcer un exécutable précis (ex. : Chromium portable)
        path = os.environ.get("TRUSTPILOT_BROWSER_PATH")
        if path:
            return self._pw.chromium.launch(executable_path=path, headless=headless)
        errors = []
        for channel in self.CHANNELS:
            try:
                return self._pw.chromium.launch(channel=channel, headless=headless)
            except Exception as e:  # noqa: BLE001 - on essaie le suivant
                errors.append(f"{channel or 'chromium'} : {str(e).splitlines()[0]}")
        self._pw.stop()
        raise RuntimeError(
            "Aucun navigateur trouvé (Chrome, Edge ou Chromium). Installe Chrome, ou lance "
            "« python -m playwright install chromium ». Détails : " + " | ".join(errors)
        )

    def get(self, url: str, timeout: float = 30) -> BrowserResponse:
        ms = int(timeout * 1000)
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=ms)
        except Exception as e:  # noqa: BLE001 - erreur réseau du navigateur
            raise OSError(str(e).splitlines()[0]) from e
        status = resp.status if resp else 0
        if status == 404:
            return BrowserResponse(404, self._page.content())
        try:
            # Si une page de défi anti-robots s'affiche, elle se recharge d'elle-même
            # une fois le défi résolu : on attend l'arrivée des données.
            self._page.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=ms)
        except Exception:  # noqa: BLE001 - pas de données : on renvoie le statut d'origine
            return BrowserResponse(status if status >= 400 else 403, self._page.content())
        return BrowserResponse(200, self._page.content())

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()
