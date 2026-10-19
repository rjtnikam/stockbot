"""Helpers shared by the research bots."""
from __future__ import annotations

import json
import re


def parse_json(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates ```json fences)."""
    if not text:
        return {}
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = m.group(1) if m else None
    if blob is None:
        start, end = text.find("{"), text.rfind("}")
        blob = text[start:end + 1] if start != -1 and end > start else ""
    try:
        return json.loads(blob)
    except Exception:
        return {"unparsed_text": text[:6000]}


SOURCE_RULE = (
    "STRICT RULES: Every factual claim must carry the URL it came from in a 'url' field. "
    "If you cannot find a source for something, leave it out. Do not guess numbers. "
    "Reply with ONE JSON object only."
)
