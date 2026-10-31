# ScoutDesk

**Autonomous Multi-Agent Venture Capital Intelligence & Diligence Platform**  
*Built for ContextCon 03 · IIT Bombay · Theme: Investing*

> ScoutDesk is an autonomous multi-agent investment analyst team that discovers early-stage founders before their first institutional round, tracks portfolio cash runway before founders disclose distress, and verifies every claim against Crustdata's live knowledge graph using strict temporal firewalls.

---

## 1. Executive Summary & Problem

### The Service ScoutDesk Takes On
Early-stage venture capital firms receive thousands of candidate companies annually. Evaluating this funnel requires:
1. **Pre-round discovery**: Identifying repeat founders and high-pedigree operators spinning out of scaled tech companies before they announce funding.
2. **Quantitative diligence**: Tracking real headcount trajectories, funding histories, hiring velocities, and competitor overlap.
3. **IC Memo preparation**: Synthesizing verifiable, factual investment committee memos without subjective spin or hallucinated numbers.
4. **Portfolio health surveillance**: Estimating burn rates and cash runway months before quarterly updates arrive.

### Who Does This Job Today
Junior venture capital analysts and associates spend 40+ hours per week manually searching LinkedIn, Crunchbase, GitHub, and corporate registries. The process is manual, error-prone, subjective, and suffers from retrospective hindsight bias.

### The Solution: ScoutDesk
ScoutDesk coordinates an autonomous swarm of 7 specialized analytical agents to perform complete, end-to-end evaluations in seconds. It enforces:
* **Zero Hallucinated Numbers**: Pure-code scoring and fact verification. LLMs never compute scores or manipulate numbers.
* **Cryptographic Citation Integrity**: Every claim references an immutable `Fact ID` in the ledger with source endpoint, field path, and timestamp.
* **Strict Temporal Firewalls**: Simulates historical decision points (`as_of`) with zero look-ahead data leakage.

---

## 2. Multi-Agent Architecture

```
User Investment Thesis & Request (+ optional as_of date)
                           │
                           ▼
              [ Orchestrator & Time Firewall ]
                           │
               ┌───────────┴───────────┐
               ▼                       ▼
      [ Sourcing Agent ]      [ Target Cohort ]
               │                       │
               └───────────┬───────────┘
                           ▼
          [ Parallel Analyst Swarm (Crustdata) ]
          ├── Company Analyst   (Identity, Funding, Headcount)
          ├── Founder Analyst   (Operator Pedigree, Roles, Scale)
          ├── Signals Analyst   (Hiring Velocity, Openings)
          └── Market Analyst    (Direct Competitor Benchmarks)
                           │
                           ▼
                    [ Fact Ledger ]  <── All claims stored with endpoint, path & known_since
                           │
                           ▼
                 [ Time Firewall View ] <── Blinds agents to facts where known_since > as_of
                           │
                           ▼
                 [ Pure-Code Scorer ]   <── Deterministic thesis scoring (0-100)
                           │
                           ▼
              [ Memo Writer Agent (LLM) ] <── Uses {F<id>} placeholders only
                           │
                           ▼
              [ Skeptic Challenger (LLM) ]<── Challenges optimistic assumptions
                           │
                           ▼
               [ Verifier Agent (Code) ]  <── Strips uncited claims & validates direction
                           │
                           ▼
              [ IC Partner Verdict (LLM) ] <── Back / Watch / Pass / Human Review
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
      [ Capital Allocator ]   [ Runway Watch ]
      (Optimal Check Size)    (Portfolio Burn Horizon)
```

### The Analytical Agents

