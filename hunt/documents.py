"""Reads the actual documents for finalists (Gemini reads PDFs directly).

1. Key filings: warrants/preferential (who pays, at what price), orders (size,
   customer, duration), capex (amount, capacity, funding), commissioning,
   change in control.
2. Last 2-3 earnings-call transcripts: guidance policy, say-do record, candor,
   evasions, expensive signals, demand visibility.
Results are cached so the same PDF is never paid for twice.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, timedelta

from bot.config import DATA, env
from . import db
from .tags import DOC_TAGS

log = logging.getLogger(__name__)
PDF_DIR = DATA / "pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)
MAX_PDF = 15 * 1024 * 1024

BASE_RULE = ("Use ONLY this document. Quote numbers exactly as written; use null when something is not stated. "
             "Return ONE JSON object.")

FILING_PROMPTS = {
    "warrants_preferential": """This is an Indian listed company's filing about a preferential issue / convertible warrants ({company}).
Extract JSON keys: issue_type ("warrants"|"equity"|"convertible"|"other"), total_amount_cr, issue_price,
market_price_reference (if stated), premium_to_market_pct (positive = above market; compute only if both prices stated),
number_of_securities, allottees: list of {{name, category: "promoter"|"promoter_group"|"non_promoter_individual"|
"non_promoter_entity"|"institution", amount_cr}}, n_allottees, promoter_share_of_issue_pct, upfront_pct_paid,
purpose_of_issue, dilution_pct, red_flags: list of strings (many unrelated individuals, entities that look like
trading/operator firms, deep discount, purpose vague), expensive_signal: true if promoters put meaningful own money
at or above market price, else false. {rule}""",
    "order_win": """This is an Indian listed company's filing about an order / contract ({company}).
Extract JSON keys: customer, customer_type ("government"|"psu"|"private"|"export"|"unknown"), order_value_cr,
value_original_currency, scope, execution_period_months, domestic_or_export, country, repeat_customer (true/false/null),
notes. {rule}""",
    "capex_announced": """This is an Indian listed company's filing about capacity expansion / capex ({company}).
Extract JSON keys: capex_amount_cr, capacity_added (with units), existing_capacity (with units), product,
expected_commissioning, funding_mix (debt / internal accruals / equity), revenue_potential_cr or asset_turnover_stated,
demand_rationale (why they are building), location. {rule}""",
    "commissioning": """This is an Indian listed company's filing about commissioning / start of commercial production ({company}).
Extract JSON keys: what_was_commissioned, capacity (with units), date, expected_ramp_up, revenue_potential_cr,
utilisation_guidance, notes. {rule}""",
    "change_in_control": """This is an Indian listed company's filing about a change in control / open offer ({company}).
Extract JSON keys: acquirer, acquirer_profile, stake_acquired_pct, price_per_share, open_offer_size_pct,
open_offer_price, rationale, red_flags. {rule}""",
}

MGMT_PROMPT = """You are judging the MANAGEMENT of {company} (Indian listed company) from its {n} most recent
earnings-call transcripts, given oldest first. Be a sceptical analyst, not a promoter.
Return ONE JSON object with keys:
- "guidance_policy": "specific_numeric" | "directional_only" | "refuses", plus "guidance_policy_quote"
- "guidance_items": list of {{"call_date","metric","value","timeframe","quote"}}
- "say_do": list of {{"promise" (from an earlier call), "later_status": "met"|"missed"|"deferred"|"dropped_silently"|"too_early",
   "evidence"}}; "say_do_score": 0-10 (null if fewer than 2 calls or nothing checkable)
- "candor_score": 0-10; "upfront_bad_news": list of short quotes where management volunteered problems;
  "evasive_answers": list of {{"question","how_evaded"}}
- "expensive_signals": list of {{"signal","quote"}}: capex committed before demand is visible, promoters putting in
   their own money, specific guidance that would be costly to miss, turning down business to protect margins,
   disclosure beyond what is required
