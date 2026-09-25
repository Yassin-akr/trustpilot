"""Extraction des avis Trustpilot d'une marque."""

from .scraper import ScrapeOptions, ScraperError, TrustpilotScraper, summarize

__all__ = ["ScrapeOptions", "ScraperError", "TrustpilotScraper", "summarize"]
