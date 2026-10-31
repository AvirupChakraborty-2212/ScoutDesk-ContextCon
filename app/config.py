"""Configuration: load environment variables and expose typed settings."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Crustdata ---
CRUSTDATA_API_KEY: str = os.getenv("CRUSTDATA_API_KEY", "")

# --- LLM ---
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_MODEL: str | None = os.getenv("LLM_MODEL") or None
LLM_MAX_CONCURRENCY: int = int(os.getenv("LLM_MAX_CONCURRENCY", "4"))
LLM_FALLBACK_PROVIDER: str | None = os.getenv("LLM_FALLBACK_PROVIDER") or None
LLM_FALLBACK_API_KEY: str | None = os.getenv("LLM_FALLBACK_API_KEY") or None

# --- Run settings ---
RUN_BUDGET_CREDITS: int = int(os.getenv("RUN_BUDGET_CREDITS", "150"))
TOP_N: int = int(os.getenv("TOP_N", "4"))
CACHE_DIR: Path = Path(os.getenv("CACHE_DIR", ".cache/crust"))
OFFLINE: bool = os.getenv("OFFLINE", "0") == "1"

# --- Paths ---
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
THESES_DIR: Path = PROJECT_ROOT / "theses"
RUNS_DIR: Path = PROJECT_ROOT / "runs"
STATIC_DIR: Path = PROJECT_ROOT / "static"
COHORTS_DIR: Path = PROJECT_ROOT / "cohorts"
PORTFOLIO_PATH: Path = PROJECT_ROOT / "portfolio.yaml"
