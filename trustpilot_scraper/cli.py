"""
Ligne de commande.

    python -m trustpilot_scraper getstryde.co
    python -m trustpilot_scraper https://fr.trustpilot.com/review/getstryde.co -o avis.xlsx --stars 1 2
    python -m trustpilot_scraper serve           # lance l'interface web
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .exporters import EXPORTERS
from .scraper import ScrapeOptions, ScraperError, TrustpilotScraper, summarize


def serve(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="trustpilot_scraper serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)

    import uvicorn

    print(f"Interface disponible sur http://{a.host}:{a.port}", file=sys.stderr)
    uvicorn.run("trustpilot_scraper.web.app:app", host=a.host, port=a.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "serve":
        return serve(argv[1:])

    p = argparse.ArgumentParser(
        prog="trustpilot_scraper",
        description="Extrait les avis Trustpilot d'une marque (CSV, JSON ou Excel).",
    )
    p.add_argument("brand", help="Domaine (getstryde.co) ou URL Trustpilot de la marque")
    p.add_argument("-o", "--output", help="Fichier de sortie (.csv, .json, .xlsx). Défaut : <domaine>_reviews.csv")
    p.add_argument("--stars", type=int, nargs="+", choices=range(1, 6), help="Ne garder que ces notes")
    p.add_argument("--lang", default="all", help="Langue des avis : all (défaut), fr, en...")
    p.add_argument("--max-pages", type=int, help="Nombre maximum de pages à parcourir")
    p.add_argument("--delay", type=float, default=1.5, help="Pause entre deux pages, en secondes (défaut 1.5)")
    a = p.parse_args(argv)

    def progress(info: dict):
        print(f"Page {info['page']}/{info['pages']} : {info['count']} avis uniques", file=sys.stderr)

    try:
        scraper = TrustpilotScraper(
            a.brand,
            ScrapeOptions(stars=a.stars or [], languages=a.lang, max_pages=a.max_pages, delay=a.delay),
            on_progress=progress,
        )
        reviews = scraper.run()
    except ScraperError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrompu.", file=sys.stderr)
        return 130

    out = Path(a.output or f"{scraper.domain.replace('.', '_')}_reviews.csv")
    fmt = out.suffix.lstrip(".").lower()
    if fmt not in EXPORTERS:
        print(f"❌ Format non supporté : .{fmt} (csv, json ou xlsx)", file=sys.stderr)
        return 2
    out.write_bytes(EXPORTERS[fmt][1](reviews, scraper.business))

    for w in scraper.warnings:
        print(f"[warn] {w}", file=sys.stderr)
    s = summarize(reviews)
    print(f"\n✅ {s['count']} avis (note moyenne {s['average']}) écrits dans {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
