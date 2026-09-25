"""
Ligne de commande.

    python -m trustpilot_scraper getstryde.co
    python -m trustpilot_scraper https://fr.trustpilot.com/review/getstryde.co -o avis.xlsx --stars 1 2
    python -m trustpilot_scraper amazon-login                    # connexion Amazon (une fois)
    python -m trustpilot_scraper amazon https://www.amazon.fr/dp/B0C1234567 -o avis.xlsx
    python -m trustpilot_scraper serve                           # lance l'interface web
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .exporters import EXPORTERS
from .scraper import Cancelled, ScrapeOptions, ScraperError, TrustpilotScraper, summarize


def serve(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="trustpilot_scraper serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)

    import uvicorn

    print(f"Interface disponible sur http://{a.host}:{a.port}", file=sys.stderr)
    uvicorn.run("trustpilot_scraper.web.app:app", host=a.host, port=a.port)
    return 0


def progress(info: dict):
    mode = " (navigateur)" if info.get("engine") == "browser" else ""
    label = f"[{info['label']}] " if info.get("label") else ""
    print(
        f"{label}Page {info['page']}/{info['pages']} : {info['count']} avis uniques{mode}",
        file=sys.stderr,
    )


def run_and_export(scraper, output: str | None) -> int:
    """Lance l'extraction puis écrit le fichier (format déduit de l'extension)."""
    out = Path(output or f"{scraper.slug}_reviews.csv")
    fmt = out.suffix.lstrip(".").lower()
    if fmt not in EXPORTERS:
        print(f"❌ Format non supporté : .{fmt} (csv, json ou xlsx)", file=sys.stderr)
        return 2
    try:
        reviews = scraper.run()
    except ScraperError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, Cancelled):
        print("Interrompu.", file=sys.stderr)
        return 130

    out.write_bytes(EXPORTERS[fmt][1](reviews, scraper.business, scraper.FIELDS))
    for w in scraper.warnings:
        print(f"[warn] {w}", file=sys.stderr)
    s = summarize(reviews)
    print(f"\n✅ {s['count']} avis (note moyenne {s['average']}) écrits dans {out}", file=sys.stderr)
    return 0


def amazon_login_cmd(argv: list[str]) -> int:
    from .amazon import AMAZON_DOMAINS, amazon_login

    p = argparse.ArgumentParser(
        prog="trustpilot_scraper amazon-login",
        description="Ouvre un navigateur pour te connecter à Amazon (la connexion est mémorisée).",
    )
    p.add_argument("--domain", default="amazon.fr", choices=AMAZON_DOMAINS)
    a = p.parse_args(argv)
    try:
        ok = amazon_login(a.domain, on_status=lambda m: print(m, file=sys.stderr))
    except ScraperError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    print("✅ Connecté à Amazon." if ok else "❌ Connexion non terminée.", file=sys.stderr)
    return 0 if ok else 1


def amazon_cmd(argv: list[str]) -> int:
    from .amazon import AMAZON_DOMAINS, AmazonScraper

    p = argparse.ArgumentParser(
        prog="trustpilot_scraper amazon",
        description="Extrait les avis d'un produit Amazon (connexion requise : voir amazon-login).",
    )
    p.add_argument("product", help="Lien de la fiche produit Amazon ou ASIN (ex. B0C1234567)")
    p.add_argument("--domain", default="amazon.fr", choices=AMAZON_DOMAINS,
                   help="Site Amazon si tu donnes un ASIN (défaut amazon.fr)")
    p.add_argument("-o", "--output", help="Fichier de sortie (.csv, .json, .xlsx). Défaut : amazon_<ASIN>_reviews.csv")
    p.add_argument("--stars", type=int, nargs="+", choices=range(1, 6), help="Ne garder que ces notes")
    p.add_argument("--max-pages", type=int, help="Pages max par recherche (10 au plus)")
    p.add_argument("--delay", type=float, default=2.0, help="Pause entre deux pages, en secondes (défaut 2)")
    p.add_argument("--show-browser", action="store_true", help="Afficher la fenêtre (utile en cas de captcha)")
    a = p.parse_args(argv)
    try:
        scraper = AmazonScraper(
            a.product,
            domain=a.domain,
            stars=a.stars,
            max_pages=a.max_pages,
            delay=a.delay,
            show_browser=a.show_browser,
            on_progress=progress,
        )
    except ScraperError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return run_and_export(scraper, a.output)


def trustpilot_cmd(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="trustpilot_scraper",
        description="Extrait les avis Trustpilot d'une marque (CSV, JSON ou Excel). "
        "Autres commandes : amazon, amazon-login, serve.",
    )
    p.add_argument("brand", help="Domaine (getstryde.co) ou URL Trustpilot de la marque")
    p.add_argument("-o", "--output", help="Fichier de sortie (.csv, .json, .xlsx). Défaut : <domaine>_reviews.csv")
    p.add_argument("--stars", type=int, nargs="+", choices=range(1, 6), help="Ne garder que ces notes")
    p.add_argument("--lang", default="", help="Langue des avis : all, fr, en... (défaut : celle du site)")
    p.add_argument("--max-pages", type=int, help="Nombre maximum de pages à parcourir")
    p.add_argument("--delay", type=float, default=1.5, help="Pause entre deux pages, en secondes (défaut 1.5)")
    p.add_argument(
        "--engine",
        choices=["auto", "http", "browser"],
        default="auto",
        help="auto (défaut) : HTTP, puis vrai navigateur si Trustpilot bloque ; http ; browser",
    )
    p.add_argument("--show-browser", action="store_true", help="Afficher la fenêtre du navigateur")
    a = p.parse_args(argv)
    try:
        scraper = TrustpilotScraper(
            a.brand,
            ScrapeOptions(
                stars=a.stars or [],
                languages=a.lang,
                max_pages=a.max_pages,
                delay=a.delay,
                engine=a.engine,
                show_browser=a.show_browser,
            ),
            on_progress=progress,
        )
    except ScraperError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return run_and_export(scraper, a.output)


COMMANDS = {"serve": serve, "amazon": amazon_cmd, "amazon-login": amazon_login_cmd}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in COMMANDS:
        return COMMANDS[argv[0]](argv[1:])
    return trustpilot_cmd(argv)


if __name__ == "__main__":
    raise SystemExit(main())
