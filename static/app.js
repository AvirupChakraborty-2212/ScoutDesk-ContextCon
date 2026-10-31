'use strict';

/* ════════════════════════════════════════════════════════════
   Scout Desk — front-end logic
   Talks to the same API as before; only the presentation changed.
   ════════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const ICON_LOCK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="3"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
const ICON_CHEVRON = '<svg class="swimlane-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';

// ════════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════════
const state = {
    runId: null,
    es: null,                    // EventSource
    lanes: {},                   // company_id → { el, eventsEl }
    verdicts: [],
    credits: { used: 0, budget: 150 },
    firewallCount: 0,
    factCache: {},               // fact_id → Fact (fetched on demand)
    activeRunFacts: {},          // fact_id → Fact (from current run events)
    running: false,
};

// ════════════════════════════════════════════════════════════
// Init
// ════════════════════════════════════════════════════════════
window.addEventListener('DOMContentLoaded', async () => {
    bindUI();
    resetTraceBody();
    updateCredits(0, state.credits.budget);
    await checkHealth();
    await loadTheses();
});

function bindUI() {
    $('run-btn').addEventListener('click', startRun);
    $('clear-btn').addEventListener('click', () => clearTrace());
    $('replay-btn').addEventListener('click', replayLastRun);
    $('runway-run-btn').addEventListener('click', startRunway);
    $('bt-run-btn').addEventListener('click', startBacktest);
    $('theme-toggle').addEventListener('click', toggleTheme);
    setupTraceScroll();

    document.querySelectorAll('.seg-btn').forEach((btn) =>
        btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

    // Memo modal: close button, backdrop click, Escape
    $('memo-close').addEventListener('click', closeMemo);
    $('memo-panel').addEventListener('click', (e) => {
        if (e.target === $('memo-panel')) closeMemo();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && $('memo-panel').classList.contains('open')) closeMemo();
    });
}

function toggleTheme() {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('sd-theme', next); } catch (e) { /* storage unavailable */ }
}

async function checkHealth() {
    try {
        const r = await fetch('/healthz');
        const d = await r.json();
        $('health-dot').className = d.ok ? 'ok' : 'error';
        $('health-label').textContent = d.crustdata_key ? 'API ready' : 'No Crustdata key';
        $('offline-badge').hidden = !d.offline;
        state.credits.budget = 150;
    } catch (e) {
        $('health-dot').className = 'error';
        $('health-label').textContent = 'Server offline';
    }
}

async function loadTheses() {
    try {
        const r = await fetch('/api/theses');
        const d = await r.json();
        const sel = $('thesis-select');
        sel.innerHTML = '';
        for (const t of d.theses) {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.name;
            sel.appendChild(opt);
        }
    } catch (e) { /* keep the default options */ }
}

// ════════════════════════════════════════════════════════════
// Run
// ════════════════════════════════════════════════════════════
async function startRun() {
    if (state.running) { toast('A run is already in progress'); return; }

    const request = $('request-input').value.trim()
        || 'Pre-seed/seed Indian B2B companies with operator founders';
    const thesis = $('thesis-select').value;
    const as_of = $('as-of-input').value || null;
    const companiesRaw = $('companies-input').value;
    const companies = companiesRaw
        ? companiesRaw.split('\n').map((s) => s.trim()).filter(Boolean)
        : null;

    clearTrace(false);
    state.running = true;
    userScrolledUp = false;
    setRunBtn(true);
    hideTraceEmpty();

    try {
        const res = await fetch('/api/runs', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ request, thesis, as_of, companies }),
        });
        if (!res.ok) throw new Error(`server returned ${res.status}`);
        const data = await res.json();
        state.runId = data.run_id;

        if (as_of) $('firewall-badge').hidden = false;

        updateRunStatus(`Run ${state.runId} started`);
        subscribeSSE(data.run_id);
    } catch (e) {
        toast('Could not start the run: ' + e.message, true);
        setRunBtn(false);
        state.running = false;
    }
}

function subscribeSSE(runId) {
    if (state.es) state.es.close();
    const es = new EventSource(`/api/runs/${runId}/events`);
    state.es = es;

    es.onmessage = (e) => handleEvent(JSON.parse(e.data));
    es.onerror = () => {
        // The stream closes when the run finishes (or fails)
        es.close();
        state.running = false;
        setRunBtn(false);
        updateRunStatus('Run complete');
    };
}

function handleEvent(ev) {
    const { kind, company_id } = ev;
    const data = ev.data || {};

    if (kind === 'credits' && data.used !== undefined) {
        updateCredits(data.used, data.budget || state.credits.budget);
    }

    if (kind === 'firewall') {
        state.firewallCount += (data.blocked || 1);
        $('firewall-count').textContent = state.firewallCount;
    }

    if (kind === 'fact' && data.fact) {
        state.activeRunFacts[data.fact.id] = data.fact;
    }

    if (kind === 'done') {
        state.running = false;
        setRunBtn(false);
        updateRunStatus('Run complete');
        loadFinalVerdicts(state.runId);
    }

    // Follow new events only if the reader is already at the bottom,
    // so scrolling up to read earlier steps isn't interrupted.
    const body = $('trace-body');
    const stick = body.scrollHeight - body.scrollTop - body.clientHeight < 80;

    if (company_id) {
        ensureLane(company_id);
        appendLaneEvent(company_id, ev);
        if (kind === 'verdict' && data.decision) setLaneStatus(company_id, 'done');
        if (kind === 'error') setLaneStatus(company_id, 'error');
    } else {
        appendGlobalEvent(ev);
    }

    if (stick) body.scrollTop = body.scrollHeight;
}

