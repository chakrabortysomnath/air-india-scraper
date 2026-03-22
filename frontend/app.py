"""
main.py
FastAPI backend — receives scrape requests from Streamlit,
runs the Playwright scraper, and returns results as JSON.
Also exposes a /download endpoint to serve the Excel file.
"""

import io
import asyncio
import uuid
from typing import Optional
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd

from scraper import scrape_round_trips

app = FastAPI(title="Air India Fare Scraper API")

# Allow Streamlit (any origin) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory job store ────────────────────────────────────────────────────
# Stores job status + results keyed by job_id (UUID string)
# Fine for a single-user deployment; swap for Redis for multi-user.
jobs: dict[str, dict] = {}


# ── Request / response models ──────────────────────────────────────────────

class ScrapeRequest(BaseModel):
    origin: str           # e.g. "CCU"
    destination: str      # e.g. "DEL"
    outbound_start: str   # "YYYY-MM-DD"
    outbound_end: str
    return_start: str
    return_end: str
    adults: int = 1


class JobStatus(BaseModel):
    job_id: str
    status: str           # "pending" | "running" | "done" | "error"
    progress: list[str]   # log messages streamed to frontend
    result_count: int = 0


# ── Background scrape task ─────────────────────────────────────────────────

def _run_scrape(job_id: str, req: ScrapeRequest):
    """Runs in a background thread. Updates jobs[job_id] as it progresses."""
    jobs[job_id]["status"] = "running"

    def log(msg: str):
        jobs[job_id]["progress"].append(msg)

    try:
        results = scrape_round_trips(
            origin=req.origin,
            destination=req.destination,
            outbound_start=req.outbound_start,
            outbound_end=req.outbound_end,
            return_start=req.return_start,
            return_end=req.return_end,
            adults=req.adults,
            progress_callback=log,
        )
        jobs[job_id]["results"] = results
        jobs[job_id]["result_count"] = len(results)
        jobs[job_id]["status"] = "done"
        log(f"✅ Scraping complete — {len(results)} flights found.")

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["progress"].append(f"❌ Fatal error: {e}")


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scrape", response_model=JobStatus)
def start_scrape(req: ScrapeRequest, background_tasks: BackgroundTasks):
    """
    Kick off a scrape job in the background.
    Returns a job_id immediately so the frontend can poll for progress.
    """
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "pending",
        "progress": ["Job queued…"],
        "results": [],
        "result_count": 0,
    }
    background_tasks.add_task(_run_scrape, job_id, req)
    return JobStatus(job_id=job_id, status="pending", progress=["Job queued…"])


@app.get("/status/{job_id}", response_model=JobStatus)
def get_status(job_id: str):
    """Poll this endpoint every few seconds from Streamlit to get live progress."""
    if job_id not in jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    j = jobs[job_id]
    return JobStatus(
        job_id=job_id,
        status=j["status"],
        progress=j["progress"],
        result_count=j["result_count"],
    )


@app.get("/download/{job_id}")
def download_excel(job_id: str):
    """
    Generate and return an Excel file for a completed job.
    Two sheets: All Flights + Cheapest Per Date Pair.
    """
    if job_id not in jobs or jobs[job_id]["status"] != "done":
        return JSONResponse(status_code=400, content={"error": "Job not ready"})

    results = jobs[job_id]["results"]
    if not results:
        return JSONResponse(status_code=404, content={"error": "No data found"})

    df = pd.DataFrame(results)

    # Cheapest round-trip per outbound+return date pair
    cheapest = (
        df.dropna(subset=["Price (INR)"])
          .sort_values("Price (INR)")
          .groupby(["Outbound Date", "Return Date"], as_index=False)
          .first()
          .sort_values(["Outbound Date", "Return Date"])
    )

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="All Flights", index=False)
        cheapest.to_excel(writer, sheet_name="Cheapest Per Date Pair", index=False)

        # Auto-fit column widths
        for sheet in writer.sheets.values():
            for col in sheet.columns:
                w = max(len(str(c.value or "")) for c in col) + 4
                sheet.column_dimensions[col[0].column_letter].width = min(w, 30)

    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=air_india_fares.xlsx"},
    )