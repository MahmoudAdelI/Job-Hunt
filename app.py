"""
FastAPI Web App & Background Scheduler for .NET Job Alert Scraper
===================================================================
Runs the job scraper pipeline every N minutes via APScheduler in background,
persists state to Upstash Redis or seen_jobs.json, and exposes a web dashboard.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse

from main import run_job_alert_pipeline, load_seen_jobs

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

# App state memory
app_state = {
    "is_running": False,
    "last_run_at": None,
    "last_summary": None,
    "total_runs": 0,
    "error_message": None,
}

scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# Background Task Execution
# ---------------------------------------------------------------------------
async def execute_scraper_pipeline():
    """Execute scraper pipeline in background thread to prevent blocking asyncio loop."""
    if app_state["is_running"]:
        logger.warning("Pipeline run skipped — previous run is still in progress.")
        return {"status": "skipped", "reason": "Already running"}

    app_state["is_running"] = True
    app_state["error_message"] = None

    try:
        logger.info("Starting background scraper execution...")
        # Run blocking scraper in threadpool
        summary = await asyncio.to_thread(run_job_alert_pipeline)
        app_state["last_summary"] = summary
        app_state["last_run_at"] = summary.get("last_run_at")
        app_state["total_runs"] += 1
        logger.info("Pipeline run completed successfully.")
        return summary
    except Exception as exc:
        logger.error("Error executing scraper pipeline: %s", exc, exc_info=True)
        app_state["error_message"] = str(exc)
        return {"status": "error", "error": str(exc)}
    finally:
        app_state["is_running"] = False


# ---------------------------------------------------------------------------
# Lifespan Context Manager
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Start APScheduler
    logger.info("Initializing APScheduler (Interval: %d minutes)...", RUN_INTERVAL_MINUTES)
    scheduler.add_job(
        execute_scraper_pipeline,
        "interval",
        minutes=RUN_INTERVAL_MINUTES,
        id="job_alert_scraper",
        replace_existing=True,
    )
    scheduler.start()

    # Trigger an initial run shortly after server startup in background
    asyncio.create_task(execute_scraper_pipeline())

    yield

    # Shutdown: Shutdown APScheduler cleanly
    logger.info("Shutting down APScheduler...")
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# FastAPI App Setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title=".NET Job Alert Scraper Service",
    description="Automated job scraper running every 5 minutes with Telegram alerts.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
async def health_check():
    """Liveness probe endpoint."""
    return {
        "status": "healthy",
        "service": ".NET Job Alert Scraper",
        "is_running": app_state["is_running"],
        "interval_minutes": RUN_INTERVAL_MINUTES,
        "total_runs": app_state["total_runs"],
        "last_run_at": app_state["last_run_at"],
    }


@app.post("/trigger")
@app.get("/trigger")
async def trigger_scraper(background_tasks: BackgroundTasks):
    """Manually trigger the scraper pipeline immediately."""
    if app_state["is_running"]:
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "message": "Scraper is currently running. Please wait."}
        )

    background_tasks.add_task(execute_scraper_pipeline)
    return {
        "status": "accepted",
        "message": "Scraper execution triggered in background.",
        "interval_minutes": RUN_INTERVAL_MINUTES,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Interactive Web Status Dashboard."""
    seen_entries = len(load_seen_jobs())
    job = scheduler.get_job("job_alert_scraper")
    next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S UTC") if job and job.next_run_time else "N/A"

    last_summary = app_state["last_summary"] or {}
    status_badge = '<span class="badge running">RUNNING NOW</span>' if app_state["is_running"] else '<span class="badge idle">IDLE (ACTIVE)</span>'
    if app_state["error_message"]:
        status_badge = f'<span class="badge error">ERROR: {app_state["error_message"]}</span>'

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>.NET Job Alert Dashboard</title>
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
                    <p style="color: var(--text-muted); font-size: 0.9rem; margin-top: 0.25rem;">FastAPI Background Worker (Runs every {RUN_INTERVAL_MINUTES} mins)</p>
                </div>
                <div>{status_badge}</div>
            </div>

            <div class="grid">
                <div class="card">
                    <div class="card-title">Total Execution Runs</div>
                    <div class="card-value">{app_state['total_runs']}</div>
                    <div class="card-sub">Since process startup</div>
                </div>
                <div class="card">
                    <div class="card-title">Deduplicated Seen Jobs</div>
                    <div class="card-value">{seen_entries}</div>
                    <div class="card-sub">Saved in persistence storage</div>
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
                    <span class="detail-val">{app_state['last_run_at'] or 'Pending initial run...'}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Next Scheduled Run</span>
                    <span class="detail-val">{next_run}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Jobs Fetched (Last Run)</span>
                    <span class="detail-val">{last_summary.get('total_jobs_fetched', 0)}</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Posts Fetched (Last Run)</span>
                    <span class="detail-val">{last_summary.get('total_posts_fetched', 0)}</span>
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
                    alert(data.message || JSON.stringify(data));
                    setTimeout(() => window.location.reload(), 1500);
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


# Export WSGI application interface for servers like PythonAnywhere
try:
    from a2wsgi import ASGIMiddleware
    wsgi_app = ASGIMiddleware(app)
except Exception:
    wsgi_app = None