async function loadFinalVerdicts(runId) {
    try {
        const r = await fetch(`/api/runs/${runId}`);
        const d = await r.json();
        if (d.verdicts && d.verdicts.length) {
            state.verdicts = d.verdicts;
            renderVerdicts(d.verdicts);
            switchTab('calls');
        }
    } catch (e) { toast('Could not load the verdicts', true); }
}

// ════════════════════════════════════════════════════════════
// Swimlanes
// ════════════════════════════════════════════════════════════
function hideTraceEmpty() {
    const el = $('trace-empty');
    if (el) el.remove();
}

function resetTraceBody() {
    const body = $('trace-body');
    body.innerHTML = '';
    body.appendChild($('tpl-trace-empty').content.cloneNode(true));
}


let userScrolledUp = false;

function setupTraceScroll() {
    const body = $('trace-body');
    if (!body) return;
    body.addEventListener('scroll', () => {
        const distFromBottom = body.scrollHeight - body.scrollTop - body.clientHeight;
        userScrolledUp = distFromBottom > 80;
    }, { passive: true });
}

function scrollTraceToBottom(force = false) {
    if (userScrolledUp && !force) return;
    const body = $('trace-body');
    if (body) {
        body.scrollTop = body.scrollHeight;
    }
}

function ensureLane(companyId) {
    if (state.lanes[companyId]) return;
    hideTraceEmpty();

    const lane = document.createElement('div');
    lane.className = 'swimlane';
    lane.id = `lane-${cssId(companyId)}`;

    const header = document.createElement('div');
    header.className = 'swimlane-header';
    header.setAttribute('role', 'button');
    header.tabIndex = 0;
    header.innerHTML = `
    <span class="swimlane-name">${esc(companyId)}</span>
    <span class="swimlane-count" id="lc-${cssId(companyId)}" hidden>0 events</span>
    <span class="swimlane-status running" id="ls-${cssId(companyId)}">Running</span>
    ${ICON_CHEVRON}`;
    const toggle = () => lane.classList.toggle('collapsed');
    header.addEventListener('click', toggle);
    header.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });

    const events = document.createElement('div');
    events.className = 'swimlane-events';

    lane.appendChild(header);
    lane.appendChild(events);
    $('trace-body').appendChild(lane);
    state.lanes[companyId] = {
        el: lane,
        eventsEl: events,
        countEl: $(`lc-${cssId(companyId)}`),
        count: 0,
    };
    scrollTraceToBottom();
}

function appendLaneEvent(companyId, ev) {
    const lane = state.lanes[companyId];
    if (!lane) return;
    lane.count = (lane.count || 0) + 1;
    if (lane.countEl) {
        lane.countEl.textContent = `${lane.count} ${lane.count === 1 ? 'event' : 'events'}`;
        lane.countEl.hidden = false;
    }
    const laneStick = (lane.eventsEl.scrollHeight - lane.eventsEl.scrollTop - lane.eventsEl.clientHeight) < 70;
    lane.eventsEl.appendChild(buildEventRow(ev));
    if (laneStick) {
        lane.eventsEl.scrollTop = lane.eventsEl.scrollHeight;
    }
}

function setLaneStatus(companyId, status) {
    const el = $(`ls-${cssId(companyId)}`);
    if (!el) return;
    el.className = `swimlane-status ${status}`;
    el.textContent = status === 'done' ? 'Done' : status === 'error' ? 'Error' : 'Running';
}

function appendGlobalEvent(ev) {
    const row = document.createElement('div');
    row.className = 'global-event';
    row.innerHTML = `<span class="event-badge" data-kind="${esc(ev.kind)}">${esc(ev.kind)}</span>
    <span class="event-msg">${esc(ev.message)}</span>`;
    hideTraceEmpty();
    const body = $('trace-body');
    body.appendChild(row);
    scrollTraceToBottom();
}

