"""
FastAPI Web App for .NET Job Alert Scraper (Vercel Serverless)
===============================================================
Stateless serverless app deployed on Vercel.
All state (seen jobs, run stats) is stored in Upstash Redis.
The scraper pipeline is triggered externally by cron-job.org.
"""

import asyncio
import logging
import os

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from main import run_job_alert_pipeline, load_seen_jobs, load_run_summary

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Logging & Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

RUN_INTERVAL_MINUTES = int(os.environ.get("RUN_INTERVAL_MINUTES", "5"))


# ---------------------------------------------------------------------------
# FastAPI App Setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title=".NET Job Alert Scraper Service",
    description="Automated job scraper with Telegram alerts, deployed on Vercel.",
)


# ---------------------------------------------------------------------------
# Security Helper
# ---------------------------------------------------------------------------
def _verify_api_secret(request: Request) -> None:
    """
    Verify the API secret from the X-API-Secret header.
    Used to protect the /api/cron endpoint so only cron-job.org can call it.
    """
    expected = os.environ.get("API_SECRET", "").strip()
    if not expected:
        logger.warning("API_SECRET not configured — cron endpoint is unprotected!")
        return

    provided = request.headers.get("x-api-secret", "").strip()
    if provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    """Liveness probe endpoint. Reads latest stats from Redis."""
    try:
        summary = load_run_summary()
    except Exception:
        summary = {}

    return {
        "status": "healthy",
        "service": ".NET Job Alert Scraper",
        "interval_minutes": RUN_INTERVAL_MINUTES,
        "total_runs": summary.get("total_runs", 0),
        "last_run_at": summary.get("last_run_at"),
    }


@app.get("/api/cron")
async def cron_trigger(request: Request):
    """
    Cron endpoint called by cron-job.org on a schedule.
    Runs the full scraper pipeline synchronously.
    Protected by API_SECRET header.
    """
    _verify_api_secret(request)

    try:
        logger.info("Cron trigger received — starting scraper pipeline...")
        summary = await asyncio.to_thread(run_job_alert_pipeline)
        logger.info("Pipeline completed successfully.")
        return summary
    except Exception as exc:
        logger.error("Pipeline execution failed: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(exc)},
        )