| Agent | Type | Core Responsibility |
|---|---|---|
| **Sourcing Agent** | Crustdata Search | Queries `/screener/company/search` using structured filter trees to discover candidates matching sector, geography, and pre-round stages. |
| **Company Analyst** | Crustdata Enrich | Fetches canonical company identity, year founded, country, total investment USD, headcount history, and funding milestones. |
| **Founder Analyst** | Crustdata People | Resolves founder profiles, extracts previous employers, analyzes organizational scale (e.g., 180k at Google, 30k at Uber), tenure, and repeat founder status. |
| **Signals Analyst** | Crustdata Hiring/News | Evaluates hiring expansion, active openings count in last 90 days, role distribution (Engineering vs. GTM), and recent press. |
| **Market Analyst** | Crustdata Competitors | Benchmarks competitors, comparing peer headcount, market overlap, and relative traction velocity. |
| **Scorer** | Pure Python Code | Evaluates companies deterministically against fund thesis thresholds (Founders: 35%, Thesis Fit: 25%, Signals: 20%, Momentum: 20%). Zero LLM scoring. |
| **Memo Writer** | Gemini LLM | Drafts structured IC memos referencing `{F<id>}` placeholders instead of typing free-form numbers. |
| **Skeptic** | Gemini LLM | Red-teams the memo, detecting contradictions and challenging growth assumptions. |
| **Verifier** | Deterministic Code | Replaces `{F<id>}` placeholders with formatted values, verifies directionality (e.g., flagging "growth" when headcount shrank), and deletes unverified claims. |
| **IC Partner** | Rules + LLM Rationale | Issues the final investment call (**Back**, **Watch**, **Pass**, or **Human Review**) with a 3-sentence partner rationale grounded in thesis thresholds. |
| **Runway Watch** | Proxy Analytics | Estimates cash burn and remaining months for portfolio companies based on headcount, geography, and funding milestones. |

---

## 3. End-to-End Diligence & Intelligence Surface

ScoutDesk provides complete, multi-dimensional intelligence across 5 pillars:

### 1. Leadership & Operator Pedigree
* **Founder Profiles**: Name, verified LinkedIn URL, current title.
* **Scaled Company Experience**: Identifies previous employers with organization scale (e.g., Google 180,000 employees, Uber 30,000 employees, Rubrik 3,500 employees).
* **Operator Tenure**: Quantified years of experience at scaled technology companies.
* **Serial Founder Bonus**: Confirms past exit or founding history.

### 2. Capital Structure & Financing History
* **Total Investment**: Total capital raised in USD.
* **Round Breakdown**: Every funding milestone with series classification (Pre-seed, Seed, Series A, Series B), amount raised, date announced, and lead investors.
* **Stage Alignment**: Evaluates whether stage aligns with the fund thesis (e.g. pre-seed max $2M vs growth stage).

### 3. Headcount, Growth & Momentum Trajectory
* **Current Scale**: Total verified headcount.
* **Growth Velocity**: Annualized Year-over-Year (YoY %), Quarter-over-Quarter (QoQ %), and Month-over-Month (MoM %) delta tracking.
* **Velocity Classification**: Evaluated as Hyper-growth (+50%+ YoY), High Velocity (+20-50%), Moderate, or Contracting (-YoY).

### 4. Talent Signals & Competitor Landscape
* **Active Open Roles**: Number of active openings in the last 90 days.
* **Talent Focus**: Mix of open roles (Engineering/AI vs. Sales/GTM).
* **Direct Competitor Benchmarking**: Automatically extracts competitor peer companies from Crustdata and benchmarks their headcounts side-by-side.

### 5. Transparent Thesis Threshold Matrix
Every decision is grounded in a transparent comparison table:
* **Sector Focus**: Evaluated against thesis target sectors.
* **Target Geography**: Matches primary operating jurisdiction.
* **Headcount Range**: Assesses scale against pre-seed criteria (2 to 40 employees).
* **Prior Rounds**: Strictly checks institutional round count against stage limits (max 0 prior rounds for pre-seed).
* **Founder Pedigree**: Validates tier-1 tech operator background.

---

## 4. UI Dashboard & Interactive Diligence

The ScoutDesk web application (`http://localhost:8000`) provides a real-time reactive interface:

* **Live Trace Swimlanes**: Follows each company in its own animated lane with Server-Sent Events (SSE) streaming every API call, fact extraction, and reasoning step.
* **Rich Verdict Cards**:
  * 👥 **Leadership Pill**: Displays founders and previous scaled employers.
  * 💰 **Capital Pill**: Total raised, round count, and stage.
  * 📈 **Headcount Pill**: Team size and YoY growth velocity.
  * 🎯 **Market Pill**: Active job openings and competitors tracked.
  * **Threshold Alert Tags**: Color-coded badges explaining exactly why a company passed or failed criteria (e.g. `⚠️ Exceeds pre-seed stage (2 prior rounds, $26M)`, `✓ Scaled operator pedigree (Uber 30k)`).
