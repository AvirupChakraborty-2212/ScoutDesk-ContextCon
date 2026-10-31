# Scout Desk

**ContextCon 03 · IIT Bombay · Theme: Investing**

A multi-agent service that does a VC analyst's first-pass work: find startups that fit a fund's thesis, research them, argue for and against, and decide **Meet / Watch / Pass**. Every claim traces back to Crustdata.

## Quick Start

```bash
cp .env.example .env
# Fill in your keys in .env

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://localhost:8000` in your browser.

## Architecture

See [SCOUTDESK_PLAN.md](SCOUTDESK_PLAN.md) for the full design document.

## Offline / Demo Mode

Set `OFFLINE=1` in `.env` to serve only from cached Crustdata responses — no network needed.