@app.post("/trigger")
@app.get("/trigger")
async def manual_trigger():
    """Manually trigger the scraper pipeline."""
    try:
        logger.info("Manual trigger — starting scraper pipeline...")
        summary = await asyncio.to_thread(run_job_alert_pipeline)
        return summary
    except Exception as exc:
        logger.error("Pipeline execution failed: %s", exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(exc)},
        )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Interactive Web Status Dashboard. Reads all state from Redis."""
    try:
        seen_entries = len(load_seen_jobs())
    except Exception:
        seen_entries = 0

    try:
        last_summary = load_run_summary()
    except Exception:
        last_summary = {}

    total_runs = last_summary.get("total_runs", 0)
    last_run_at = last_summary.get("last_run_at", "Pending initial run...")
    last_status = last_summary.get("status", "unknown")

    if last_status == "success":
        status_badge = '<span class="badge idle">ACTIVE</span>'
    elif last_status == "error":
        status_badge = '<span class="badge error">LAST RUN ERROR</span>'
    else:
        status_badge = '<span class="badge idle">WAITING</span>'

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>.NET Job Alert Dashboard</title>
        <meta name="description" content="Automated .NET job scraper dashboard with Telegram alerts for Egypt-based positions.">
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg: #0f172a;
                --card-bg: #1e293b;
                --text: #f8fafc;
                --text-muted: #94a3b8;
                --accent: #38bdf8;
                --accent-hover: #0284c7;
                --success: #22c55e;
                --warning: #f59e0b;
                --border: #334155;
            }}
            * {{ box-sizing: border-box; margin: 0; padding: 0; }}
            body {{
                font-family: 'Inter', sans-serif;
                background-color: var(--bg);
                color: var(--text);
                padding: 2rem;
                display: flex;
                justify-content: center;
            }}
            .container {{
                max-width: 800px;
                width: 100%;
            }}
            .header {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                margin-bottom: 2rem;
            }}
            h1 {{ font-size: 1.75rem; font-weight: 700; color: #ffffff; }}
            .badge {{
                padding: 0.35rem 0.85rem;
                border-radius: 9999px;
                font-size: 0.8rem;
                font-weight: 600;
                letter-spacing: 0.05em;
            }}
            .badge.idle {{ background: rgba(34, 197, 94, 0.15); color: var(--success); border: 1px solid var(--success); }}
            .badge.running {{ background: rgba(56, 189, 248, 0.15); color: var(--accent); border: 1px solid var(--accent); animation: pulse 1.5s infinite; }}
            .badge.error {{ background: rgba(239, 68, 68, 0.15); color: #ef4444; border: 1px solid #ef4444; }}
            @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}

            .grid {{
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 1.25rem;
                margin-bottom: 2rem;
            }}
            .card {{
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 1.25rem;
            }}
            .card-title {{ font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; font-weight: 600; margin-bottom: 0.5rem; }}
            .card-value {{ font-size: 1.75rem; font-weight: 700; color: #ffffff; }}
            .card-sub {{ font-size: 0.8rem; color: var(--text-muted); margin-top: 0.25rem; }}

            .details-box {{
                background: var(--card-bg);
                border: 1px solid var(--border);
                border-radius: 12px;
                padding: 1.5rem;
                margin-bottom: 2rem;
            }}
            .details-header {{ font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; color: var(--accent); }}
            .detail-row {{ display: flex; justify-content: space-between; padding: 0.6rem 0; border-bottom: 1px solid rgba(255,255,255,0.05); }}
            .detail-row:last-child {{ border-bottom: none; }}
            .detail-label {{ color: var(--text-muted); font-size: 0.9rem; }}
            .detail-val {{ font-weight: 500; font-size: 0.9rem; }}

            .actions {{ display: flex; gap: 1rem; }}
            .btn {{
                background-color: var(--accent);
                color: #0f172a;
                border: none;
                padding: 0.75rem 1.5rem;
                border-radius: 8px;
                font-weight: 600;
                font-size: 0.95rem;
                cursor: pointer;
                transition: background 0.2s;
                text-decoration: none;
                display: inline-block;
            }}
            .btn:hover {{ background-color: var(--accent-hover); }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <div>
                    <h1>🚀 .NET Job Alert Scraper</h1>
                    <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem;">Vercel Serverless + cron-job.org (every {RUN_INTERVAL_MINUTES} mins)</p>
                </div>
                <div>{status_badge}</div>
            </div>

            <div class="grid">
                <div class="card">
                    <div class="card-title">Total Execution Runs</div>
                    <div class="card-value">{total_runs}</div>
                    <div class="card-sub">Persisted in Redis</div>
                </div>
                <div class="card">
                    <div class="card-title">Deduplicated Seen Jobs</div>
                    <div class="card-value">{seen_entries}</div>
                    <div class="card-sub">Stored in Upstash Redis</div>
                </div>
                <div class="card">
                    <div class="card-title">Last Run Alerts Sent</div>
                    <div class="card-value">{last_summary.get('telegram_alerts_sent', 0)}</div>
                    <div class="card-sub">Duration: {last_summary.get('duration_seconds', 0)}s</div>
                </div>
            </div>

            <div class="details-box">
                <div class="details-header">Latest Execution Status</div>
                <div class="detail-row">
                    <span class="detail-label">Last Execution Time</span>
                    <span class="detail-val">{last_run_at}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Jobs Fetched (Last Run)</span>
                    <span class="detail-val">{last_summary.get('total_jobs_fetched', 0)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">New Jobs Found (Last Run)</span>
                    <span class="detail-val">{last_summary.get('new_jobs_found', 0)}</span>
                </div>
            </div>

            <div class="actions">
                <button class="btn" onclick="triggerRun()">⚡ Run Scraper Now</button>
                <a href="/health" class="btn" style="background: transparent; border: 1px solid var(--border); color: var(--text);">API Health Check</a>
            </div>
        </div>

        <script>
            async function triggerRun() {{
                const btn = document.querySelector('.btn');
                btn.innerText = 'Triggering...';
                btn.disabled = true;
                try {{
                    const res = await fetch('/trigger', {{ method: 'POST' }});
                    const data = await res.json();
                    alert(data.status === 'success'
                        ? 'Scraper completed! Alerts sent: ' + (data.telegram_alerts_sent || 0)
                        : data.error || JSON.stringify(data));
                    setTimeout(() => window.location.reload(), 1000);
                }} catch (e) {{
                    alert('Error triggering run: ' + e);
                }} finally {{
                    btn.innerText = '⚡ Run Scraper Now';
                    btn.disabled = false;
                }}
            }}
        </script>
    </body>
    </html>
    """
    return html_content