* **Deep-Dive IC Memo Modal**:
  * **Executive KPI Tiles**: 4 top summary tiles highlighting Capital, Leadership, Headcount, and Thesis Call.
  * **Thesis Criteria Matrix**: Side-by-side table comparing Fund Requirement vs. Company Actual with `✓ PASS` and `⚠️ FAIL` badges.
  * **Founder Profile Cards**: Complete with LinkedIn links, prior employer organization scale, and verified career history.
  * **Funding Milestones Table**: Round #, Series Type, Amount (USD), and Date.
  * **Headcount Trajectory Grid**: Dedicated tiles for Current Team, YoY %, QoQ %, and MoM % momentum.
  * **Competitor Benchmark Chips**: Direct competitors with verified team sizes.
  * **Audit Trail**: Every sentence and metric is linked to clickable Fact ID chips showing source endpoints, field paths, and raw payloads.

---

## 5. Temporal Firewalls & Historical Backtesting

To eliminate look-ahead bias and evaluate analytical hit rates objectively, ScoutDesk features a strict temporal firewall:

1. **`as_of` Date Blinding**: When an `as_of` date is provided (e.g., `2024-01-01`), the `FactLedger` masks every fact whose `known_since` date is after that cutoff.
2. **Entity Anonymization**: Company names and identifiable tokens are pseudonymized before prompting the LLM, preventing the model from utilizing post-cutoff training memory.
3. **Outcome Evaluation**: The harness evaluates historical calls (Back / Pass) against subsequent financing rounds actually recorded in Crustdata 12–24 months later to compute precision, recall, and false-positive rates.

---

## 6. How to Run

### Prerequisites
* Python 3.11, 3.12, or 3.13
* Git

### Step 1: Clone Repository
```bash
git clone https://github.com/AvirupChakraborty-2212/ScoutDesk-ContextCon.git
cd ScoutDesk-ContextCon
```

### Step 2: Environment Configuration
Create a `.env` file in the project root:
```env
# Crustdata API Key
CRUSTDATA_API_KEY="your_crustdata_api_token"

# LLM API Key (Google Gemini recommended)
GEMINI_API_KEY="your_gemini_api_key"
LLM_PROVIDER="gemini"
LLM_MODEL="gemini-3.5-flash-lite"
```

### Step 3: Virtual Environment Setup

**On Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**On Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Step 4: Run the Application

**On Windows (PowerShell):**
```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
```

**On Linux / macOS:**
```bash
uvicorn app.main:app --reload --port 8000
```

Open **[http://localhost:8000](http://localhost:8000)** in your browser.

---

## 7. Running Unit Tests

ScoutDesk includes a test suite covering the Fact Ledger, time firewalls, verifier citation logic, scoring engine, and Crustdata error handling:

```bash
# Run all unit tests
pytest

# Verbose output
pytest -v
```

All 11 unit tests pass in `< 1.0s`.

---

## 8. API Reference

| Endpoint | Method | Description |
|---|---|---|
| `GET /healthz` | GET | Health check reporting Crustdata, LLM, and active thesis configurations. |
| `GET /api/theses` | GET | Lists available investment theses (e.g. `preseed_india`, `us_ai_seed`). |
| `POST /api/runs` | POST | Launches an autonomous diligence run with natural language query, thesis, and optional cohort. |
| `GET /api/runs/{id}` | GET | Returns completed run events, verdicts, scores, and company profiles. |
| `GET /api/runs/{id}/events` | GET | Live Server-Sent Events (SSE) stream for real-time progress. |
| `GET /api/facts/{id}` | GET | Retrieves full provenance, endpoint, field path, and raw excerpt for a specific Fact ID. |
| `POST /api/runway` | POST | Computes estimated burn rate and remaining cash runway for portfolio domains. |
| `POST /api/backtest` | POST | Executes a historical backtest run with temporal firewall masking. |

---

## 9. Technology Stack

* **Backend & API**: Python 3.13, FastAPI, Uvicorn, Pydantic v2.
* **Network & Data**: Async `httpx` with disk caching (SHA-256) and exponential backoff retry policies.
* **LLM Engine**: Google Gemini API via async REST (`gemini-3.5-flash-lite`) with JSON schema enforcement.
* **Primary Data Provider**: [Crustdata API](https://crustdata.com) (`/company/search`, `/company/enrich`, `/person/search`, `/person/enrich`, `/job/search`).
* **Frontend**: Vanilla ES6 JavaScript, Server-Sent Events, HTML5 semantic layout, custom dark/light responsive CSS.

---

## License & Attribution

Developed for **ContextCon 03** (IIT Bombay). Live graph intelligence powered by the **Crustdata API**.
