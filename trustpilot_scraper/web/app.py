"""API web + interface : lancer une extraction, suivre sa progression, exporter."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..exporters import EXPORTERS
from ..scraper import Cancelled, ScrapeOptions, ScraperError, TrustpilotScraper, summarize

STATIC = Path(__file__).parent / "static"
MAX_JOBS = 50  # on garde les N dernières extractions en mémoire

app = FastAPI(title="Trustpilot Reviews Scraper")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class JobRequest(BaseModel):
    brand: str = Field(..., min_length=3)
    stars: list[int] = Field(default_factory=list)
    languages: str = ""
    max_pages: int | None = Field(default=None, ge=1)
    delay: float = Field(default=1.5, ge=0.5, le=10)
    engine: str = Field(default="auto", pattern="^(auto|http|browser)$")
    show_browser: bool = False


class Job:
    def __init__(self, req: JobRequest):
        self.id = uuid.uuid4().hex[:12]
        self.req = req
        self.status = "queued"  # queued | running | done | cancelled | error
        self.progress: dict = {"page": 0, "pages": 0, "count": 0}
        self.business: dict = {}
        self.reviews: list[dict] = []
        self.warnings: list[str] = []
        self.error: str | None = None
        self.created = time.time()
        self.cancel_event = threading.Event()
        self.scraper: TrustpilotScraper | None = None

    def run(self):
        self.status = "running"
        try:
            self.scraper = TrustpilotScraper(
                self.req.brand,
                ScrapeOptions(
                    stars=[s for s in self.req.stars if 1 <= s <= 5],
                    languages=self.req.languages,
                    max_pages=self.req.max_pages,
                    delay=self.req.delay,
                    engine=self.req.engine,
                    show_browser=self.req.show_browser,
                ),
                on_progress=self._on_progress,
                cancel_event=self.cancel_event,
            )
            self.reviews = self.scraper.run()
            self.status = "done"
        except Cancelled:
            self.reviews = self.scraper.results() if self.scraper else []
            self.status = "cancelled"
        except ScraperError as e:
            self.error = str(e)
            self.status = "error"
        except Exception as e:  # noqa: BLE001 - on remonte tout à l'interface
            self.error = f"Erreur inattendue : {e}"
            self.status = "error"
        finally:
            if self.scraper:
                self.business = self.scraper.business or self.business
                self.warnings = self.scraper.warnings

    def _on_progress(self, info: dict):
        self.progress = {k: info.get(k) for k in ("page", "pages", "count", "engine", "label")}
        self.business = info.get("business") or self.business

    @property
    def domain(self) -> str:
        return self.scraper.domain if self.scraper else self.req.brand

    def to_dict(self, with_reviews: bool = False) -> dict:
        reviews = self.reviews if self.status in ("done", "cancelled") else []
        d = {
            "id": self.id,
            "brand": self.req.brand,
            "domain": self.domain,
            "url": self.scraper.base_url if self.scraper else None,
            "status": self.status,
            "progress": self.progress,
            "business": self.business,
            "warnings": self.warnings,
            "error": self.error,
            "summary": summarize(reviews) if reviews else None,
        }
        if with_reviews:
            d["reviews"] = reviews
        return d


JOBS: dict[str, Job] = {}


def _get_job(job_id: str) -> Job:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Extraction inconnue")
    return job


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/jobs")
def create_job(req: JobRequest):
    from ..scraper import normalize_domain

    try:
        normalize_domain(req.brand)
    except ScraperError as e:
        raise HTTPException(400, str(e))

    job = Job(req)
    JOBS[job.id] = job
    for old in sorted(JOBS.values(), key=lambda j: j.created)[:-MAX_JOBS]:
        JOBS.pop(old.id, None)
    threading.Thread(target=job.run, daemon=True).start()
    return job.to_dict()


@app.get("/api/jobs")
def list_jobs():
    return [j.to_dict() for j in sorted(JOBS.values(), key=lambda j: j.created, reverse=True)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, reviews: bool = False):
    return _get_job(job_id).to_dict(with_reviews=reviews)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _get_job(job_id)
    job.cancel_event.set()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/export/{fmt}")
def export_job(job_id: str, fmt: str):
    job = _get_job(job_id)
    if fmt not in EXPORTERS:
        raise HTTPException(400, "Format inconnu (csv, json, xlsx)")
    if job.status not in ("done", "cancelled"):
        raise HTTPException(409, "Extraction pas encore terminée")
    media, fn = EXPORTERS[fmt]
    filename = f"{job.domain.replace('.', '_')}_reviews.{fmt}"
    return Response(
        fn(job.reviews, job.business),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
