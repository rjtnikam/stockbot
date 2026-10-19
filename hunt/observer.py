"""The evening filing observer: at day end it reads all of today's exchange filings and sends the important ones.

Who decides what is important:
  1. Rules (free): junk filings (trading window, ads, compliance, notices) are dropped.
  2. Claude Haiku (the reader) reads every remaining filing and scores it 0-10 with a reason.
     It is the same reader as the morning scan, so no filing is paid for twice.
  3. Claude Sonnet (the editor) reads the best-scored filings with your philosophy and picks max 10:
     red flags first, then real business changes, then news on the stocks you follow.
  4. Grok checks X for the top 3 business changes: does anybody talk about it yet?
  5. Free quality checks (Screener company page) for the business changes.
The observer only ranks news. It takes no investment decision: a business change that passes the quality
checks goes into the next morning's scan, where the council (Claude, Grok, Gemini) discusses it.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from bot import ai, fmt
from bot.config import PHILOSOPHY_FILE, REPORTS
from bot.llm_util import parse_json
from bot.telegram import esc, link
from . import db, filings, gates
from .http import Http
from .tags import info

log = logging.getLogger(__name__)
SECTIONS = {"red": "🔴 <b>Red flags</b>", "story": "🟢 <b>Business changes</b>", "follow": "👀 <b>Stocks you follow</b>"}

EDITOR_ROLE = """# YOUR ROLE
You are the EDITOR of Rajat's evening filing report. A first reader (Claude Haiku) scored today's exchange
filings. You decide which ones Rajat must read today. You rank news; you do not give buy or sell advice."""

EDITOR_TASK = """These are today's exchange filings by Indian listed companies that the first reader marked as worth a look.
Pick the ones Rajat must read today: max {n}. You decide; the reader's score is only a hint.

Rules:
- Red flags first (auditor exit, default, pledge, fraud, regulator action, key people leaving), most of all on
  stocks he follows.
- Then real business changes that could change the company's value. Size matters: compare an order or a capex
  with the company's yearly sales or market cap when the line gives them, and say the ratio.
- Prefer small and mid caps (market cap ₹100-25,000 Cr). Skip routine items, even if the reader scored them high.
- A real news item on a stock he follows (follows: YES) goes in section "follow".
- Plain English: short sentences, active voice. Use only numbers from the line. Never invent numbers.

Return ONE JSON object:
{{"picks": [{{"i": 0, "section": "red or story or follow", "score": 1-10,
  "what": "max 20 words: what happened, with the number", "why": "max 20 words: why it matters"}}],
  "summary": "one sentence about the day's filings"}}

FILINGS:
{lines}"""

X_TASK = """For each Indian listed company below, search X (Twitter) posts from the last 7 days about this news.
In max 15 words per company, say if people talk about it and who (retail tip accounts, serious investors, analysts).
If you find nothing, write "no posts found". Never invent posts.
Return ONE JSON object: {{"notes": [{{"i": 0, "x": "..."}}]}}