function buildEventRow(ev) {
    const row = document.createElement('div');
    row.className = 'event-row';
    const t = new Date(ev.ts);
    const pad = (n) => String(n).padStart(2, '0');
    const ts = `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
    row.innerHTML = `
    <span class="event-ts">${ts}</span>
    <span class="event-badge" data-kind="${esc(ev.kind)}">${esc(ev.kind)}</span>
    <span class="event-msg">${esc(ev.message)}</span>`;
    return row;
}

function formatUSD(num) {
    if (num === null || num === undefined) return '—';
    const val = Number(num);
    if (isNaN(val)) return String(num);
    if (val >= 1e9) return `$${(val / 1e9).toFixed(1)}B`;
    if (val >= 1e6) return `$${(val / 1e6).toFixed(1)}M`;
    if (val >= 1e3) return `$${(val / 1e3).toFixed(1)}K`;
    return `$${val.toLocaleString()}`;
}

// ════════════════════════════════════════════════════════════
// Verdicts
// ════════════════════════════════════════════════════════════
function renderVerdicts(verdicts) {
    const list = $('verdict-list');
    list.innerHTML = '';
    if (!verdicts.length) return;
    $('calls-empty').style.display = 'none';

    // Back → Watch → Pass → Human review, then by score
    const order = { back: 0, watch: 1, pass: 2, human_review: 3 };
    const rank = (d) => (d in order ? order[d] : 4);
    const sorted = [...verdicts].sort((a, b) =>
        rank(a.decision) - rank(b.decision) || b.score.total - a.score.total);

    for (const v of sorted) list.appendChild(buildVerdictCard(v));
}

function buildVerdictCard(v) {
    const card = document.createElement('div');
    card.className = 'verdict-card';
    card.tabIndex = 0;
    card.setAttribute('role', 'button');
    card.addEventListener('click', () => openMemo(v));
    card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openMemo(v); }
    });

    const score = v.score || {};
    const parts = score.parts || {};
    const prof = v.company_profile || {};
    const cap = prof.capital || {};
    const trac = prof.traction || {};
    const founders = prof.founders || [];
    const signals = prof.signals || {};
    const thresholds = prof.threshold_analysis || [];

    // Ring & Score
    const ringColor = (score.total || 0) >= 70 ? 'var(--sage)'
        : (score.total || 0) >= 50 ? 'var(--sand)' : 'var(--rose)';
    const ringPct = Math.min(100, Math.max(0, score.total || 0));

    // Metrics Pills
    let founderText = 'Unverified';
    if (founders.length) {
        const f0 = founders[0];
        let pOrg = '';
        if (f0.prior_company) {
            const cparts = f0.prior_company.split(' at ');
            pOrg = cparts[1] || cparts[0];
        } else if (f0.prior_headcount) {
            pOrg = `${Number(f0.prior_headcount).toLocaleString()} org`;
        }
        founderText = `${esc(f0.name)}${founders.length > 1 ? ` +${founders.length - 1}` : ''}${pOrg ? ` (ex-${esc(pOrg)})` : ''}`;
    }

    let capitalText = 'Pre-seed / Undisclosed';
    if (cap.total_raised_usd) {
        capitalText = `${formatUSD(cap.total_raised_usd)} • ${cap.rounds_count || 1} rd${(cap.rounds_count || 1) === 1 ? '' : 's'}`;
        if (cap.stage) capitalText += ` (${esc(cap.stage)})`;
    } else if (cap.stage) {
        capitalText = cap.stage;
    }

    let headcountText = 'Headcount: —';
    if (trac.headcount_total !== null && trac.headcount_total !== undefined) {
        headcountText = `${trac.headcount_total} employees`;
        if (trac.headcount_yoy !== null && trac.headcount_yoy !== undefined) {
            headcountText += ` (${trac.headcount_yoy > 0 ? '+' : ''}${trac.headcount_yoy.toFixed(1)}% YoY)`;
        }
    }

    let signalsText = '0 active postings';
    if (signals.openings_count !== null && signals.openings_count !== undefined && signals.openings_count > 0) {
        signalsText = `${signals.openings_count} open role${signals.openings_count === 1 ? '' : 's'}`;
    } else if (prof.competitors && prof.competitors.length) {
        signalsText = `${prof.competitors.length} competitors tracked`;
    }

    const metricsHtml = `
    <div class="card-metrics-grid">
      <div class="card-metric-pill" title="Leadership">
        <span class="card-metric-icon">👥</span>
        <div class="card-metric-body">
          <span class="card-metric-label">Leadership</span>
          <span class="card-metric-val">${founderText}</span>
        </div>
      </div>
      <div class="card-metric-pill" title="Capital & Stage">
        <span class="card-metric-icon">💰</span>
        <div class="card-metric-body">
          <span class="card-metric-label">Capital Raised</span>
          <span class="card-metric-val">${capitalText}</span>
        </div>
      </div>
      <div class="card-metric-pill" title="Headcount & Growth">
        <span class="card-metric-icon">📈</span>
        <div class="card-metric-body">
          <span class="card-metric-label">Headcount / Momentum</span>
          <span class="card-metric-val">${headcountText}</span>
        </div>
      </div>
      <div class="card-metric-pill" title="Talent & Market">
        <span class="card-metric-icon">🎯</span>
        <div class="card-metric-body">
          <span class="card-metric-label">Hiring & Market</span>
          <span class="card-metric-val">${signalsText}</span>
        </div>
      </div>
    </div>`;

    // Threshold Highlights (up to 3 concrete reasons)
    let thresholdPills = '';
    const fails = thresholds.filter((t) => t.status === 'fail');
    const passes = thresholds.filter((t) => t.status === 'pass');

    if (fails.length) {
        thresholdPills = fails.slice(0, 2).map((f) =>
            `<span class="threshold-pill fail">⚠️ ${esc(f.detail)}</span>`).join('');
    }
    if (passes.length && fails.length < 2) {
        thresholdPills += passes.slice(0, 2 - fails.length).map((p) =>
            `<span class="threshold-pill pass">✓ ${esc(p.detail)}</span>`).join('');
    }

    // Score Bars
    const barsHtml = Object.keys(parts).map((k) => {
        const val = Number(parts[k]) || 0;
        const pct = Math.min(100, Math.max(0, val));
        return `<div class="score-bar-row">
      <span class="score-bar-label">${esc(k.replace(/_/g, ' '))}</span>
      <div class="score-bar-track"><div class="score-bar-fill" style="width:${pct}%"></div></div>
      <span class="score-bar-val">${val.toFixed(0)}</span>
    </div>`;
    }).join('');

    const flagsHtml = (v.flags || []).map((f) =>
        `<span class="flag-chip">${esc(f)}</span>`).join('');

    card.innerHTML = `
    <div class="verdict-card-header">
      <div class="verdict-title">
        <span class="verdict-name">${esc(v.name || v.company_id)}</span>
        <span class="badge ${esc(v.decision)}">${decisionLabel(v.decision)}</span>
      </div>
      <div class="ring" style="--p:${ringPct};--ring:${ringColor}" title="Score ${(score.total || 0).toFixed(1)} / 100">
        <span>${Math.round(score.total || 0)}</span>
      </div>
    </div>
    ${metricsHtml}
    ${thresholdPills ? `<div class="threshold-tags-wrap">${thresholdPills}</div>` : ''}
    <div class="score-bars">${barsHtml}</div>
    <div class="confidence-row">Data Confidence <b>${((score.confidence || 0) * 100).toFixed(0)}%</b></div>
    ${flagsHtml ? `<div class="flags-row">${flagsHtml}</div>` : ''}
    ${v.rationale ? `<div class="rationale-text">${esc(v.rationale)}</div>` : ''}
    <button type="button" class="card-memo-btn">
      <span>📊 View Full End-to-End Analysis & Memo →</span>
    </button>`;

    return card;
}

function decisionLabel(d) {
    return { back: 'Back', watch: 'Watch', pass: 'Pass', human_review: 'Human review' }[d] || d;
}

// ════════════════════════════════════════════════════════════
// Memo modal
// ════════════════════════════════════════════════════════════
function openMemo(v) {
    const panel = $('memo-panel');
    $('memo-box-title').textContent = `${v.name || v.company_id} (${v.company_id})`;

    const badge = $('memo-verdict-badge');
    badge.className = `badge ${v.decision}`;
    badge.textContent = decisionLabel(v.decision);

    const prof = v.company_profile || {};
    const cap = prof.capital || {};
    const trac = prof.traction || {};
    const founders = prof.founders || [];
    const signals = prof.signals || {};
    const thresholds = prof.threshold_analysis || [];
    const memo = v.memo || {};
    const sections = memo.sections || {};
    const score = v.score || {};

    let html = '';

    // 1. Executive KPI Tiles Dashboard
    const totalRaisedStr = cap.total_raised_usd ? formatUSD(cap.total_raised_usd) : 'Pre-seed / $0';
    const roundsStr = cap.rounds_count ? `${cap.rounds_count} round(s) • ${cap.stage || 'Seed'}` : (cap.stage || 'Pre-round');

    let fTileVal = 'Unverified';
    let fTileSub = 'No founder records in registry';
    if (founders.length) {
        fTileVal = founders[0].name;
        if (founders.length > 1) fTileVal += ` +${founders.length - 1}`;
        fTileSub = founders[0].prior_headcount
            ? `${founders[0].prior_company || 'Operator'} (${Number(founders[0].prior_headcount).toLocaleString()} org scale)`
            : (founders[0].prior_company || `${founders[0].years_experience || 0} yrs experience`);
    }

    const hcTileVal = trac.headcount_total ? `${trac.headcount_total} employees` : 'Unconfirmed';
    const hcTileSub = trac.headcount_yoy !== null && trac.headcount_yoy !== undefined
        ? `${trac.headcount_yoy > 0 ? '+' : ''}${trac.headcount_yoy.toFixed(1)}% YoY • ${trac.growth_velocity || 'Steady'}`
        : (trac.growth_velocity || 'Headcount momentum tracking');

    const scoreTileVal = `${(score.total || 0).toFixed(1)} / 100`;
    const scoreTileSub = `Confidence: ${((score.confidence || 0) * 100).toFixed(0)}% • ${decisionLabel(v.decision)}`;

    html += `
    <div class="memo-kpi-grid">
      <div class="memo-kpi-card">
        <div class="memo-kpi-head">💰 Capital & Valuation</div>
        <div class="memo-kpi-val">${esc(totalRaisedStr)}</div>
        <div class="memo-kpi-sub">${esc(roundsStr)}</div>
      </div>
      <div class="memo-kpi-card">
        <div class="memo-kpi-head">👥 Leadership & Scale</div>
        <div class="memo-kpi-val">${esc(fTileVal)}</div>
        <div class="memo-kpi-sub">${esc(fTileSub)}</div>
      </div>
      <div class="memo-kpi-card">
        <div class="memo-kpi-head">📈 Headcount & Growth</div>
        <div class="memo-kpi-val">${esc(hcTileVal)}</div>
        <div class="memo-kpi-sub">${esc(hcTileSub)}</div>
      </div>
      <div class="memo-kpi-card">
        <div class="memo-kpi-head">🎯 Thesis Fit & Call</div>
        <div class="memo-kpi-val">${esc(scoreTileVal)}</div>
        <div class="memo-kpi-sub">${esc(scoreTileSub)}</div>
      </div>
    </div>`;

    // 2. Executive Verdict & Thesis Alignment Section
    html += `
    <div class="memo-section-wrap">
      <div class="memo-sec-header">
        <span class="memo-sec-title">1. Investment Committee Verdict & Rationale</span>
        <span class="memo-sec-badge">${decisionLabel(v.decision)}</span>
      </div>
      <div class="rationale-text" style="margin-bottom: 16px;">
        ${esc(v.rationale || '')}
      </div>`;

    if (thresholds.length) {
        html += `
      <div class="memo-table-wrap">
        <table class="memo-table">
          <thead>
            <tr>
              <th>Evaluation Dimension</th>
              <th>Thesis Criterion</th>
              <th>Company Actual</th>
              <th>Alignment Detail</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>`;
        for (const t of thresholds) {
            const tagClass = t.status === 'pass' ? 'pass' : t.status === 'fail' ? 'fail' : 'neutral';
            const tagLabel = t.status === 'pass' ? '✓ PASS' : t.status === 'fail' ? '⚠️ FAIL' : 'NEUTRAL';
            html += `
            <tr>
              <td><b>${esc(t.criterion)}</b></td>
              <td>${esc(t.required)}</td>
              <td><b>${esc(t.actual)}</b></td>
              <td>${esc(t.detail)}</td>
              <td><span class="memo-table-tag ${tagClass}">${tagLabel}</span></td>
            </tr>`;
        }
        html += `
          </tbody>
        </table>
      </div>`;
    }

    if (score.reasons && score.reasons.length) {
        html += `<div style="margin-top: 10px; font-family: var(--font-sans); font-size: 12.5px; color: var(--text-2);">
          <b>Key Quantitative Drivers:</b>
          <ul style="margin: 6px 0 0 18px; line-height: 1.6;">
            ${score.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}
          </ul>
        </div>`;
    }
    html += `</div>`;

    // 3. Founders & Operator Pedigree Section
    html += `
    <div class="memo-section-wrap">
      <div class="memo-sec-header">
        <span class="memo-sec-title">2. Founders & Leadership Team</span>
        <span class="memo-sec-badge">${founders.length} Identified</span>
      </div>`;

    if (founders.length) {
        html += `<div class="founder-cards-grid">`;
        for (const f of founders) {
            const liLink = f.linkedin_url
                ? `<a href="${esc(f.linkedin_url)}" target="_blank" rel="noopener noreferrer" class="founder-link">LinkedIn Profile ↗</a>`
                : '';
            const rolesHtml = (f.roles && f.roles.length)
                ? f.roles.map((r) => `<span>• ${esc(r)}</span>`).join('')
                : '';

            html += `
            <div class="founder-card">
              <div class="founder-card-head">
                <span class="founder-name">${esc(f.name)}</span>
                ${liLink}
              </div>
              <div><span class="founder-pedigree">${esc(f.pedigree || 'Operator')}</span></div>
              ${rolesHtml ? `<div class="founder-roles-list"><b>Career Trajectory:</b>${rolesHtml}</div>` : ''}
              ${f.years_experience ? `<div style="font-size: 11.5px; color: var(--muted);">${f.years_experience} years verified operator experience</div>` : ''}
            </div>`;
        }
        html += `</div>`;
    } else {
        html += `<p class="memo-empty">No founder records could be verified autonomously in primary registries. Flagged for analyst verification.</p>`;
    }

    if (sections.founders && sections.founders.length) {
        html += `<div style="margin-top: 12px;">`;
        for (const s of sections.founders) html += `<div class="memo-sentence">${buildSentenceHtml(s)}</div>`;
        html += `</div>`;
    }
    html += `</div>`;

    // 4. Capital, Funding Rounds & Valuation
    html += `
    <div class="memo-section-wrap">
      <div class="memo-sec-header">
        <span class="memo-sec-title">3. Capital Structure & Financing History</span>
        <span class="memo-sec-badge">${esc(cap.stage || 'Financing')}</span>
      </div>`;

    if (cap.rounds && cap.rounds.length) {
        html += `
      <div class="memo-table-wrap">
        <table class="memo-table">
          <thead>
            <tr>
              <th>Round</th>
              <th>Investment Series</th>
              <th>Amount (USD)</th>
              <th>Announced Date</th>
            </tr>
          </thead>
          <tbody>`;
        for (const r of cap.rounds) {
            html += `
            <tr>
              <td>Round ${r.round_number}</td>
              <td><b>${esc(r.type || 'Institutional')}</b></td>
              <td><b>${formatUSD(r.amount_usd)}</b></td>
              <td>${esc(r.date || 'Undisclosed')}</td>
            </tr>`;
        }
        html += `
          </tbody>
        </table>
      </div>`;
    } else {
        html += `<div class="memo-sentence">${esc(v.name || v.company_id)} has no recorded institutional venture rounds or is pre-round/bootstrapped.</div>`;
    }

    if (sections.market && sections.market.length) {
        html += `<div style="margin-top: 8px;">`;
        for (const s of sections.market) html += `<div class="memo-sentence">${buildSentenceHtml(s)}</div>`;
        html += `</div>`;
    }
    html += `</div>`;

    // 5. Headcount, Growth & Sales / Momentum Trajectory
    html += `
    <div class="memo-section-wrap">
      <div class="memo-sec-header">
        <span class="memo-sec-title">4. Headcount & Momentum Trajectory</span>
        <span class="memo-sec-badge">${esc(trac.growth_velocity || 'Traction')}</span>
      </div>
      <div class="memo-kpi-grid" style="margin-bottom: 14px;">
        <div class="memo-kpi-card">
          <div class="memo-kpi-head">Total Headcount</div>
          <div class="memo-kpi-val">${trac.headcount_total || '—'}</div>
          <div class="memo-kpi-sub">Full-time team members</div>
        </div>
        <div class="memo-kpi-card">
          <div class="memo-kpi-head">YoY Headcount Delta</div>
          <div class="memo-kpi-val">${trac.headcount_yoy !== null && trac.headcount_yoy !== undefined ? `${trac.headcount_yoy > 0 ? '+' : ''}${trac.headcount_yoy.toFixed(1)}%` : '—'}</div>
          <div class="memo-kpi-sub">Annualized growth</div>
        </div>
        <div class="memo-kpi-card">
          <div class="memo-kpi-head">QoQ Growth</div>
          <div class="memo-kpi-val">${trac.headcount_qoq !== null && trac.headcount_qoq !== undefined ? `${trac.headcount_qoq > 0 ? '+' : ''}${trac.headcount_qoq.toFixed(1)}%` : '—'}</div>
          <div class="memo-kpi-sub">Quarterly momentum</div>
        </div>
        <div class="memo-kpi-card">
          <div class="memo-kpi-head">MoM Growth</div>
          <div class="memo-kpi-val">${trac.headcount_mom !== null && trac.headcount_mom !== undefined ? `${trac.headcount_mom > 0 ? '+' : ''}${trac.headcount_mom.toFixed(1)}%` : '—'}</div>
          <div class="memo-kpi-sub">Trailing month change</div>
        </div>
      </div>`;

    if (sections.signals && sections.signals.length) {
        for (const s of sections.signals) html += `<div class="memo-sentence">${buildSentenceHtml(s)}</div>`;
    }
    html += `</div>`;

    // 6. Signals, Jobs & Competitor Benchmarking
    html += `
    <div class="memo-section-wrap">
      <div class="memo-sec-header">
        <span class="memo-sec-title">5. Talent Signals & Competitor Landscape</span>
        <span class="memo-sec-badge">${prof.competitors ? prof.competitors.length : 0} Competitors</span>
      </div>`;

    if (signals.jobs && signals.jobs.length) {
        html += `<div style="margin-bottom: 14px; font-family: var(--font-sans); font-size: 13px;">
          <b>Active Job Openings:</b>
          <div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px;">
            ${signals.jobs.map((j) => `<span class="flag-chip" style="background: var(--surface-3); color: var(--text);">💼 ${esc(j.title)}</span>`).join('')}
          </div>
        </div>`;
    }

    if (prof.competitors && prof.competitors.length) {
        html += `<div style="font-family: var(--font-sans); font-size: 13px;">
          <b>Benchmarked Competitors:</b>
          <div class="competitor-chips">
            ${prof.competitors.map((c) => `
              <div class="competitor-chip">
                <span>🏢 <b>${esc(c.name)}</b></span>
                ${c.headcount ? `<span class="competitor-hc">(${c.headcount} team)</span>` : ''}
              </div>`).join('')}
          </div>
        </div>`;
    }
    html += `</div>`;

    // 7. Skeptic Challenges & Risk Analysis
    html += `
    <div class="memo-section-wrap">
      <div class="memo-sec-header">
        <span class="memo-sec-title">6. Independent Skeptic Analysis & Risks</span>
        <span class="memo-sec-badge">Risk Protocol</span>
      </div>`;

    if (sections.risks && sections.risks.length) {
        for (const s of sections.risks) html += `<div class="memo-sentence">${buildSentenceHtml(s)}</div>`;
    }
    if (sections.bear_case && sections.bear_case.length) {
        html += `<div class="memo-section-title" style="margin-top: 14px;">Bear Case Scenario</div>`;
        for (const s of sections.bear_case) html += `<div class="memo-sentence">${buildSentenceHtml(s)}</div>`;
    }
    html += `</div>`;

    // 8. Evidence Verification & Citations
    html += `
    <div class="memo-section-wrap">
      <div class="memo-sec-header">
        <span class="memo-sec-title">7. Verifiable Fact Citations & Audit Ledger</span>
        <span class="memo-sec-badge">Fact Ledger</span>
      </div>
      <p style="font-family: var(--font-sans); font-size: 12.5px; color: var(--muted); margin-bottom: 12px;">
        Every claim and number above is cross-referenced with an immutable Fact ID in the central ledger. Click any badge to inspect source endpoint, timestamp, and raw payload.
      </p>`;

    if (memo.removed_by_verifier > 0) {
        html += `<div class="memo-removed">The verifier removed ${memo.removed_by_verifier} claim${memo.removed_by_verifier === 1 ? '' : 's'} it could not trace to an immutable Fact ID.</div>`;
    }

    const otherKeys = Object.keys(sections).filter((k) => !['summary', 'founders', 'market', 'signals', 'risks', 'bear_case'].includes(k));
    for (const sec of otherKeys) {
        html += `<div class="memo-section-title">${esc(sec.replace(/_/g, ' '))}</div>`;
        for (const s of sections[sec]) html += `<div class="memo-sentence">${buildSentenceHtml(s)}</div>`;
    }
    html += `</div>`;

    $('memo-content').innerHTML = html;
    $('memo-content').scrollTop = 0;
    bindFactChips();

    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    $('memo-close').focus();
}

function closeMemo() {
    hideFactPop();
    const panel = $('memo-panel');
    panel.classList.remove('open');
    panel.setAttribute('aria-hidden', 'true');
}

function buildSentenceHtml(sentence) {
    const text = sentence.rendered || sentence.template || '';
    const fids = sentence.fact_ids || [];

    // Turn {F12} placeholders into fact chips
    const display = esc(text).replace(/\{([A-Za-z0-9_-]+)\}/g, (_, fid) => buildFactChip(fid));

    // Add chips for cited facts that aren't already inline
    const inTemplate = (text.match(/\{([A-Za-z0-9_-]+)\}/g) || []).map((m) => m.slice(1, -1));
    const extra = fids.filter((f) => !inTemplate.includes(f)).map(buildFactChip).join('');

    return display + (extra ? ' ' + extra : '');
}

function buildFactChip(fid) {
    return `<button type="button" class="fact-chip" data-fid="${esc(fid)}">${esc(fid)}</button>`;
}

async function loadFact(fid) {
    if (state.activeRunFacts[fid] || state.factCache[fid]) return;
    try {
        const r = await fetch(`/api/facts/${encodeURIComponent(fid)}`);
        if (r.ok) state.factCache[fid] = await r.json();
    } catch (e) { /* popover falls back to "not available" */ }
}

function getFact(fid) {
    return state.activeRunFacts[fid] || state.factCache[fid] || null;
}

function formatFactValue(fact) {
    if (fact.value === null || fact.value === undefined) return 'N/A';
    const v = fact.value;
    const u = fact.unit;
    if (u === '%') {
        const pct = typeof v === 'number' ? (Math.abs(v) <= 1 ? v * 100 : v) : parseFloat(v);
        return isNaN(pct) ? String(v) : `${pct.toFixed(1)}%`;
    }
    if (u === 'USD' && typeof v === 'number') {
        if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
        if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
        if (v >= 1e3) return `$${(v / 1e3).toFixed(0)}K`;
        return `$${v}`;
    }
    return String(v) + (u && u !== 'USD' && u !== '%' ? ` ${u}` : '');
}

// ─── Fact popover (single floating element, positioned from the chip) ───
let activeChip = null;

function bindFactChips() {
    $('memo-content').querySelectorAll('.fact-chip').forEach((chip) => {
        const show = () => showFactPop(chip);
        chip.addEventListener('mouseenter', show);
        chip.addEventListener('focus', show);
        chip.addEventListener('mouseleave', hideFactPop);
        chip.addEventListener('blur', hideFactPop);
    });
}

async function showFactPop(chip) {
    activeChip = chip;
    const fid = chip.dataset.fid;
    renderFactPop(fid);
    if (!getFact(fid)) {
        await loadFact(fid);
        if (activeChip === chip) renderFactPop(fid);
    }
}

function renderFactPop(fid) {
    const pop = $('fact-pop');
    const fact = getFact(fid);
    if (!fact) {
        pop.innerHTML = `<div class="fact-popover-key">${esc(fid)}</div><div class="fact-popover-note">Source details are not available for this fact.</div>`;
    } else {
        const fetched = fact.fetched_at ? new Date(fact.fetched_at).toLocaleDateString() : '';
        pop.innerHTML = `
      <div class="fact-popover-key">${esc(fact.key)}</div>
      <div class="fact-popover-val">${esc(formatFactValue(fact))}</div>
      <div class="fact-popover-meta">
        <b>Endpoint</b> ${esc(fact.endpoint)}<br>
        <b>Field</b> ${esc(fact.field_path)}<br>
        ${fact.known_since ? `<b>Known since</b> ${esc(fact.known_since)}<br>` : ''}
        ${fetched ? `<b>Fetched</b> ${esc(fetched)}` : ''}
      </div>
      ${fact.raw_excerpt ? `<div class="fact-popover-raw">${esc(fact.raw_excerpt)}</div>` : ''}`;
    }
    pop.hidden = false;
    positionFactPop();
}

function positionFactPop() {
    const pop = $('fact-pop');
    if (!activeChip || pop.hidden) return;
    const c = activeChip.getBoundingClientRect();
    const p = pop.getBoundingClientRect();
    const gap = 10, margin = 12;

    let left = c.left + c.width / 2 - p.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - p.width - margin));

    let top = c.top - p.height - gap;
    if (top < margin) top = c.bottom + gap;   // not enough room above → show below

    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
}

function hideFactPop() {
    activeChip = null;
    $('fact-pop').hidden = true;
}

// ════════════════════════════════════════════════════════════
// Runway
// ════════════════════════════════════════════════════════════
async function startRunway() {
    const raw = $('runway-domains-input').value.trim();
    const domains = raw ? raw.split(',').map((s) => s.trim()).filter(Boolean) : null;
    const thesis = $('thesis-select').value;

    try {
        const r = await fetch('/api/runway', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ domains, thesis }),
        });
        if (!r.ok) throw new Error(`server returned ${r.status}`);
        const d = await r.json();
        renderRunway(d.reports || []);
    } catch (e) {
        toast('Runway check failed: ' + e.message, true);
    }
}

function renderRunway(reports) {
    const list = $('runway-list');
    list.innerHTML = '';
    if (!reports.length) return;
    $('runway-empty').style.display = 'none';

    for (const rpt of reports) {
        const card = document.createElement('div');
        card.className = 'runway-card';

        const hasMonths = rpt.est_months_left !== null && rpt.est_months_left !== undefined;
        const monthsHtml = hasMonths
            ? `${rpt.est_months_left.toFixed(1)}<small>months left</small>`
            : '—<small>no estimate</small>';
        const monthsColor = rpt.level === 'alert' ? 'var(--rose-ink)'
            : rpt.level === 'watch' ? 'var(--sand-ink)' : 'var(--text)';

        const signalsHtml = (rpt.signals || []).map((s) =>
            `<div class="runway-signal-item">${esc(s)}</div>`).join('');
        const assumptionsHtml = (rpt.assumptions || []).map((a) =>
            `<div class="runway-assumption">${esc(a)}</div>`).join('');

        card.innerHTML = `
      <div class="runway-card-header">
        <span class="runway-company">${esc(rpt.company_id)}</span>
        <span class="runway-level ${esc(rpt.level)}">${esc(rpt.level)}</span>
      </div>
      <div class="runway-months" style="color:${monthsColor}">${monthsHtml}</div>
      <span class="runway-proxy-label">Proxy estimate, not confirmed by the company</span>
      ${signalsHtml ? `<div class="runway-signals">${signalsHtml}</div>` : ''}
      ${assumptionsHtml ? `<div class="runway-assumptions">${assumptionsHtml}</div>` : ''}`;

        list.appendChild(card);
    }
}

// ════════════════════════════════════════════════════════════
// Backtest
// ════════════════════════════════════════════════════════════
async function startBacktest() {
    const as_of = $('bt-as-of').value;
    const horizon_months = parseInt($('bt-horizon').value, 10) || 18;
    const thesis = $('thesis-select').value;

    if (!as_of) { toast('Choose an as-of date first'); return; }

    $('backtest-empty').style.display = 'none';
    $('backtest-content').innerHTML =
        '<div class="empty-state"><div class="spinner"></div><p class="empty-sub">Running backtest…</p></div>';

    try {
        const r = await fetch('/api/backtest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ thesis, as_of, horizon_months }),
        });
        if (!r.ok) throw new Error(`server returned ${r.status}`);
        const d = await r.json();
        const btId = d.backtest_id;

        // Poll until the report is ready
        const poll = async () => {
            try {
                const rr = await fetch(`/api/backtest/${btId}`);
                const dd = await rr.json();
                if (dd.back_hit_rate !== undefined) renderBacktest(dd);
                else setTimeout(poll, 1500);
            } catch (e) {
                toast('Backtest failed: ' + e.message, true);
                $('backtest-content').innerHTML = '';
                $('backtest-empty').style.display = 'flex';
            }
        };
        setTimeout(poll, 1000);
    } catch (e) {
        toast('Backtest failed: ' + e.message, true);
        $('backtest-content').innerHTML = '';
        $('backtest-empty').style.display = 'flex';
    }
}

function renderBacktest(report) {
    $('backtest-empty').style.display = 'none';

    const pct = (x) => (x * 100).toFixed(0);
    const backGood = report.back_hit_rate > report.base_rate;
    const passGood = report.pass_hit_rate < report.base_rate;

    const casesHtml = (report.cases || []).map((c) => {
        const raised = c.outcome_raised_within_horizon;
        const outCls = raised === true ? 'bt-outcome-yes' : raised === false ? 'bt-outcome-no' : 'bt-outcome-unk';
        const outText = raised === true ? 'Raised' : raised === false ? 'Did not raise' : 'Unknown';
        return `<tr>
      <td>${esc(c.company_id)}</td>
      <td><span class="badge badge-sm ${esc(c.decision)}">${decisionLabel(c.decision)}</span></td>
      <td>${Number(c.score).toFixed(1)}</td>
      <td class="${outCls}">${outText}</td>
    </tr>`;
    }).join('');

    $('backtest-content').innerHTML = `
    <div class="backtest-scorecard">
      <div class="backtest-meta">
        As of <b>${esc(report.as_of)}</b>, ${esc(report.horizon_months)}-month horizon,
        <b>${esc(report.n)}</b> companies
      </div>
      <div class="bt-metrics">
        <div class="bt-metric">
          <div class="bt-metric-val ${backGood ? 'good' : 'bad'}">${pct(report.back_hit_rate)}%</div>
          <div class="bt-metric-label">Back hit rate</div>
        </div>
        <div class="bt-metric">
          <div class="bt-metric-val ${passGood ? 'good' : 'bad'}">${pct(report.pass_hit_rate)}%</div>
          <div class="bt-metric-label">Pass hit rate (lower is better)</div>
        </div>
        <div class="bt-metric">
          <div class="bt-metric-val">${pct(report.base_rate)}%</div>
          <div class="bt-metric-label">Cohort base rate</div>
        </div>
        <div class="bt-metric">
          <div class="bt-metric-val">${pct(report.baseline_hit_rate)}%</div>
          <div class="bt-metric-label">Naive baseline</div>
        </div>
      </div>
      <div class="bt-firewall">${ICON_LOCK}<span>${esc(report.facts_blocked)} facts hidden by the time firewall, prompts anonymised</span></div>
    </div>
    ${casesHtml ? `
    <div class="bt-table-wrap">
      <table class="bt-case-table">
        <thead><tr><th>Company</th><th>Decision</th><th>Score</th><th>Outcome</th></tr></thead>
        <tbody>${casesHtml}</tbody>
      </table>
    </div>` : ''}`;
}

// ════════════════════════════════════════════════════════════
// Replay
// ════════════════════════════════════════════════════════════
async function replayLastRun() {
    try {
        const r = await fetch('/api/runs/last');
        if (!r.ok) { toast('No saved run found'); return; }
        const d = await r.json();
        if (d.verdicts && d.verdicts.length) {
            clearTrace(false);
            state.verdicts = d.verdicts;
            renderVerdicts(d.verdicts);
            switchTab('calls');
            toast('Showing the last run');
        } else {
            toast('The last run has no verdicts');
        }
    } catch (e) {
        toast('No saved run available');
    }
}

// ════════════════════════════════════════════════════════════
// UI helpers
// ════════════════════════════════════════════════════════════
function switchTab(name) {
    document.querySelectorAll('.seg-btn').forEach((b) => {
        const on = b.dataset.tab === name;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.tab-panel').forEach((p) =>
        p.classList.toggle('active', p.id === `tab-${name}`));
}

function clearTrace(resetVerdicts = true) {
    userScrolledUp = false;
    resetTraceBody();
    state.lanes = {};
    state.firewallCount = 0;
    $('firewall-count').textContent = '0';
    $('firewall-badge').hidden = true;
    updateRunStatus('');

    if (resetVerdicts) {
        state.verdicts = [];
        $('verdict-list').innerHTML = '';
        $('calls-empty').style.display = 'flex';
    }

    updateCredits(0, state.credits.budget);
    if (state.es) { state.es.close(); state.es = null; }
    state.running = false;
    setRunBtn(false);
}

function setRunBtn(running) {
    $('run-btn-text').textContent = running ? 'Running…' : 'Run analysis';
    $('run-spinner').hidden = !running;
    $('run-btn').disabled = running;
    $('run-btn').querySelector('.run-icon').style.display = running ? 'none' : '';
}

function updateCredits(used, budget) {
    state.credits.used = used;
    state.credits.budget = budget;
    const pct = budget ? (used / budget) * 100 : 0;
    const bar = $('credit-bar');
    bar.style.width = `${Math.min(pct, 100)}%`;
    bar.className = pct > 80 ? 'danger' : pct > 60 ? 'warn' : '';
    $('credit-label').textContent = `${used} / ${budget}`;
}

function updateRunStatus(msg) {
    $('run-status-label').textContent = msg;
}

function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function cssId(s) {
    return String(s).replace(/[^a-z0-9]/gi, '_');
}

let toastTimer;
function toast(msg, isError = false) {
    const el = $('toast');
    el.textContent = msg;
    el.classList.toggle('error', isError);
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 3200);
}