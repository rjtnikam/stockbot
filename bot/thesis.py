"""Claude's final decision on a stock: the full memo, written AFTER the council discussion.

write_thesis() refuses to run without a real discussion (council.require_discussion).
It returns (memo_markdown, card) — the card feeds the clean Telegram message.
"""
from __future__ import annotations

import json
import logging
import re

from . import ai
from .config import PHILOSOPHY_FILE
from .council import Discussion, require_discussion
from .llm_util import parse_json

log = logging.getLogger(__name__)

INSTRUCTIONS = """You are Rajat's research partner. You write the investment memo for ONE stock and you take
the final decision. You reason strictly against his philosophy (above).

HARD RULES
1. Every number you state must come from dossier.numbers, dossier.technicals, dossier.timeline
   or dossier.story_stage. Never compute new financial figures from memory or invent them.
2. Every external fact (news, X, ValuePickr, management, capex) must cite its URL inline like [source](url).
   If a claim has no URL, ignore it.
3. Probabilities: only quote convergence odds if dossier.story_stage.convergence.available is true.
   Otherwise say plainly that base rates are not built yet and give a qualitative view only.
4. Be a sparring partner, not a promoter. The bear case gets as much rigour as the bull case.
5. Flag Rajat's two known failure modes when relevant: exiting quality compounders too early on
   price anchors, and overstaying cyclicals.
6. If dossier.hunt exists, the stock was found by the automated scan. Test its reasons (dossier.hunt.why_flagged,
   setups) skeptically: say which signals hold up and which are noise.
7. Technicals: MarketSmith is the authority. Quote dossier.technicals.marketsmith first (RS rating, EPS rating,
   Master Score, A/D rating, industry group rank, latest base, lists, market direction, red flags). The bot's own
   metrics are secondary and must be labelled "derived". If marketsmith is absent, say so.
8. Management: use dossier.hunt.management_quality and filing_documents. If absent, say management was not
   assessed from transcripts. Never infer candor from price action.
9. THE DISCUSSION RULE. You chaired a discussion with GROK (X/Twitter search) and GEMINI (web search); the
   transcript follows the dossier. Your decision must come out of that discussion. Weigh every point each
   member made; do not ignore any. A member's fact counts only with a link. If your verdict differs from the
   majority of the members' votes, give the exact reason. If a member was absent, say so and lower your
   conviction if their evidence mattered.

WRITE THESE SECTIONS (markdown, concise, dense):
## Verdict
One line: PURSUE / WATCH / REJECT, conviction 1-5, and the single reason.
## 0. Why it came up
Only if dossier.hunt exists: each signal marked HOLDS UP / NOISE / UNVERIFIED with one reason.
## 1. The story
What the business is, what changed, why now. 5-8 sentences.
## 2. The numbers
Do the numbers confirm or contradict the story? Growth trend, ROTNA band, RR x ROIIC, cash conversion,
interest coverage, working-capital drift, shareholding, capex funding and conversion. Call out every mismatch.
## 2b. Management
Guidance policy, say-do record, candor vs evasions, expensive signals, warrant allottee quality.
Separate what management SAID from what they DID.
## 3. Timeline: where the story stands
Name the stage (Hidden / Inflection / Recognition / Consensus / Decay); agree or disagree with the rule-based
call. How price reacted to past results; what the technicals say now.
## 4. Price vs story: gap and convergence
Is price behind or ahead of the story, by how much? What event would force convergence? Odds per rule 3.
## 5. Bear case: what kills it
## 6. What the price implies
Reverse-DCF implied growth vs delivered growth.
## 7. Codex checklist
Table: rule | pass / fail / unknown | evidence.
## 8. What to verify yourself
3-5 specific checks with where to look.
## 9. The discussion
Each member's vote. Each point from GROK and GEMINI that you ACCEPTED or REJECTED, and why.

AT THE VERY END write the line CARD_JSON: followed by ONE JSON object (no code fence) for the phone summary.
Write its text in plain English: short sentences, active voice, no markdown, no jargon such as "hunt".
CARD_JSON: {"verdict": "PURSUE|WATCH|REJECT", "conviction": 1-5,
 "stage": "Hidden|Inflection|Recognition|Consensus|Decay", "price_vs_story": "price ahead of story|story ahead of price|aligned",
 "what_it_does": "max 12 words",
 "story": "3-4 short sentences: what changed in the business and why it matters, with 1-2 numbers",
 "where_it_stands": "1-2 sentences: the stage in plain words, and whether the price is ahead of or behind the business (use the implied vs delivered growth numbers)",
 "main_risk": "1 sentence",
 "watch_next": ["max 3 items, max 15 words each, with a date when known"],
 "decision_reason": "1-2 sentences: why this verdict after the discussion",
 "discussion": {"accepted": [{"from": "GROK|GEMINI", "point": "..."}],
                "rejected": [{"from": "GROK|GEMINI", "point": "...", "why": "..."}],
                "overruled_majority": false}}
"""

