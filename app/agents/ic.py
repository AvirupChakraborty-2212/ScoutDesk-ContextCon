"""IC Partner agent — makes the final Back / Watch / Pass / Human Review verdict.

Rules-first decision logic ensures rigorous adherence to the fund thesis thresholds.
Synthesizes a rich, concrete 3-sentence partner rationale grounded in verifiable ledger facts.
Generates an end-to-end company profile for deep-dive investor inspection.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from app.anonymize import Anonymizer
from app.events import EventBus
from app.ledger import LedgerView
from app.llm import LLMFailed, call_json
from app.models import AgentTask, Challenge, Event, Fact, Memo, Score, Verdict
from app.verifier import verify_ic_rationale

logger = logging.getLogger(__name__)

_AGENT = "ic"


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _build_company_profile(
    ledger_view: LedgerView,
    company_id: str,
    thesis: dict,
    score: Score,
    unknowns: list[str],
) -> dict[str, Any]:
    """Extract a comprehensive end-to-end company intelligence profile from the ledger."""
    visible_facts = ledger_view.facts(company_id)

    # 1. Company identity
    name_facts = [f for f in visible_facts if f.key == "company.name"]
    company_name = str(name_facts[0].value) if name_facts and name_facts[0].value else company_id

    domain_facts = [f for f in visible_facts if f.key == "company.domain"]
    domain = str(domain_facts[0].value) if domain_facts and domain_facts[0].value else company_id

    sector_facts = [f for f in visible_facts if f.key == "company.sector"]
    sector = str(sector_facts[0].value) if sector_facts and sector_facts[0].value else "B2B SaaS"

    country_facts = [f for f in visible_facts if f.key == "company.country"]
    country = str(country_facts[0].value) if country_facts and country_facts[0].value else "India"

    year_facts = [f for f in visible_facts if f.key == "company.founded_year"]
    founded_year: int | None = None
    if year_facts and year_facts[0].value:
        try:
            founded_year = int(float(year_facts[0].value))
        except (ValueError, TypeError):
            pass

    desc_facts = [f for f in visible_facts if f.key == "company.description"]
    description = str(desc_facts[0].value) if desc_facts and desc_facts[0].value else ""

    # 2. Capital & Funding
    total_raised_usd: float | None = None
    tr_facts = [f for f in visible_facts if f.key == "company.total_raised_usd"]
    if tr_facts and tr_facts[0].value is not None:
        try:
            total_raised_usd = float(tr_facts[0].value)
        except (ValueError, TypeError):
            pass

    rounds_count: int | None = None
    rc_facts = [f for f in visible_facts if f.key == "company.funding_rounds_count"]
    if rc_facts and rc_facts[0].value is not None:
        try:
            rounds_count = int(float(rc_facts[0].value))
        except (ValueError, TypeError):
            pass

    rounds: list[dict[str, Any]] = []
    for n in range(10):
        amt_facts = [f for f in visible_facts if f.key == f"funding.round_{n}.amount"]
        type_facts = [f for f in visible_facts if f.key == f"funding.round_{n}.type"]
        date_facts = [f for f in visible_facts if f.key == f"funding.round_{n}.date"]
        if amt_facts or type_facts or date_facts:
            amt = float(amt_facts[0].value) if amt_facts and amt_facts[0].value is not None else None
            rtype = str(type_facts[0].value) if type_facts and type_facts[0].value else f"Round {n+1}"
            rdate = str(date_facts[0].value) if date_facts and date_facts[0].value else None
            rounds.append({
                "round_number": n + 1,
                "type": rtype,
                "amount_usd": amt,
                "date": rdate,
            })

    stage = "Pre-seed / Seed"
    if rounds:
        latest_type = rounds[0].get("type", "").lower()
        if "seed" in latest_type:
            stage = "Seed Stage"
        elif "series_a" in latest_type or "series a" in latest_type:
            stage = "Series A Stage"
        elif "series_b" in latest_type or "series b" in latest_type:
            stage = "Series B Stage"
        elif "growth" in latest_type:
            stage = "Growth Stage"
    elif total_raised_usd and total_raised_usd > 5_000_000:
        stage = "Growth Stage"
    elif total_raised_usd and total_raised_usd > 1_000_000:
        stage = "Seed Stage"

    # 3. Headcount, Growth & Sales / Momentum Trajectory
    headcount_total: int | None = None
    hc_facts = [f for f in visible_facts if f.key == "company.headcount_total"]
    if hc_facts and hc_facts[0].value is not None:
        try:
            headcount_total = int(float(hc_facts[0].value))
        except (ValueError, TypeError):
            pass

    headcount_mom: float | None = None
    mom_facts = [f for f in visible_facts if f.key == "company.headcount_mom"]
    if mom_facts and mom_facts[0].value is not None:
        try:
            headcount_mom = float(mom_facts[0].value)
        except (ValueError, TypeError):
            pass

    headcount_qoq: float | None = None
    qoq_facts = [f for f in visible_facts if f.key == "company.headcount_qoq"]
    if qoq_facts and qoq_facts[0].value is not None:
        try:
            headcount_qoq = float(qoq_facts[0].value)
        except (ValueError, TypeError):
            pass

    headcount_yoy: float | None = None
    yoy_facts = [f for f in visible_facts if f.key == "company.headcount_yoy"]
    if yoy_facts and yoy_facts[0].value is not None:
        try:
            headcount_yoy = float(yoy_facts[0].value)
        except (ValueError, TypeError):
            pass

    growth_velocity = "Steady"
    if headcount_yoy is not None:
        if headcount_yoy >= 50.0:
            growth_velocity = "Hyper-growth (+50%+ YoY)"
        elif headcount_yoy >= 20.0:
            growth_velocity = "High velocity (+20-50% YoY)"
        elif headcount_yoy > 0.0:
            growth_velocity = "Moderate growth"
        elif headcount_yoy < 0.0:
            growth_velocity = "Contracting (-YoY)"

    # 4. Founders & Leadership
    founders: list[dict[str, Any]] = []
    founder_subjects = sorted(list(set(f.subject for f in visible_facts if f.subject.startswith("founder:"))))
    for subj in founder_subjects:
        f_name_facts = [f for f in visible_facts if f.subject == subj and f.key == "founder.name"]
        f_name = str(f_name_facts[0].value) if f_name_facts and f_name_facts[0].value else None
        if not f_name:
            continue

        f_li_facts = [f for f in visible_facts if f.subject == subj and f.key == "founder.linkedin_url"]
        f_li = str(f_li_facts[0].value) if f_li_facts and f_li_facts[0].value else None

        f_hc_facts = [f for f in visible_facts if f.subject == subj and f.key == "founder.prior_employer_headcount"]
        prior_hc = float(f_hc_facts[0].value) if f_hc_facts and f_hc_facts[0].value is not None else None

        f_yr_facts = [f for f in visible_facts if f.subject == subj and f.key == "founder.years_at_prior"]
        years_prior = float(f_yr_facts[0].value) if f_yr_facts and f_yr_facts[0].value is not None else None

        f_exp_facts = [f for f in visible_facts if f.subject == subj and f.key == "founder.years_experience"]
        years_exp = float(f_exp_facts[0].value) if f_exp_facts and f_exp_facts[0].value is not None else None

        f_rf_facts = [f for f in visible_facts if f.subject == subj and f.key == "founder.is_repeat_founder"]
        is_repeat = bool(f_rf_facts[0].value) if f_rf_facts and f_rf_facts[0].value is not None else False

        roles: list[str] = []
        for r_fact in sorted([f for f in visible_facts if f.subject == subj and "founder.role" in f.key], key=lambda x: x.key):
            roles.append(str(r_fact.value))

        prior_company = None
        if roles:
            for r in roles:
                if company_name.lower() not in r.lower():
                    prior_company = r
                    break
            if not prior_company:
                prior_company = roles[0]

        pedigree_str = f"Operator ({int(years_exp)} yrs experience)" if years_exp else "Operator"
        if prior_hc:
            pedigree_str = f"Scaled operator pedigree ({int(prior_hc):,} org size)"
            if prior_company:
                pedigree_str = f"{prior_company} ({int(prior_hc):,} employees)"

        founders.append({
            "name": f_name,
            "linkedin_url": f_li,
            "prior_company": prior_company,
            "prior_headcount": prior_hc,
            "years_at_prior": years_prior,
            "years_experience": years_exp,
            "is_repeat": is_repeat,
            "roles": roles[:4],
            "pedigree": pedigree_str,
        })

    # Fallback to company.founders or company.founders_raw if no founder subjects
    if not founders:
        raw_f_facts = [f for f in visible_facts if f.key == "company.founders"]
        if raw_f_facts and raw_f_facts[0].value:
            v = raw_f_facts[0].value
            names = v if isinstance(v, list) else [v]
            for n in names:
                founders.append({
                    "name": str(n),
                    "linkedin_url": None,
                    "prior_company": None,
                    "prior_headcount": None,
                    "years_at_prior": None,
                    "years_experience": None,
                    "is_repeat": False,
                    "roles": [],
                    "pedigree": "Unverified external registry profile",
                })

    # 5. Signals & Hiring
    openings_count: int | None = None
    op_facts = [f for f in visible_facts if f.key == "openings.count" or f.key == "openings.last_90d"]
    if op_facts and op_facts[0].value is not None:
        try:
            openings_count = int(float(op_facts[0].value))
        except (ValueError, TypeError):
            pass

    openings_growth: float | None = None
    opg_facts = [f for f in visible_facts if f.key == "openings.growth_percent"]
    if opg_facts and opg_facts[0].value is not None:
        try:
            openings_growth = float(opg_facts[0].value)
        except (ValueError, TypeError):
            pass

    jobs: list[dict[str, Any]] = []
    job_facts = [f for f in visible_facts if f.key.startswith("signals.job.") and f.key.endswith(".title")]
    for jf in job_facts[:5]:
        jobs.append({"title": str(jf.value)})

    news: list[dict[str, Any]] = []
    news_facts = [f for f in visible_facts if f.key.startswith("signals.news.") and f.key.endswith(".title")]
    for nf in news_facts[:5]:
        news.append({"title": str(nf.value)})

    # 6. Competitors
    competitors: list[dict[str, Any]] = []
    comp_facts = [f for f in visible_facts if f.key.startswith("competitor.") and f.key.endswith(".name")]
    for cf in comp_facts[:6]:
        parts = cf.key.split(".")
        c_num = parts[1] if len(parts) >= 2 else "0"
        hcf = [f for f in visible_facts if f.key == f"competitor.{c_num}.headcount"]
        chc = int(float(hcf[0].value)) if hcf and hcf[0].value is not None else None
        competitors.append({
            "name": str(cf.value),
            "headcount": chc,
        })

    # 7. Concrete Thesis Threshold Comparison
    co_cfg = thesis.get("company", {})
    hc_cfg = co_cfg.get("headcount", {})
    min_hc = int(hc_cfg.get("min", 2))
    max_hc = int(hc_cfg.get("max", 40))
    max_rounds = int(co_cfg.get("max_rounds", 0))
    target_geos = thesis.get("geographies", ["India"])
    target_sectors = thesis.get("sectors", ["B2B SaaS", "AI"])

    thresholds = []

    # Sector
    sector_match = any(s.lower() in sector.lower() or sector.lower() in s.lower() for s in target_sectors)
    thresholds.append({
        "criterion": "Sector Focus",
        "required": ", ".join(target_sectors),
        "actual": sector,
        "status": "pass" if sector_match else "fail",
        "detail": f"Aligns with target {', '.join(target_sectors)}" if sector_match else f"Outside target sector ({sector})",
    })

    # Geo
    geo_match = any(g.lower() in country.lower() or country.lower() in g.lower() for g in target_geos)
    thresholds.append({
        "criterion": "Target Geography",
        "required": ", ".join(target_geos),
        "actual": country,
        "status": "pass" if geo_match else "fail",
        "detail": f"Matches geographic mandate ({country})" if geo_match else f"Outside mandate ({country} vs {', '.join(target_geos)})",
    })

    # Headcount
    if headcount_total is not None:
        hc_ok = min_hc <= headcount_total <= max_hc
        thresholds.append({
            "criterion": "Headcount Scale",
            "required": f"{min_hc} - {max_hc} employees (Pre-seed)",
            "actual": f"{headcount_total} employees",
            "status": "pass" if hc_ok else "fail",
            "detail": f"Within pre-seed target range ({headcount_total} employees)" if hc_ok else f"Exceeds pre-seed limit ({headcount_total} > {max_hc})",
        })
    else:
        thresholds.append({
            "criterion": "Headcount Scale",
            "required": f"{min_hc} - {max_hc} employees",
            "actual": "Unknown",
            "status": "neutral",
            "detail": "Headcount data unconfirmed in primary registry",
        })

    # Rounds / Capital
    if rounds_count is not None:
        rounds_ok = rounds_count <= max_rounds
        raised_str = f" (${total_raised_usd / 1_000_000:.1f}M raised)" if total_raised_usd else ""
        thresholds.append({
            "criterion": "Prior Institutional Rounds",
            "required": f"<= {max_rounds} rounds (Pre-seed)",
            "actual": f"{rounds_count} rounds{raised_str}",
            "status": "pass" if rounds_ok else "fail",
            "detail": f"Pre-round / minimal prior funding ({rounds_count} rounds)" if rounds_ok else f"Exceeds pre-seed limit ({rounds_count} rounds raised)",
        })

    # Founders
    has_operator_founder = any(f.get("prior_headcount", 0) and f.get("prior_headcount", 0) >= 500 for f in founders)
    if founders:
        top_f = founders[0]
        f_detail = top_f.get("pedigree", "Verified founders")
        thresholds.append({
            "criterion": "Founder Operator Pedigree",
            "required": "Scaled tech operator (>500 emp)",
            "actual": top_f.get("name", "Founders identified"),
            "status": "pass" if has_operator_founder else "neutral",
            "detail": f_detail,
        })
    else:
        thresholds.append({
            "criterion": "Founder Operator Pedigree",
            "required": "Scaled tech operator (>500 emp)",
            "actual": "Unverified",
            "status": "fail",
            "detail": "No founder profiles could be autonomously verified in primary registry",
        })

    return {
        "name": company_name,
        "domain": domain,
        "sector": sector,
        "country": country,
        "founded_year": founded_year,
        "description": description,
        "capital": {
            "total_raised_usd": total_raised_usd,
            "rounds_count": rounds_count,
            "rounds": rounds,
            "stage": stage,
        },
        "traction": {
            "headcount_total": headcount_total,
            "headcount_mom": headcount_mom,
            "headcount_qoq": headcount_qoq,
            "headcount_yoy": headcount_yoy,
            "growth_velocity": growth_velocity,
        },
        "founders": founders,
        "signals": {
            "openings_count": openings_count,
            "openings_growth_percent": openings_growth,
            "jobs": jobs,
            "news": news,
        },
        "competitors": competitors,
        "threshold_analysis": thresholds,
        "key_score_reasons": score.reasons,
    }


def _build_detailed_rationale(
    decision: str,
    score: Score,
    company_name: str,
    profile: dict[str, Any],
    thesis: dict[str, Any],
    unknowns: list[str],
) -> str:
    """Construct a rigorous, concrete 3-sentence IC partner rationale citing exact facts and threshold metrics."""
    cap = profile.get("capital", {})
    trac = profile.get("traction", {})
    founders = profile.get("founders", [])
    thresholds = profile.get("threshold_analysis", [])
    fails = [t for t in thresholds if t.get("status") == "fail"]

    total_raised_m = (cap.get("total_raised_usd") or 0.0) / 1_000_000.0
    rounds_count = cap.get("rounds_count") or 0
    hc = trac.get("headcount_total")
    yoy = trac.get("headcount_yoy")
    country = profile.get("country", "")
    sector = profile.get("sector", "")

    f_str = ""
    if founders:
        f_names = [f["name"] for f in founders if f.get("name")]
        if f_names:
            top_f = founders[0]
            ped = top_f.get("prior_company") or (f"{int(top_f['prior_headcount']):,} employee org" if top_f.get("prior_headcount") else "")
            f_str = f"Founders ({', '.join(f_names[:2])}) bring operator pedigree{f' from {ped}' if ped else ''}"

    if decision == "pass":
        s1 = f"We recommend a Pass on {company_name} with an overall thesis score of {score.total:.1f}/100 ({score.confidence:.0%} confidence)."

        fail_reasons: list[str] = []
        if hc and hc > 40:
            fail_reasons.append(f"headcount of {hc} exceeds our early-stage maximum of 40")
        if rounds_count > 0:
            funds_str = f" (${total_raised_m:.1f}M raised)" if total_raised_m > 0 else ""
            fail_reasons.append(f"already completed {rounds_count} institutional round(s){funds_str}, placing it beyond pre-seed scope")
        if country and country.lower() not in ["india", "ind"]:
            fail_reasons.append(f"headquartered in {country}, outside our India geographic mandate")
        if not fail_reasons:
            for f in fails:
                fail_reasons.append(f["detail"])
        if not fail_reasons:
            fail_reasons.append("metrics do not satisfy minimum threshold requirements across target criteria")

        s2 = f"The company does not meet our thesis threshold: {'; '.join(fail_reasons)}."

        if f_str:
            s3 = f"While {f_str.lower()}, {company_name} has progressed beyond our target pre-seed deployment stage; we will track for future growth co-investments."
        else:
            s3 = f"We will archive this profile and monitor for major strategic pivots or thesis re-alignment."

        return f"{s1} {s2} {s3}"

    elif decision == "human_review":
        s1 = f"{company_name} requires Human Review with an automated score of {score.total:.1f}/100 ({score.confidence:.0%} confidence)."

        ambiguities: list[str] = []
        if "founders_unidentified" in unknowns or not founders:
            ambiguities.append("primary corporate registries could not autonomously confirm founder executive profiles")
        if profile.get("signals", {}).get("openings_count") == 0:
            ambiguities.append("active hiring signals are currently dormant (0 open roles)")
        if score.confidence < 0.7:
            ambiguities.append(f"data coverage confidence ({score.confidence:.0%}) falls below automated threshold")
        if not ambiguities:
            ambiguities.append("key entity identity and registry records require manual confirmation")

        s2 = f"Automated scoring flagged data gaps: {'; '.join(ambiguities)}."
        s3 = f"An investment associate must manually verify founder credentials and current operational status before capital allocation."
        return f"{s1} {s2} {s3}"

    elif decision == "watch":
        s1 = f"We recommend placing {company_name} on Watch status with an overall thesis score of {score.total:.1f}/100 ({score.confidence:.0%} confidence)."
        traction_str = f"Headcount is {hc} team members" if hc else "Initial company traction is established"
        if yoy:
            traction_str += f" ({yoy:+.1f}% YoY)"
        s2 = f"{traction_str} in {sector}, but hiring velocity and operator signal depth require further maturation."
        s3 = f"We will track ongoing execution velocity and re-evaluate upon subsequent milestone achievements or next financing round."
        return f"{s1} {s2} {s3}"

    else:  # back
        s1 = f"We recommend a high-conviction Back call on {company_name} driven by a thesis score of {score.total:.1f}/100 ({score.confidence:.0%} confidence)."
        f_highlight = f_str if f_str else "The founding team demonstrated elite operator pedigree"
        traction_info = f"paired with {hc} team members and {yoy:+.1f}% YoY growth" if (hc and yoy) else "paired with strong early traction"
        s2 = f"{f_highlight}, {traction_info}."
        s3 = f"The company perfectly matches our {country} {sector} pre-seed thesis and demonstrates exceptional execution potential."
        return f"{s1} {s2} {s3}"


async def run(
    task: AgentTask,
    score: Score,
    memo: Memo,
    challenges: list[Challenge],
    ledger_view: LedgerView,
    thesis: dict,
    bus: EventBus,
    unknowns: list[str],
    *,
    anonymizer: Anonymizer | None = None,
) -> Verdict:
    """Decide investment verdict (Back / Watch / Pass / Human Review) with rich factual grounding."""
    company_id = task.company_id
    run_id = task.run_id

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=company_id,
            kind="start",
            message=f"Investment Committee convening for verdict on {company_id}",
        )
    )

    # 1. Build comprehensive company profile from ledger
    profile = _build_company_profile(ledger_view, company_id, thesis, score, unknowns)
    company_name = profile["name"]
    visible_facts = ledger_view.facts(company_id)

    # 2. Deterministic decision rules
    min_confidence = float(thesis.get("min_confidence_for_auto_verdict", 0.6))
    thresholds = thesis.get("verdict_thresholds", {"back": 70, "watch": 50})
    back_thresh = float(thresholds.get("back", 70))
    watch_thresh = float(thresholds.get("watch", 50))

    flags: list[str] = []

    if score.confidence < 0.7:
        flags.append("low_confidence")

    founder_facts = [f for f in visible_facts if f.subject.startswith("founder") or "founder" in f.key]
    if len(founder_facts) < 2:
        flags.append("sparse_founder_data")

    openings_facts = [f for f in visible_facts if f.key == "openings.last_90d"]
    if not openings_facts or openings_facts[0].value == 0:
        flags.append("no_hiring_signal")

    rounds_facts = [f for f in visible_facts if f.key == "company.funding_rounds_count"]
    hc_facts = [f for f in visible_facts if f.key == "company.headcount_total"]
    if rounds_facts and rounds_facts[0].value == 0:
        if hc_facts:
            try:
                if float(hc_facts[0].value) < 5:
                    flags.append("pre_revenue_signal")
            except (ValueError, TypeError):
                pass

    if challenges:
        flags.append("skeptic_challenges_flagged")

    # Determine Decision
    decision: Literal["back", "watch", "pass", "human_review"]
    if (
        score.confidence < min_confidence
        or "founders_unidentified" in unknowns
        or "ambiguous_identity" in unknowns
        or (score.parts.get("founders", 0) < 20 and score.confidence < 0.5)
    ):
        decision = "human_review"
    elif score.total >= back_thresh:
        decision = "back"
    elif score.total >= watch_thresh:
        decision = "watch"
    else:
        decision = "pass"

    # 3. Build detailed, data-driven rationale
    rationale = _build_detailed_rationale(decision, score, company_name, profile, thesis, unknowns)

    # 4. Optional LLM refinement if feasible
    prompt = f"""You are the Managing Partner of an elite venture fund writing the final Investment Committee verdict rationale.
