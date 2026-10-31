# Scout Desk

**ContextCon 03 · IIT Bombay · Theme: Investing**

> An autonomous investment analyst team that discovers early-stage founders before their first round, tracks portfolio burn before founders disclose it, and verifies every decision against Crustdata's graph with strict temporal firewalls.

---

## 1. The Service

### Which service it takes on
Scout Desk replaces the labor-intensive first-pass screening, thesis-driven sourcing, and portfolio health monitoring traditionally performed by venture capital investment teams.

### Who does that job today
Junior venture capital analysts and associates. They spend 40+ hours each week parsing unstructured startup databases, manually cross-checking founder backgrounds across professional graphs, estimating headcount trends, writing initial Investment Committee (IC) evaluation memos, and chasing portfolio updates.

### Who pays for it
Venture capital funds, family offices, and corporate venture arms (CVCs). Managing partners pay significant payroll for junior talent whose time is consumed by repetitive manual sourcing and error-prone diligence, rather than closing deals and helping portfolio companies.

---

## 2. What It Does Now

### Handled End-to-End
- **Pre-Round Sourcing & Candidate Discovery:** Evaluates natural language investment requests against typed fund theses (e.g., *Pre-seed India B2B with operator founders*) and queries Crustdata to pull candidate companies and executive profiles.
- **Parallel Multi-Agent Research:** 
  - *Company Analyst:* Enriches identity, funding timeline, headcount trajectory, executive rosters, and competitors.
  - *Founder Analyst:* Analyzes work history, operator experience (e.g. 500+ headcount scaling background), and serial founder pedigree.
  - *Signals Analyst:* Categorizes hiring momentum (engineering vs. GTM) and recent press/announcements.
- **Strict Provenance & Fact Ledger:** Every data point extracted from Crustdata is stored as an immutable `Fact` tagged with endpoint, field path, timestamp, and a `known_since` date.
- **Code-Only Scoring & Deterministic Rules:** Quantitative scoring (thesis fit, momentum, founder pedigree, signals) is performed purely in Python, ensuring zero hallucination of numbers.
- **Memo Writer ↔ Skeptic ↔ Verifier Loop:**
  - *Writer (LLM):* Generates memos using `{Fact_ID}` tokens instead of naked numbers.
  - *Skeptic (LLM):* Identifies contradictions and challenges over-optimistic narratives against graph facts.
  - *Verifier (Code):* Blocks untraceable claims, detects mismatches, validates directionality (e.g. "grew" vs. negative growth), and substitutes verified numbers.
- **Time Firewall & Backtesting Harness:** Allows simulating past decision dates (`as_of`). The ledger strictly blinds agents to any fact where `known_since > as_of`, anonymizes prompts to eliminate LLM hindsight bias, and evaluates calls against actual subsequent funding outcomes.
- **Runway Watch (Portfolio Risk):** Estimates cash-out horizons and flags early warning indicators (hiring freezes, negative headcount trends, prolonged periods without new funding).
- **Final Verdicts:** Outputs verifiable **Back**, **Watch**, or **Pass** decisions with structured memos.

### Where It Still Needs a Person (Human-in-the-Loop)
Scout Desk deliberately refuses to guess when data is insufficient or ambiguous:
- **Ambiguous Entity Resolution:** When a company name matches multiple distinct entities with comparable confidence, it raises a flag for human review.
- **Unidentified Founders:** If person enrichments fail to identify active founders/CEOs, it routes the candidate to manual diligence.
- **Low Confidence Threshold:** When data coverage falls below the thesis threshold (`min_confidence`), auto-verdict is withheld.
- **Final Check Signing:** Scout Desk generates the IC memo and verdict rationale; human partners make the legal investment commitment.

---

## 3. What It Could Do Next

### With more time, we would build:
1. **Dynamic Cap Table & Valuation Modeling (Allocator):** Automated valuation range synthesis based on live comparable transaction comps from Crustdata.
2. **Automated Follow-On LP Reporting:** Generating quarterly LP letters from live portfolio data changes.
3. **Automated Outreach Engine with Guardrails:** Personalized outreach draft staging requiring one-click partner authorization before transmission (omitted here per event rules).
4. **Live Crustdata Webhook Streaming:** Real-time push updates rather than scheduled polling for portfolio alerts.

### Why now is not the time:
- Hackathon time is best spent guaranteeing **bulletproof data integrity, zero hallucinated numbers, and rigorous historical backtesting**.
- Legal commitments and cold founder outreach require established human relationship workflows; automating outreach adds surface area without addressing the core investing thesis.

---

## 4. How to Run It, and How It Is Built

### Quick Start

