"""
Vercel Serverless Endpoint: /api/cron
=====================================
Dedicated cron endpoint for cron-job.org and manual API triggers.
Always returns JSON summary of the scraper run.
"""

import sys
import os
import asyncio

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from main import run_job_alert_pipeline

app = FastAPI(title="Job Scraper Cron API")


@app.api_route("/{path:path}", methods=["GET", "POST"])
@app.api_route("", methods=["GET", "POST"])
async def trigger_cron(request: Request):
    """
    Executes the job alert scraper pipeline and returns JSON stats.
    Protected by API_SECRET via header (X-API-Secret / Authorization) or query param (?secret=).
    """
    expected = os.environ.get("API_SECRET", "").strip()
    if expected:
        provided = (
            request.headers.get("x-api-secret", "").strip()
            or request.query_params.get("secret", "").strip()
            or request.query_params.get("key", "").strip()
            or request.headers.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()
        )
        if provided != expected:
            raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        summary = await asyncio.to_thread(run_job_alert_pipeline)
        return summary
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(exc)},
        )
