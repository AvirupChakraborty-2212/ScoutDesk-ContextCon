"""FastAPI application — routes, SSE, static files."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import CRUSTDATA_API_KEY, LLM_API_KEY, OFFLINE, STATIC_DIR

app = FastAPI(title="Scout Desk", version="0.2.0")

# --- Static files (UI) ---
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Health check ---
@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "crustdata_key": bool(CRUSTDATA_API_KEY),
        "llm": bool(LLM_API_KEY),
        "offline": OFFLINE,
    }


# --- Run endpoints ---
# POST /api/runs                    → start a run, body {request, thesis, as_of?}
# GET  /api/runs/{id}/events        → SSE stream
# GET  /api/runs/{id}               → final RunResult

# --- Fact lookup ---
# GET  /api/facts/{id}              → fact + raw excerpt (for citation popovers)

# --- Backtest ---
# POST /api/backtest                → start a backtest, body {thesis, as_of, cohort, horizon_months}
# GET  /api/backtest/{id}           → BacktestReport

# --- Runway Watch ---
# POST /api/runway                  → runway report for portfolio domains