```bash
# 1. Clone & enter workspace
git clone <repo-url>
cd scout-desk

# 2. Configure environment
cp .env.example .env
# Edit .env with your CRUSTDATA_API_KEY and LLM_API_KEY

# 3. Install dependencies
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Start the server
uvicorn app.main:app --reload --port 8000
```
Open `http://localhost:8000` to access the live dashboard.

#### Running in Offline / Demo Mode
To run reliably without live network dependencies (using cached fixtures):
```bash
OFFLINE=1 uvicorn app.main:app --reload --port 8000
```

### Architecture & System Design

```
User Thesis / Request (+ optional as_of date)
                 │
                 ▼
      [ Managing Partner Orchestrator ]
                 │
       ┌─────────┴─────────┐
       ▼                   ▼
 [ Sourcing Agent ]   [ Triage Pass ] ──► Crustdata API
       │                   │
       └─────────┬─────────┘
                 ▼
   [ Parallel Analyst Swarm ]
    ├── Company Analyst   (Identity, Funding, Headcount)
    ├── Founder Analyst   (Pedigree, Operator tenure)
    ├── Signals Analyst   (Hiring velocity, job roles)
    └── Market Analyst    (Peer growth comparison)
                 │
                 ▼
          [ Fact Ledger ]  <── All claims saved with endpoint, field path & known_since
                 │
                 ▼
       [ Time Firewall ]   <── Hides all facts where known_since > as_of
                 │
                 ▼
          [ Pure Scorer ]  <── Code-only math, zero LLM numbers
                 │
                 ▼
       [ Memo Writer (LLM) ] ◄── Uses {F12} placeholders only
                 │
                 ▼
        [ Skeptic (LLM) ]  <── Challenges claims & detects contradictions
                 │
                 ▼
       [ Verifier (Code) ] ──► Reject / Strip unverified claims
                 │
                 ▼
   [ IC Partner Decision ] ──► Back / Watch / Pass / Human Review
                 │
        ┌────────┴────────┐
        ▼                 ▼
 [ Runway Watch ]  [ Backtest Harness ]
 (Portfolio Burn)  (Blind Historical Hit-Rate)
```

### Agents, Models, Tools & Data
- **Infra & API:** FastAPI, `httpx` async client with disk caching (SHA-256) and exponential jitter retries.
- **LLM Layer:** Structured Pydantic outputs with schema validation, retry-on-malformed-JSON, and concurrency control (`asyncio.Semaphore`). Compatible with Gemini, Anthropic, or OpenAI.
- **Crustdata Endpoints Utilized:**
  - `POST /company/search` — Sourcing candidates matching stage, geography, and headcount.
  - `POST /company/enrich` — Core company signals (`basic_info`, `funding`, `headcount`, `people`, `hiring`, `competitors`, `news`).
  - `POST /person/enrich` & `/person/search` — Founder career track record and operator tenure verification.
  - `POST /job/search` — Granular hiring mix and expansion signals.

---

## 5. The Approaches We Tried

### 1. Free-Text LLM Memo Writing vs. Placeholder-Based Verification
- **What We Tried:** Prompts asking the LLM to write investment memos and cite facts directly in natural language.
- **What Failed:** LLMs consistently reformatted currencies, rounded percentages unpredictably (e.g., turning 22.4% into "nearly 25%"), or hallucinated external context not in the prompt.
- **What We Built:** **Placeholders + Code Rendering (`{F12}`).** The LLM is prohibited from typing standalone numbers; it outputs sentence templates with fact IDs. The code verifier substitutes the exact formatted values and validates directional vocabulary (e.g. flagging "growth" if negative).

### 2. Traditional Watchlist vs. "Runway Watch" Proxy
- **What We Tried:** An alert watcher waiting for company events to re-trigger analysis.
- **What Failed:** During a live demo or hackathon evaluation, external company data changes rarely occur in real-time, making a passive watcher unimpressive.
- **What We Built:** **Runway Watch.** Using Crustdata's headcount changes, hiring velocity, and last round dates/amounts, we synthesize a proxy burn rate and alert on portfolio companies running out of cash before disclosure.

### 3. Evaluating on Present-Day Data vs. Historical Time Firewall Backtesting
- **What We Tried:** Screening currently successful companies to demonstrate that the tool picks good companies.
- **What Failed:** Anyone can cherry-pick prominent winners with today's data; it proves nothing about prospective analytical quality and suffers from hindsight leakage.
- **What We Built:** **Temporal Firewall (`as_of` date enforcement).** We evaluate historical cohorts (e.g., as of 2024 or 2025) while strictly masking all facts dated after the cutoff. Company names are anonymized to eliminate LLM memory leakage. We then evaluate hit rates against actual follow-on funding rounds recorded in Crustdata.

---

## License & Attribution
Developed for **ContextCon 03** at IIT Bombay. Data powered by the **Crustdata** API.
