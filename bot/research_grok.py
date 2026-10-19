"""Grok bot: what X (Twitter) is saying, via xAI's server-side x_search tool.

No X login and no X API subscription needed: Grok searches X itself.
"""
from __future__ import annotations

import logging

from openai import OpenAI

from .config import env
from .llm_util import SOURCE_RULE, parse_json

log = logging.getLogger(__name__)


def x_research(name: str, symbol: str, lookback_days: int) -> dict:
    client = OpenAI(api_key=env("XAI_API_KEY"), base_url="https://api.x.ai/v1")
    prompt = f"""Search X (Twitter) for discussion of the Indian listed company {name} (ticker {symbol}) over the last {lookback_days} days.
Look for threads by investors, analysts and plant-visit / concall note-takers, not just price chatter.

Return JSON with keys:
- "mention_level": "none" | "low" | "medium" | "high"
- "mention_trend": "rising" | "flat" | "falling" (vs the {lookback_days} days before)
- "key_threads": up to 8 {{"author","date","summary","url"}}, highest-quality analysis first
- "bull_narratives": list of {{"point","url"}}
- "bear_narratives": list of {{"point","url"}}
- "pump_warning_signs": list of {{"sign","url"}} (coordinated promotion, tip accounts, target-price spam)
- "who_is_talking": "retail tip accounts" | "serious small-cap investors" | "institutions/analysts" | "mixed"
{SOURCE_RULE}"""
    resp = client.responses.create(
        model=env("GROK_MODEL", "grok-4.3"),
        input=[{"role": "user", "content": prompt}],
        tools=[{"type": "x_search"}],
    )
    data = parse_json(getattr(resp, "output_text", "") or "")
    cites = getattr(resp, "citations", None)
    if cites:
        data["_citations"] = list(cites)[:40]
    return data
