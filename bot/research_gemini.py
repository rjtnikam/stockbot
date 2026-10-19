"""Gemini bot: Google research + news (grounded in live search) and ValuePickr digest."""
from __future__ import annotations

import json
import logging

from google import genai
from google.genai import types

from .config import env
from .llm_util import SOURCE_RULE, parse_json

log = logging.getLogger(__name__)


def _client():
    return genai.Client(api_key=env("GEMINI_API_KEY"))


def _sources(resp) -> list[dict]:
    out = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (gm.grounding_chunks or []):
            if ch.web:
                out.append({"title": ch.web.title, "url": ch.web.uri})
    except Exception:
        pass
    return out


def web_research(name: str, symbol: str, lookback_days: int) -> dict:
    prompt = f"""You are an equity research assistant covering Indian listed companies.
Company: {name} (NSE/BSE: {symbol}).
Search the web (news, exchange filings, company website, annual report, concall coverage, credit rating notes).

Return JSON with keys:
- "what_the_company_does": 2-3 sentences
- "story_now": what has changed in the business in the last 2 years and why it matters (the narrative bulls tell)
- "recent_news": list of {{"date","headline","summary","url"}} from the last {lookback_days} days, newest first
- "management_and_governance": list of {{"claim","url"}} (capital allocation, related parties, auditor changes, pledges, rating actions)
- "capacity_and_guidance": list of {{"claim","expected_date","url"}} (capex, commissioning, order book, guidance)
- "upcoming_catalysts": list of {{"event","expected_date","url"}}
- "red_flags": list of {{"claim","url"}}
- "coverage_level": "none" | "low" | "medium" | "high" (how many brokers/media cover it)
{SOURCE_RULE}"""
    client = _client()  # keep a reference: the SDK closes unreferenced clients
    resp = client.models.generate_content(
        model=env("GEMINI_MODEL", "gemini-3.5-flash"),
        contents=prompt,
        config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())],
                                           temperature=0.2),
    )
    data = parse_json(resp.text or "")
    data["_grounding_sources"] = _sources(resp)
    return data


def digest_valuepickr(name: str, vp: dict) -> dict:
    if not vp.get("found"):
        return {"found": False}
    posts = json.dumps(vp["posts"], ensure_ascii=False)[:120_000]
    prompt = f"""Below are the most recent posts from the ValuePickr forum thread on {name}.
Summarise what serious investors there think. Return JSON with keys:
- "bull_points": list of {{"point","url"}}
- "bear_points": list of {{"point","url"}}
- "best_insights": up to 5 {{"insight","author","url"}} (original analysis, plant visits, concall notes, scuttlebutt)
- "sentiment_shift": how the tone changed from older to newer posts, in one sentence
- "questions_unanswered": open questions members keep raising
Use only the post URLs given. {SOURCE_RULE}

POSTS:
{posts}"""
    client = _client()  # keep a reference: the SDK closes unreferenced clients
    resp = client.models.generate_content(
        model=env("GEMINI_MODEL", "gemini-3.5-flash"), contents=prompt,
        config=types.GenerateContentConfig(temperature=0.2))
    data = parse_json(resp.text or "")
    data["thread_url"], data["attention"] = vp.get("url"), vp.get("attention")
    return data