Company: {company_name}
Decision: {decision.upper()}
Total Score: {score.total}/100
Confidence: {score.confidence:.0%}
Sub-scores: {score.parts}
Key Thresholds: {profile.get('threshold_analysis')}
Key Reasons: {score.reasons[:4]}
Flags: {flags}

Write EXACTLY 3 clear, professional sentences explaining this {decision.upper()} decision.
Be precise and cite the key metrics, headcount, funding, and founder pedigree.
Return JSON with 'rationale' (string) and 'used_fact_ids' (list of strings).
"""

    schema = {
        "type": "object",
        "properties": {
            "rationale": {"type": "string"},
            "used_fact_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["rationale"],
    }

    try:
        data = await call_json(prompt, schema=schema, run_id=run_id)
        llm_rationale = str(data.get("rationale", "")).strip()
        used_fids = [str(f) for f in data.get("used_fact_ids", [])]

        if anonymizer:
            llm_rationale = anonymizer.deanonymize(llm_rationale)

        passed, violations = verify_ic_rationale(
            llm_rationale,
            used_fids,
            ledger_view,
            as_of=ledger_view.as_of,
        )
        if passed and len(llm_rationale.split(".")) >= 2:
            rationale = llm_rationale
        else:
            logger.info("Using data-grounded rationale template for %s: %s", company_name, rationale)

    except (LLMFailed, Exception) as exc:
        logger.info("LLM IC rationale skipped (%s), using data-grounded rationale", exc)

    verdict = Verdict(
        company_id=company_id,
        name=company_name,
        decision=decision,
        score=score,
        memo=memo,
        rationale=rationale,
        flags=flags,
        check_size=None,
        company_profile=profile,
    )

    await bus.emit(
        Event(
            run_id=run_id,
            ts=_now(),
            agent=_AGENT,
            company_id=company_id,
            kind="verdict",
            message=f"FINAL VERDICT: {decision.upper()} ({score.total:.1f}/100, confidence {score.confidence:.0%})",
            data={
                "decision": decision,
                "score": score.total,
                "confidence": score.confidence,
                "flags": flags,
                "rationale": rationale,
                "company_profile": profile,
                "verdict": verdict.model_dump(mode="json"),
            },
        )
    )

    return verdict
