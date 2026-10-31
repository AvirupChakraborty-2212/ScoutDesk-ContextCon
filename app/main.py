"""FastAPI application — routes, SSE event streaming, API endpoints, and static file serving."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.backtest import run_backtest
from app.config import (
    CRUSTDATA_API_KEY,
    LLM_API_KEY,
    OFFLINE,
    PORTFOLIO_PATH,
    RUNS_DIR,
    STATIC_DIR,
    THESES_DIR,
)
from app.crust import CreditCounter
from app.events import bus as global_bus
from app.ledger import FactLedger
from app.models import BacktestReport, Event, Fact, RunwayReport, Verdict
from app.orchestrator import load_thesis, run_pipeline
from app.runway import load_portfolio, run_runway_watch

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Scout Desk",
    description="Autonomous VC Analyst Agents with Time Firewall and Provenance Fact Ledger",
    version="0.2.0",
)

# Enable CORS for developer ease
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store in-memory run results and reports
_RUN_RESULTS: dict[str, list[Verdict]] = {}
_BACKTEST_RESULTS: dict[str, BacktestReport] = {}
_RUNWAY_RESULTS: dict[str, list[RunwayReport]] = {}


# --- Request Models ---

class RunRequest(BaseModel):
    request: str = "Pre-seed/seed Indian B2B companies with operator founders"
    thesis: str = "preseed_india"
    as_of: Optional[str] = None
    companies: Optional[list[str]] = None


class BacktestRequest(BaseModel):
    thesis: str = "preseed_india"
    as_of: str = "2024-01-01"
    cohort: Optional[list[str]] = None
    horizon_months: int = 18


class RunwayRequest(BaseModel):
    domains: Optional[list[str]] = None
    thesis: str = "preseed_india"


# --- Root & Static Files ---

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Scout Desk API running. Visit /static/index.html"}


@app.get("/styles.css")
async def styles_fallback():
    return FileResponse(STATIC_DIR / "styles.css")


@app.get("/app.js")
async def app_js_fallback():
    return FileResponse(STATIC_DIR / "app.js")


# --- Health Check ---

@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "crustdata_key": bool(CRUSTDATA_API_KEY),
        "llm": bool(LLM_API_KEY),
        "offline": OFFLINE,
        "active_theses": [f.stem for f in THESES_DIR.glob("*.yaml")],
    }


# --- Theses ---

@app.get("/api/theses")
async def list_theses():
    theses = []
    for f in THESES_DIR.glob("*.yaml"):
        try:
            t = load_thesis(f.stem)
            theses.append({
                "id": f.stem,
                "name": t.get("name", f.stem),
                "geographies": t.get("geographies", []),
                "sectors": t.get("sectors", []),
                "stage": t.get("stage_focus", "pre_round"),
            })
        except Exception:
            pass
    return {"theses": theses}


# --- Run Endpoints ---

@app.post("/api/runs")
async def create_run(req: RunRequest, background_tasks: BackgroundTasks):
    run_id = f"run-{uuid.uuid4().hex[:8]}"

    # Parse optional as_of date
    as_of_dt: Optional[date] = None
    if req.as_of:
        try:
            as_of_dt = datetime.strptime(req.as_of.strip(), "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid as_of date format. Use YYYY-MM-DD")

    async def _execute_run():
        try:
            verdicts = await run_pipeline(
                run_id=run_id,
                thesis_name_or_dict=req.thesis,
                companies=req.companies,
                as_of=as_of_dt,
            )
            _RUN_RESULTS[run_id] = verdicts
        except Exception as exc:
            logger.error("Run %s failed: %s", run_id, exc, exc_info=True)

    # Launch background task
    background_tasks.add_task(_execute_run)

    return {
        "run_id": run_id,
        "status": "started",
        "thesis": req.thesis,
        "as_of": str(as_of_dt) if as_of_dt else None,
        "stream_url": f"/api/runs/{run_id}/events",
    }


@app.get("/api/runs/{run_id}/events")
async def stream_run_events(run_id: str):
    """Server-Sent Events (SSE) live event stream for a run."""
    async def event_generator():
        try:
            async for event in global_bus.subscribe(run_id):
                payload = json.dumps(event.model_dump(mode="json"), default=str)
                yield f"data: {payload}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/runs/last")
async def get_last_run():
    """Return the most recent run results."""
    if not _RUN_RESULTS:
        raise HTTPException(status_code=404, detail="No completed runs available")
    last_run_id = list(_RUN_RESULTS.keys())[-1]
    return await get_run(last_run_id)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    events = global_bus.get_events(run_id)
    verdicts = _RUN_RESULTS.get(run_id)

    # Check if run dump exists on disk
    dump_path = RUNS_DIR / f"{run_id}.json"
    has_dump = dump_path.exists()

    return {
        "run_id": run_id,
        "completed": verdicts is not None,
        "events_count": len(events),
        "events": [e.model_dump(mode="json") for e in events],
        "verdicts": [v.model_dump(mode="json") for v in (verdicts or [])],
        "has_dump": has_dump,
    }


# --- Fact Lookup for Citation Popovers ---

@app.get("/api/facts/{fact_id}")
async def get_fact(fact_id: str):
    """Look up a Fact with full provenance and raw excerpt for citation popovers."""
    # Search disk dumps for the fact
    if RUNS_DIR.exists():
        for run_file in RUNS_DIR.glob("*.json"):
            try:
                ledger = FactLedger.load(run_file)
                fact = ledger.get(fact_id)
                if fact:
                    return fact.model_dump(mode="json")
            except Exception:
                continue

    raise HTTPException(status_code=404, detail=f"Fact '{fact_id}' not found")


# --- Backtest Endpoints ---

@app.post("/api/backtest")
async def start_backtest(req: BacktestRequest, background_tasks: BackgroundTasks):
    run_id = f"bt-{uuid.uuid4().hex[:8]}"

    try:
        as_of_dt = datetime.strptime(req.as_of.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid as_of date format. Use YYYY-MM-DD")

    async def _execute_backtest():
        try:
            report = await run_backtest(
                thesis_name_or_dict=req.thesis,
                as_of=as_of_dt,
                cohort_domains=req.cohort,
                horizon_months=req.horizon_months,
                run_id=run_id,
            )
            _BACKTEST_RESULTS[run_id] = report
        except Exception as exc:
            logger.error("Backtest %s failed: %s", run_id, exc, exc_info=True)

    background_tasks.add_task(_execute_backtest)

    return {
        "backtest_id": run_id,
        "status": "started",
        "as_of": str(as_of_dt),
        "horizon_months": req.horizon_months,
        "stream_url": f"/api/runs/{run_id}/events",
    }


@app.get("/api/backtest/{backtest_id}")
async def get_backtest(backtest_id: str):
    report = _BACKTEST_RESULTS.get(backtest_id)
    if report:
        return report.model_dump(mode="json")

    # If still processing, return status with current events
    events = global_bus.get_events(backtest_id)
    return {
        "backtest_id": backtest_id,
        "completed": False,
        "events_count": len(events),
        "events": [e.model_dump(mode="json") for e in events],
    }


# --- Runway Watch Endpoints ---

@app.post("/api/runway")
async def trigger_runway(req: RunwayRequest):
    run_id = f"runway-{uuid.uuid4().hex[:8]}"
    global_bus.create_run(run_id)
    domains = req.domains or load_portfolio(PORTFOLIO_PATH)

    if not domains:
        domains = ["sarvam.ai", "krutrim.com", "composio.dev"]

    thesis = load_thesis(req.thesis)
    ledger = FactLedger()
    credits = CreditCounter(budget=50)

    reports = await run_runway_watch(
        portfolio_domains=domains,
        thesis=thesis,
        ledger=ledger,
        bus=global_bus,
        credits=credits,
        run_id=run_id,
    )
    _RUNWAY_RESULTS[run_id] = reports

    return {
        "runway_id": run_id,
        "companies_evaluated": len(reports),
        "reports": [r.model_dump(mode="json") for r in reports],
    }


@app.get("/api/runway/{runway_id}")
async def get_runway(runway_id: str):
    reports = _RUNWAY_RESULTS.get(runway_id)
    if not reports:
        raise HTTPException(status_code=404, detail="Runway report not found")
    return {
        "runway_id": runway_id,
        "reports": [r.model_dump(mode="json") for r in reports],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