COMPANIES:
{lines}"""


def _followed(watch: set[str]) -> set[str]:
    from .results import _followed as f
    return f(watch)


def _deep(slug: str) -> dict:
    return db.doc_get(f"deep:{slug}", max_age_days=7) or {}


def candidates(since: str, followed: set[str], min_score: int, limit: int = 40) -> list[dict]:
    out = []
    for r in db.filings_scored_since(since):
        if db.observer_was_sent(r["newsid"]):
            continue
        tags = r["tags"]
        red = any(info(t)["kind"] == "red" for t in tags)
        story = any(info(t)["kind"] == "pos" for t in tags)
        mine = (r["slug"] or "").upper() in followed or (r["code"] or "").upper() in followed
        imp = r["importance"]
        if imp is None:  # the reader did not run (test run or reader error): fall back on the tags
            imp_used = 8 if red else 6 if (story and r.get("change") != 0) else 2
        else:
            imp_used = imp
        if imp_used >= min_score or (red and mine) or (mine and imp_used >= 3):
            out.append({**r, "red": red, "mine": mine, "imp": imp_used})
    out.sort(key=lambda c: (-c["imp"], not c["red"], not c["mine"]))
    return out[:limit]


def edit(cands: list[dict], n: int) -> tuple[list[dict], str]:
    """The editor (Claude, chat model) picks max n. Falls back on the reader's ranking if the editor fails."""
    lines = []
    for i, c in enumerate(cands):
        d = _deep(c["slug"] or "")
        mc, ttm = d.get("market_cap_cr") or c.get("mcap"), d.get("ttm_sales_cr")
        lines.append(f"{i} | {c['name']} ({c['slug']}) | mcap {fmt.cr(mc) if mc else 'unknown'} | yearly sales "
                     f"{fmt.cr(ttm) if ttm else 'unknown'} | follows: {'YES' if c['mine'] else 'no'} | tags: "
                     f"{', '.join(c['tags']) or '-'} | {c['headline'][:160]} | {(c['more'] or '')[:300]} | reader: "
                     f"{c['imp']}/10 {c.get('why') or ''}")
    try:
        phil = PHILOSOPHY_FILE.read_text(encoding="utf-8")[:12000] if PHILOSOPHY_FILE.exists() else ""
        out = parse_json(ai.claude(f"# RAJAT'S INVESTING PHILOSOPHY\n{phil}\n\n{EDITOR_ROLE}",
                                   EDITOR_TASK.format(n=n, lines="\n".join(lines)), max_tokens=3000))
        picks = []
        for p in out.get("picks") or []:
            try:
                i = int(p["i"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 <= i < len(cands) and all(i != q["i"] for q in picks):
                sec = p.get("section") if p.get("section") in SECTIONS else ("red" if cands[i]["red"] else "story")
                try:
                    score = max(1, min(10, int(p.get("score") or cands[i]["imp"])))
                except (TypeError, ValueError):
                    score = cands[i]["imp"]
                picks.append({"i": i, "section": sec, "score": score,
                              "what": str(p.get("what") or cands[i].get("note") or cands[i]["headline"])[:220],
                              "why": str(p.get("why") or cands[i].get("why") or "")[:220]})
        if picks or out.get("picks") == []:
            return picks[:n], str(out.get("summary") or "")[:300]
    except Exception as e:
        log.warning("filing editor failed, using the reader's ranking: %s", e)
    picks = [{"i": i, "section": "red" if c["red"] else "follow" if c["mine"] else "story", "score": c["imp"],
              "what": c.get("note") or c["headline"], "why": c.get("why") or ""} for i, c in enumerate(cands[:n])]
    return picks, ""


def x_check(cands: list[dict], picks: list[dict], k: int = 3) -> dict[int, str]:
    top = [p for p in picks if p["section"] == "story"][:k]
    if not top:
        return {}
    lines = "\n".join(f"{p['i']} | {cands[p['i']]['name']} ({cands[p['i']]['slug']}) | {p['what']}" for p in top)
    try:
        out = parse_json(ai.grok("You search X (Twitter) live. Never invent posts. Plain text in JSON values.",
                                 X_TASK.format(lines=lines)))
        return {int(x["i"]): str(x.get("x") or "")[:160] for x in out.get("notes") or [] if isinstance(x, dict) and "i" in x}
    except Exception as e:
        log.warning("Grok X check failed: %s", e)
        return {}


def quality(br, cfg: dict, cands: list[dict], picks: list[dict], budget: int) -> dict[int, dict]:
    """Free quality checks for the business changes: 7-day cache first, then Screener pages within the budget."""
    from . import runner
    out, opened = {}, 0
    for p in [p for p in picks if p["section"] in ("story", "follow")]:
        c = cands[p["i"]]
        d = _deep(c["slug"] or "")
        if not d.get("gate") and br is not None and opened < budget:
            opened += 1
            got = runner._deep(br, cfg, {"slug": c["slug"], "name": c["name"]})
            d = got[0] if got else {}
        if d.get("gate"):
            out[p["i"]] = d["gate"]
    return out


def run(br, cfg: dict, watch: set[str], today: date | None = None, dry: bool = False) -> dict:
    today = today or date.today()
    oc = cfg.get("observer") or {}
    stats = filings.ingest(Http(br), cfg, watch, dry=dry, today=today)  # reads + scores today's new filings
    since = db.get_state("last_observer_date") or (today - timedelta(days=1)).isoformat()
    followed = _followed(watch)
    cands = candidates(since, followed, int(oc.get("min_importance", 6)))
    picks, summary = edit(cands, int(oc.get("max_picks", 10))) if (cands and not dry) else \
        ([{"i": i, "section": "red" if c["red"] else "follow" if c["mine"] else "story", "score": c["imp"],
           "what": c.get("note") or c["headline"], "why": c.get("why") or ""} for i, c in enumerate(cands[:10])], "")
    xs = x_check(cands, picks) if (picks and not dry and oc.get("x_check", True)) else {}
    gq = quality(br, cfg, cands, picks, int(oc.get("max_pages", 8))) if picks else {}
    scored = sum(1 for r in db.filings_scored_since(since) if r["importance"] is not None)
    digest = render(today, stats, scored, cands, picks, summary, xs, gq)
    db.observer_sent([c["newsid"] for c in cands])
    db.set_state("last_observer_date", today.isoformat())
    (REPORTS / f"{today}_filings.txt").write_text(digest, encoding="utf-8")
    return {"digest": digest, "picks": picks, "candidates": cands, "stats": stats}


def render(today: date, stats: dict, scored: int, cands: list[dict], picks: list[dict], summary: str,
           xs: dict[int, str], gq: dict[int, dict]) -> str:
    head = (f"🌙 <b>Today's important filings</b> · {today:%a %d %b}\n"
            f"Read {stats.get('fetched', 0)} filings ({stats.get('relevant', 0)} new, junk removed) · {scored} scored by "
            f"the reader · {len(picks)} picked by the editor")
    if summary:
        head += f"\n<i>{esc(summary)}</i>"
    parts = [head]
    if not picks:
        parts.append("Nothing important today.")
    n, next_scan = 0, []
    for sec in ("red", "story", "follow"):
        items = [p for p in picks if p["section"] == sec]
        if not items:
            continue
        lines = [SECTIONS[sec]]
        for p in items:
            n += 1
            c = cands[p["i"]]
            g = gq.get(p["i"])
            gate = ""
            if g:
                reasons = gates.plain_reasons(g, 1)
                gate = " · ✅ quality checks" if g.get("status") == "pass" else \
                    f" · {'⛔' if g.get('status') == 'fail' else '👀'} {esc(reasons[0] if reasons else g.get('status'))}"
                if g.get("status") == "pass" and sec == "story":
                    next_scan.append(c["name"])
            block = (f"{n}. <b>{esc(c['name'])}</b> ({esc(c['slug'])}) · {p['score']}/10{gate}\n"
                     f"   {esc(p['what'])}")
            if p.get("why"):
                block += f"\n   <i>Why: {esc(p['why'])}</i>"
            if p["i"] in xs:
                block += f"\n   🐦 X: {esc(xs[p['i']])}"
            if c.get("url"):
                block += " · " + link(c["url"], "PDF")
            lines.append(block)
        parts.append("\n".join(lines))
    if next_scan:
        parts.append("➡️ Passed the quality checks: " + esc(", ".join(next_scan)) +
                     ". Tomorrow's scan ranks them; the council studies the best ones.")
    parts.append("/filings SYMBOL — all filings of one company")
    return "\n\n".join(parts)