FIX_DISCUSSION = """You wrote the memo below but did not account for the discussion. Read the discussion and
return ONLY one JSON object: {{"decision_reason": "...", "discussion": {{"accepted": [{{"from": "GROK|GEMINI",
"point": "..."}}], "rejected": [{{"from": "GROK|GEMINI", "point": "...", "why": "..."}}], "overruled_majority": false}}}}

THE DISCUSSION:
{transcript}

YOUR MEMO'S VERDICT SECTION:
{verdict}"""


def extract_card(text: str) -> tuple[str, dict]:
    """Split 'memo ... CARD_JSON: {...}' into (memo, card). Tolerates fences, bold markers and line breaks."""
    text = text or ""
    i = text.rfind("CARD_JSON")
    if i == -1:  # older format
        i = text.rfind("META:")
    if i == -1:
        return text.strip(), {}
    tail = text[i:]
    start = tail.find("{")
    card: dict = {}
    if start != -1:
        try:
            card, _ = json.JSONDecoder().raw_decode(tail[start:])
        except Exception:
            card = parse_json(tail[start:])
            if "unparsed_text" in card:
                card = {}
    memo = text[:i]
    memo = re.sub(r"(\s|`|\*|#|-)+$", "", memo).rstrip()  # trailing ``` / ** / --- left by the model
    return memo, card if isinstance(card, dict) else {}


def verdict_from_memo(memo: str) -> dict:
    """Fallback when the card is missing: read the Verdict section."""
    m = re.search(r"##\s*Verdict\s*\n(.*?)(?=\n##\s|\Z)", memo or "", re.S | re.I)
    sec = m.group(1) if m else (memo or "")[:400]
    v = re.search(r"\b(PURSUE|WATCH|REJECT)\b", sec)
    c = re.search(r"conviction\D{0,10}([1-5])", sec, re.I)
    return {"verdict": v.group(1) if v else None, "conviction": int(c.group(1)) if c else None}


def _discussion_ok(card: dict, d: Discussion) -> bool:
    disc = card.get("discussion")
    if not isinstance(disc, dict):
        return False
    named = " ".join(str(x.get("from", "")) for x in (disc.get("accepted") or []) + (disc.get("rejected") or [])
                     if isinstance(x, dict)).upper()
    return any(m.upper() in named for m in d.others_spoke())


def write_thesis(dossier: dict, discussion: Discussion) -> tuple[str, dict]:
    require_discussion(discussion)  # the rule: no decision without the council
    philosophy = PHILOSOPHY_FILE.read_text(encoding="utf-8")
    system = f"# RAJAT'S INVESTING PHILOSOPHY\n\n{philosophy}\n\n# YOUR JOB\n\n{INSTRUCTIONS}"
    content = ("DOSSIER (JSON):\n" + json.dumps(dossier, default=str, ensure_ascii=False)[:330_000] +
               "\n\nTHE DISCUSSION (transcript, in order):\n" + discussion.transcript())
    text = ai.claude(system, content, model=ai.thesis_model(), max_tokens=7000)
    memo, card = extract_card(text)
    for k, v in verdict_from_memo(memo).items():
        if not card.get(k) and v is not None:
            card[k] = v
    if not _discussion_ok(card, discussion):  # one cheap retry: transcript + verdict only, no dossier
        sec = re.search(r"##\s*Verdict\s*\n(.*?)(?=\n##\s|\Z)", memo, re.S | re.I)
        try:
            fix = parse_json(ai.claude(system, FIX_DISCUSSION.format(transcript=discussion.transcript(),
                                                                     verdict=(sec.group(1) if sec else memo[:600])),
                                       model=ai.thesis_model(), max_tokens=1500))
            if isinstance(fix.get("discussion"), dict):
                card["discussion"] = fix["discussion"]
                card["decision_reason"] = card.get("decision_reason") or fix.get("decision_reason")
        except Exception as e:
            log.warning("discussion fix-up failed: %s", e)
    card["discussion_checked"] = _discussion_ok(card, discussion)
    return memo, card