- "demand_visibility": order book, utilisation, pipeline (numbers where given)
- "capex_and_capacity": plans, timelines, utilisation, payback talk
- "tone_shift": one sentence on how confidence changed across the calls
- "red_flags": blaming externals repeatedly, KPI changes, guidance cuts, related-party hints, accounting changes
- "one_line_verdict"
Quotes under 20 words. Use only the transcripts."""


def _client():
    from google import genai
    return genai.Client(api_key=env("GEMINI_API_KEY"))


def _model():
    return env("GEMINI_DEEP_MODEL") or env("GEMINI_MODEL", "gemini-3.5-flash")


def fetch_pdf(http, url: str) -> bytes | None:
    if not url or not url.startswith("http"):
        return None
    path = PDF_DIR / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".pdf")
    if path.exists():
        return path.read_bytes()
    try:
        data = http.get(url, kind="bytes", referer="https://www.bseindia.com/", warm="https://www.bseindia.com/")
    except Exception as e:
        log.warning("pdf download failed %s: %s", url, e)
        return None
    if not data or not data[:5].startswith(b"%PDF") or len(data) > MAX_PDF:
        log.warning("not a usable PDF: %s", url)
        return None
    path.write_bytes(data)
    return data


def _ask(prompt: str, pdfs: list[tuple[str, bytes]]) -> dict:
    from google.genai import types
    parts: list = []
    for label, data in pdfs:
        parts += [label, types.Part.from_bytes(data=data, mime_type="application/pdf")]
    parts.append(prompt)
    client = _client()  # keep a reference: the SDK closes unreferenced clients
    resp = client.models.generate_content(
        model=_model(), contents=parts,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1))
    try:
        return json.loads(resp.text or "{}")
    except Exception:
        return {"unparsed": (resp.text or "")[:3000]}


def read_filing(http, tag: str, url: str, company: str) -> dict | None:
    key = f"filing:{tag}:{url}"
    cached = db.doc_get(key, max_age_days=3650)
    if cached is not None:
        return cached
    pdf = fetch_pdf(http, url)
    if pdf is None or tag not in FILING_PROMPTS:
        return None
    data = _ask(FILING_PROMPTS[tag].format(company=company, rule=BASE_RULE), [("FILING:", pdf)])
    data["_source"] = url
    db.doc_put(key, data)
    return data


def management_quality(http, company: str, transcript_urls: list[str]) -> dict | None:
    """transcript_urls newest first (Screener order)."""
    urls = [u for u in transcript_urls if u][:3]
    if not urls:
        return None
    key = "mgmt:" + "|".join(urls)
    cached = db.doc_get(key, max_age_days=120)
    if cached is not None:
        return cached
    pdfs = []
    for i, u in enumerate(reversed(urls)):  # oldest first for the model
        b = fetch_pdf(http, u)
        if b:
            pdfs.append((f"TRANSCRIPT {i + 1} of {len(urls)} (oldest first):", b))
    if not pdfs:
        return None
    data = _ask(MGMT_PROMPT.format(company=company, n=len(pdfs)), pdfs)
    data["_transcripts"] = list(reversed(urls))
    db.doc_put(key, data)
    return data


def pick_filings(slug: str, max_docs: int = 4) -> list[dict]:
    since = (date.today() - timedelta(days=270)).isoformat()
    rows = [r for r in db.filings_for(slug, since) if (r.get("url") or "").lower().endswith(".pdf")]
    chosen, used = [], set()
    for tag in DOC_TAGS:  # priority order
        cands = [r for r in rows if tag in json.loads(r["tags"] or "[]") and r["newsid"] not in used]
        if tag == "order_win":
            cands.sort(key=lambda r: r.get("order_cr") or 0, reverse=True)
        for r in cands[: (2 if tag == "order_win" else 1)]:
            chosen.append({"tag": tag, **r})
            used.add(r["newsid"])
        if len(chosen) >= max_docs:
            break
    return chosen[:max_docs]


def read_all(http, slug: str, name: str, transcript_urls: list[str]) -> dict:
    out = {"filings": [], "management": None}
    for f in pick_filings(slug):
        try:
            d = read_filing(http, f["tag"], f["url"], name)
        except Exception as e:
            log.warning("reading %s for %s failed: %s", f["tag"], slug, e)
            d = None
        if d:
            out["filings"].append({"tag": f["tag"], "date": f["dt"], "headline": f["headline"], "url": f["url"],
                                   "extracted": d})
    try:
        out["management"] = management_quality(http, name, transcript_urls)
    except Exception as e:
        log.warning("management read failed for %s: %s", slug, e)
    return out
