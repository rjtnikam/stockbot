"""Where does the story stand? Is price behind or ahead of it? When might they meet?

Rule-based and transparent: each stage scores the evidence for it, and the
evidence list travels to Claude so the thesis must argue from it.
"""
from __future__ import annotations

import json

from .config import BASE_RATES_FILE

STAGES = ["Hidden", "Inflection", "Recognition", "Consensus", "Decay"]
LEVEL = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _attention(grok: dict, gemini: dict, vp: dict) -> dict:
    x = LEVEL.get(str(grok.get("mention_level", "")).lower())
    cov = LEVEL.get(str(gemini.get("coverage_level", "")).lower())
    vpa = (vp or {}).get("attention") or {}
    vp_ratio = None
    if vpa.get("posts_prior_period"):
        vp_ratio = round(vpa["posts_last_period"] / vpa["posts_prior_period"], 2)
    elif vpa.get("posts_last_period"):
        vp_ratio = 3.0  # new activity from nothing
    levels = [v for v in (x, cov) if v is not None]
    score = sum(levels) / len(levels) if levels else None
    rising = (grok.get("mention_trend") == "rising") or (vp_ratio is not None and vp_ratio > 1.5)
    return {"x_level": grok.get("mention_level"), "x_trend": grok.get("mention_trend"),
            "media_coverage": gemini.get("coverage_level"), "valuepickr_activity_ratio": vp_ratio,
            "attention_score_0_3": round(score, 2) if score is not None else None, "rising": bool(rising)}


def assess(numbers: dict, tech: dict, rs_pct: int | None, grok: dict, gemini: dict, vp: dict,
           hunt_setups: list[str] | None = None) -> dict:
    q, gapd = numbers.get("quarterly", {}), numbers.get("expectations_gap", {})
    pat_trend = q.get("pat_trend")
    pat_yoy = [v for v in q.get("pat_yoy", []) if v is not None]
    last_yoy = pat_yoy[-1] if pat_yoy else None
    w = (tech.get("weinstein") or {}).get("stage")
    gap = gapd.get("implied_minus_delivered_growth")
    att = _attention(grok or {}, gemini or {}, vp or {})
    a = att["attention_score_0_3"]

    s = {k: 0.0 for k in STAGES}
    ev = {k: [] for k in STAGES}

    def add(stage, pts, why):
        s[stage] += pts
        ev[stage].append(why)

    # fundamentals
    if pat_trend == "accelerating":
        add("Inflection", 2, "PAT growth accelerating"); add("Hidden", 1, "PAT growth accelerating")
    if pat_trend == "decelerating":
        add("Consensus", 1, "PAT growth decelerating"); add("Decay", 2, "PAT growth decelerating")
    if last_yoy is not None and last_yoy < 0:
        add("Decay", 2, f"latest PAT YoY negative ({last_yoy:.0%})")
    if pat_trend == "steady" and last_yoy and last_yoy > 0.15:
        add("Recognition", 1, "strong, steady growth")
    # price structure
    if w == 1:
        add("Hidden", 2, "Weinstein stage 1 (basing)"); add("Inflection", 1, "stage 1 base")
    if w == 2:
        add("Inflection", 1, "stage 2 begun"); add("Recognition", 2, "Weinstein stage 2 uptrend")
    if w == 3:
        add("Consensus", 2, "Weinstein stage 3 (topping)")
    if w == 4:
        add("Decay", 2, "Weinstein stage 4 (downtrend)")
    if rs_pct is not None:
        if rs_pct < 50: add("Hidden", 1, f"RS percentile low ({rs_pct})")
        elif rs_pct < 80: add("Inflection", 1, f"RS percentile rising band ({rs_pct})")
        else: add("Recognition", 1, f"RS percentile high ({rs_pct})"); add("Consensus", 0.5, f"RS high ({rs_pct})")
    # attention
    if a is not None:
        if a < 1: add("Hidden", 2, "almost nobody talks about it")
        elif a < 2: add("Inflection", 1, "low-moderate attention")
        else: add("Consensus", 1.5, "high attention"); add("Recognition", 1, "widely discussed")
    if att["rising"]:
        add("Recognition", 1.5, "attention rising (X / ValuePickr)")
    # valuation
    gap_recent = gapd.get("implied_minus_ttm_growth")
    if gap is not None and gap > 0.05 and gap_recent is not None and gap_recent < -0.03:
        add("Inflection", 2, "price implies more than 5y history but less than the new TTM run-rate")
        gap = None  # handled: the market is pricing the change partly, not fully
    if gap is not None:
        if gap < -0.03: add("Hidden", 1, "price implies less growth than delivered"); add("Inflection", 1, "expectations below delivery")
        elif gap > 0.05: add("Consensus", 2, "price implies more growth than delivered"); add("Decay", 0.5, "rich expectations")
        else: add("Recognition", 1, "expectations ≈ delivered growth")

    # hunter setups (filings + capex evidence the price/attention data can't see)
    for setup in hunt_setups or []:
        if setup.startswith("Capex digestion"):
            add("Hidden", 1.5, "capacity built, revenue not yet catching up")
        elif setup.startswith("Capex → revenue"):
            add("Inflection", 2, "capex converting into revenue")
        elif setup.startswith("Coming-out"):
            add("Inflection", 1, "first-ever concall"); add("Recognition", 1, "management starting to court investors")
        elif setup.startswith("Earnings inflection"):
            add("Inflection", 1, "multiple earnings-inflection screens hit")

    ranked = sorted(s.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(v for _, v in ranked) or 1
    best, second = ranked[0], ranked[1]

    # gap direction: story ahead of price, or price ahead of story
    fund_strong = pat_trend == "accelerating" or (last_yoy is not None and last_yoy > 0.20)
    price_lagging = (w in (1, 4)) or (rs_pct is not None and rs_pct < 60) or (gap is not None and gap < -0.03)
    price_leading = (gap is not None and gap > 0.05) or (w in (2, 3) and pat_trend == "decelerating")
    direction = ("story ahead of price" if fund_strong and price_lagging else
                 "price ahead of story" if price_leading else "roughly aligned")

    return {
        "stage": best[0], "stage_confidence": round(best[1] / total, 2),
        "runner_up": second[0], "scores": dict(ranked), "evidence": {k: v for k, v in ev.items() if v},
        "attention": att, "price_vs_story": direction,
        "convergence": convergence_odds(best[0], direction, numbers),
    }


def convergence_odds(stage: str, direction: str, numbers: dict) -> dict:
    """Looks up empirical base rates produced by event_study/convergence.py.

    Never invents probabilities: if the base-rate file doesn't exist yet, says so.
    """
    if not BASE_RATES_FILE.exists():
        return {"available": False,
                "note": "Base rates not built yet. Run event_study (README step 9). Until then, no probabilities."}
    rates = json.loads(BASE_RATES_FILE.read_text())
    mcap = (numbers.get("top_ratios") or {}).get("Market Cap")
    bucket = ("micro (<1,000 Cr)" if mcap and mcap < 1000 else
              "small (1,000-10,000 Cr)" if mcap and mcap < 10000 else "mid+ (>10,000 Cr)")
    key = f"{direction}|{bucket}"
    r = rates.get("buckets", {}).get(key) or rates.get("buckets", {}).get(f"{direction}|all")
    if not r:
        return {"available": False, "note": f"no base-rate bucket for {key}"}
    return {"available": True, "bucket": key, **r, "definition": rates.get("definition")}
