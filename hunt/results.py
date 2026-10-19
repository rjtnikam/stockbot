"""Quarterly results: who filed, and the numbers. Source: Screener (Results feed + company pages).

Each run:
 1. Read Screener's Results feed (/announcements/results/) since the last run.
 2. Claude Haiku reads each headline + Screener's one-line summary: growth if stated, tone.
 3. Open company pages within a page budget: your stocks first, then retries, then strong-looking results.
 4. From each page: latest quarter vs a year ago and vs last quarter; the quality gates (same page, free).
 5. Strong result + gates pass -> 'strong_result' signal (the scan sees it). Weak result -> warning.
 6. One clean Telegram message.
Screener can update a company page some hours after the filing: such companies are retried (max 3 runs).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from bot import ai, fmt, fundamentals, screener
from bot.config import REPORTS, env
from bot.fundamentals import _row
from bot.telegram import esc, link
from . import confirm, db, filings, gates

log = logging.getLogger(__name__)

FUTURE = re.compile(r"to consider|will be held|to be held|is scheduled|prior intimation|trading window|"
                    r"closure of|proposes? to", re.I)
DONE = re.compile(r"\bapproved\b|\bdeclared\b|\breported\b|\bposted\b|\brevenue\b|\bprofit\b|\bpat\b", re.I)


def is_notice(headline: str, summary: str) -> bool:
    """A notice of a future board meeting, not a result (Haiku double-checks the rest)."""
    return bool(FUTURE.search(f"{headline} {summary}") and not DONE.search(summary or ""))

PRESCREEN = """These are quarterly-result filings by Indian listed companies, with Screener's one-line summary.
For each line "i | company | headline | summary" return what the text STATES (null when not stated; never guess):
sales_yoy (fraction: 0.24 means +24%), profit_yoy (fraction), tone ("strong", "weak", "mixed" or "unclear"),
is_result (false if the line is only a notice of a future board meeting, not the result itself).
Return JSON: {{"items": [{{"i": 0, "sales_yoy": null, "profit_yoy": null, "tone": "unclear", "is_result": true}}]}}

LINES:
{lines}"""


# ------------------------------------------------------------------ dates
def expected_quarter(filed: date) -> str:
    """The quarter a filing on this date reports: the last quarter-end before it ('Sep 2026')."""
    y = filed.year
    ends = [date(y - 1, 12, 31), date(y, 3, 31), date(y, 6, 30), date(y, 9, 30), date(y, 12, 31)]
    q = max(e for e in ends if e < filed)
    return q.strftime("%b %Y")


def _qdate(label: str) -> date | None:
    try:
        return datetime.strptime(label.strip(), "%b %Y").date()
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ numbers from the company page
def quarter_numbers(company: dict) -> dict:
    q = (company.get("tables") or {}).get("quarters")
    if q is None or q.empty:
        return {}
    cols = list(q.columns)
    sales, pat = _row(q, "Sales", "Revenue"), _row(q, "Net Profit")
    opm, eps = _row(q, "OPM", "Financing Margin"), _row(q, "EPS")
    main = sales if sales is not None else pat
    if main is None:
        return {}
    filled = [i for i, v in enumerate(main.tolist()) if fmt.num(v) is not None]
    if not filled:
        return {}
    i = filled[-1]

    def at(s, k):
        return fmt.num(s.iloc[i - k]) if s is not None and i - k >= 0 else None

    def g(now, before):
        return None if now is None or before is None or before <= 0 else now / before - 1

    out = {"quarter": cols[i], "sales": at(sales, 0), "sales_prev_q": at(sales, 1), "sales_year_ago": at(sales, 4),
           "pat": at(pat, 0), "pat_prev_q": at(pat, 1), "pat_year_ago": at(pat, 4),
           "opm": at(opm, 0), "opm_year_ago": at(opm, 4), "eps": at(eps, 0), "eps_year_ago": at(eps, 4)}
    out["sales_yoy"], out["sales_qoq"] = g(out["sales"], out["sales_year_ago"]), g(out["sales"], out["sales_prev_q"])
    out["pat_yoy"], out["pat_qoq"] = g(out["pat"], out["pat_year_ago"]), g(out["pat"], out["pat_prev_q"])
    out["opm_change"] = (out["opm"] - out["opm_year_ago"]) if out["opm"] is not None and out["opm_year_ago"] is not None else None
    out["turnaround"] = bool(out["pat"] is not None and out["pat"] > 0 and out["pat_year_ago"] is not None and out["pat_year_ago"] <= 0)
    out["small_base"] = bool(out["pat_year_ago"] is not None and abs(out["pat_year_ago"]) < 1.0)
    return out


def classify(qn: dict, cfg: dict | None = None) -> str:
    rc = (cfg or {}).get("results") or {}
    s, p, o = qn.get("sales_yoy"), qn.get("pat_yoy"), qn.get("opm_change")
    if not qn:
        return "n/a"
    if qn.get("turnaround") and (s or 0) >= 0.10:
        return "strong"
    if (s is not None and s >= rc.get("strong_sales_yoy", 0.15) and p is not None and p >= rc.get("strong_profit_yoy", 0.20)
            and (o is None or o >= -1.0)):
        return "strong"
    pat, pya = qn.get("pat"), qn.get("pat_year_ago")
    if (s is not None and s < 0 and p is not None and p < 0) or (pat is not None and pat < 0 and (pya or 0) > 0) \
            or (p is not None and p <= -0.25):
        return "weak"
    return "mixed" if s is not None or p is not None else "n/a"


ICON = {"strong": "🟢", "weak": "🔴", "mixed": "🟡", "n/a": "⚪"}


# ------------------------------------------------------------------ the run
def _followed(watch: set[str]) -> set[str]:
    studied = set()
    cutoff = (date.today() - timedelta(days=180)).isoformat()
    for p in REPORTS.glob("*.md"):
        if p.name[:10] >= cutoff and len(p.name) > 14:
            studied.add(p.name[11:-3].upper())
    return {w.upper() for w in watch} | studied


def _known_mcap(slug: str) -> float | None:
    m = db.master_by_slug(slug) or {}
    if m.get("mcap"):
        return m["mcap"]
    d = db.doc_get(f"deep:{slug}", max_age_days=60) or {}
    return d.get("market_cap_cr")


def prescreen(items: list[dict]) -> dict[int, dict]:
    if not items:
        return {}
    out: dict[int, dict] = {}
    model = env("CLAUDE_FAST_MODEL", "claude-haiku-4-5-20251001")
    for start in range(0, len(items), 80):
        chunk = items[start:start + 80]
        lines = "\n".join(f"{start + j} | {it['name'][:50]} | {it['headline'][:150]} | {it['summary'][:300]}"
                          for j, it in enumerate(chunk))
        try:
            from bot.llm_util import parse_json
            txt = ai.claude("You extract facts from short texts. Never guess.", PRESCREEN.format(lines=lines),
                            model=model, max_tokens=6000)
            for x in parse_json(txt).get("items") or []:
                if isinstance(x, dict) and "i" in x:
                    out[int(x["i"])] = x
        except Exception as e:
            log.warning("results pre-screen failed: %s", e)
    return out


def _feed_items(br, start: date) -> list[dict]:
    rows = filings.fetch_screener(br, start, "results")
    items: dict[str, dict] = {}
    for r in rows:
        if is_notice(r["HEADLINE"], r["MORE"]):
            continue
        slug = r["SCRIP_CD"]
        filed = (r["NEWS_DT"] or date.today().isoformat())[:10]
        it = items.setdefault(slug, {"slug": slug, "name": r["SLONGNAME"], "filed": filed, "url": r["NSURL"],
                                     "headline": r["HEADLINE"], "summary": r["MORE"] or ""})
        if len(r["MORE"] or "") > len(it["summary"]):  # keep the richest summary (results + press release)
            it["summary"] = r["MORE"]
    return list(items.values())


def run(br, cfg: dict, watch: set[str], today: date | None = None, dry: bool = False) -> dict:
    today = today or date.today()
    rc = cfg.get("results") or {}
    budget = int(rc.get("max_pages_per_day", 30))
    lo, hi = cfg.get("hunt", {}).get("market_cap_cr", [100, 25000])
    last = db.get_state("last_results_date")
    start = max(date.fromisoformat(last), today - timedelta(days=4)) if last else today - timedelta(days=1)
    items = _feed_items(br, start)
    followed = _followed(watch)

    # new filings into the table; earlier pending ones come back for a retry
    fresh = []
    for it in items:
        if db.result_get(it["slug"], it["filed"]) is None:
            fresh.append({**it, "status": "listed", "tries": 0})
    pre = {} if dry else prescreen(fresh)
    for i, it in enumerate(fresh):
        p = pre.get(i) or {}
        if p.get("is_result") is False:
            it["status"] = "not a result"
        it["pre"] = {"tone": p.get("tone") or "unclear", "sales_yoy": p.get("sales_yoy"), "profit_yoy": p.get("profit_yoy")}
        it["data"] = {"pre": it["pre"]}
        db.result_put(it)
    pending = [r for r in db.results_since((today - timedelta(days=6)).isoformat(), "pending") if (r["tries"] or 0) < 3]
    for r in pending:
        r["pre"] = (r["data"] or {}).get("pre") or {}

    def rank(r):
        pre_ = r.get("pre") or {}
        growth = (fmt.num(pre_.get("sales_yoy")) or 0) + (fmt.num(pre_.get("profit_yoy")) or 0)
        tone = {"strong": 0, "unclear": 1, "mixed": 2, "weak": 3}.get(pre_.get("tone"), 1)
        return (0 if r["slug"] in followed else 1, 0 if r.get("status") == "pending" else 1, tone, -growth)

    todo = [r for r in fresh + pending if r.get("status") != "not a result"]
    todo.sort(key=rank)
    opened, rows = 0, []
    for r in todo:
        mine = r["slug"] in followed
        mc = _known_mcap(r["slug"])
        if not mine and mc is not None and not (lo <= mc <= hi):
            r["status"] = "out of band"
            db.result_put(r)
            continue
        if not mine and (r.get("pre") or {}).get("tone") == "weak":
            continue  # weak results of companies you do not follow: no page needed
        if opened >= budget:
            continue
        opened += 1
        try:
            comp = screener.fetch_company(br, r["slug"])
        except Exception as e:
            log.info("results: page for %s failed: %s", r["slug"], e)
            r["status"], r["tries"] = "pending", (r.get("tries") or 0) + 1
            db.result_put({**r, "data": {"pre": r.get("pre")}})
            continue
        page_mcap = fmt.num((comp.get("top_ratios") or {}).get("Market Cap"))
        if not mine and page_mcap is not None and not (lo <= page_mcap <= hi):
            r["status"] = "out of band"  # remembered: this company never costs a page again this season
            db.result_put({**r, "data": {"pre": r.get("pre"), "mcap": page_mcap}})
            db.doc_put(f"deep:{r['slug']}", {"market_cap_cr": page_mcap})
            continue
        qn = quarter_numbers(comp)
        want = expected_quarter(date.fromisoformat(r["filed"]))
        got, need = _qdate(qn.get("quarter") or ""), _qdate(want)
        if got and need and got < need and (r.get("tries") or 0) < 2:
            r["status"], r["tries"] = "pending", (r.get("tries") or 0) + 1  # Screener not updated yet
            db.result_put({**r, "quarter": want, "data": {"pre": r.get("pre")}})
            continue
        nums = fundamentals.build(comp, cfg)
        deep = confirm.deep_check(comp, nums, cfg)
        db.doc_put(f"deep:{r['slug']}", deep)
        kind = classify(qn, cfg)
        gate = deep.get("gate") or {}
        data = {"pre": r.get("pre"), "q": qn, "kind": kind, "gate": gate.get("status"),
                "gate_reasons": gates.plain_reasons(gate), "mcap": (comp.get("top_ratios") or {}).get("Market Cap"),
                "stale": bool(got and need and got < need), "name": comp.get("name") or r["name"]}
        r.update({"status": "done", "quarter": qn.get("quarter"), "data": data})
        db.result_put(r)
        rows.append(r)
        if not dry and kind == "strong" and gate.get("status") == "pass":
            db.add_signal(r["slug"], data["name"], "strong_result", 2.0, r["filed"],
                          detail={"note": _short_line(qn), "quarter": qn.get("quarter"), "change": True},
                          url=r.get("url"), ref=f"result:{qn.get('quarter')}")
        elif not dry and kind == "weak" and mine:
            db.add_signal(r["slug"], data["name"], "weak_result", -1.0, r["filed"],
                          detail={"note": _short_line(qn), "quarter": qn.get("quarter")},
                          url=r.get("url"), ref=f"result:{qn.get('quarter')}")
    db.set_state("last_results_date", today.isoformat())
    stats = {"filed": len(items), "new": len(fresh), "opened": opened,
             "pending": len(db.results_since((today - timedelta(days=6)).isoformat(), "pending")),
             "not_opened": len([r for r in todo if r.get("status") == "listed"])}
    digest = render_digest(rows, stats, followed, start, today) if (rows or fresh) else None
    return {"digest": digest, "stats": stats, "rows": rows}


# ------------------------------------------------------------------ messages
def _short_line(qn: dict) -> str:
    bits = [f"Sales {fmt.pct(qn.get('sales_yoy'))}", f"Profit {fmt.pct(qn.get('pat_yoy'))}"]
    if qn.get("opm") is not None:
        bits.append(f"Margin {fmt.pctv(qn.get('opm'))} ({fmt.pts(qn.get('opm_change'))})")
    if qn.get("turnaround"):
        bits.append("back to profit")
    return " · ".join(bits) + (f" · {qn.get('quarter')} quarter" if qn.get("quarter") else "")


def block(r: dict) -> str:
    """Full result block for one company (your stocks, /results SYM)."""
    d = r.get("data") or {}
    q = d.get("q") or {}
    name = d.get("name") or r.get("name") or r["slug"]
    lines = [f"{ICON.get(d.get('kind'), '⚪')} <b>{esc(name)}</b> ({esc(r['slug'])}) · {esc(q.get('quarter') or '?')} quarter"
             + (" <i>(Screener not updated yet)</i>" if d.get("stale") else "")]
    if q:
        lines.append(f"Sales {esc(fmt.cr(q.get('sales')))} ({esc(fmt.pct(q.get('sales_yoy')))} YoY, "
                     f"{esc(fmt.pct(q.get('sales_qoq')))} QoQ)")
        lines.append(f"Profit {esc(fmt.cr(q.get('pat')))} ({esc(fmt.pct(q.get('pat_yoy')))} YoY"
                     + (", back to profit" if q.get("turnaround") else "") + (", small base" if q.get("small_base") else "") + ")")
        if q.get("opm") is not None:
            lines.append(f"Margin {esc(fmt.pctv(q.get('opm')))} ({esc(fmt.pctv(q.get('opm_year_ago')))} a year ago)")
    if r.get("summary"):
        lines.append("<i>" + esc(r["summary"][:220]) + "</i>")
    if r.get("url"):
        lines.append(link(r["url"], "Result PDF"))
    return "\n".join(lines)


def render_digest(rows: list[dict], stats: dict, followed: set[str], start: date, today: date) -> str:
    span = f"{start:%d %b}" if start >= today - timedelta(days=1) else f"{start:%d %b}–{today:%d %b}"
    parts = [f"📑 <b>Quarterly results</b> · filed since {span}\n"
             f"{stats['filed']} companies filed · {stats['opened']} checked"
             + (f" · {stats['pending']} waiting for Screener" if stats.get("pending") else "")
             + (f" · {stats['not_opened']} not opened (page limit)" if stats.get("not_opened") else "")]
    mine = [r for r in rows if r["slug"] in followed]
    if mine:
        parts.append("<b>Stocks you follow</b>\n\n" + "\n\n".join(block(r) for r in mine))
    strong = sorted([r for r in rows if r["slug"] not in followed and (r["data"] or {}).get("kind") == "strong"],
                    key=lambda r: -((fmt.num(r["data"]["q"].get("sales_yoy")) or 0) + (fmt.num(r["data"]["q"].get("pat_yoy")) or 0)))
    good = [r for r in strong if r["data"].get("gate") == "pass"]
    flawed = [r for r in strong if r["data"].get("gate") != "pass"]
    if good:
        lines = ["🟢 <b>Strong results</b> (passed the quality checks)"]
        for i, r in enumerate(good[:10], 1):
            d = r["data"]
            mc = f" · {fmt.cr(d.get('mcap'))}" if d.get("mcap") else ""
            lines.append(f"{i}. <b>{esc(d.get('name') or r['name'])}</b>{esc(mc)}\n   {esc(_short_line(d['q']))} · "
                         + link(r.get("url"), "PDF"))
        parts.append("\n".join(lines))
    if flawed:
        lines = ["🟡 <b>Strong, but failed a quality check</b>"]
        for r in flawed[:6]:
            d = r["data"]
            why = "; ".join(d.get("gate_reasons") or []) or "quality check not passed"
            lines.append(f"• <b>{esc(d.get('name') or r['name'])}</b>: {esc(_short_line(d['q']))}\n   ⛔ {esc(why)}")
        parts.append("\n".join(lines))
    if not (mine or good or flawed):
        parts.append("No strong results among the companies checked today.")
    parts.append("/results — every company that filed · /results SYMBOL — one company")
    return "\n\n".join(parts)


def list_recent(days: int = 3) -> str:
    """/results without a symbol: every company that filed recently, with Screener's one-line summary."""
    rows = [r for r in db.results_since((date.today() - timedelta(days=days)).isoformat()) if r["status"] != "not a result"]
    if not rows:
        return "No quarterly results stored for the last few days."
    out = [f"📑 <b>Results filed in the last {days} days</b> ({len(rows)})"]
    for r in rows[:60]:
        d = r["data"] or {}
        kind = d.get("kind") or (d.get("pre") or {}).get("tone") or "n/a"
        line = f"{ICON.get(kind, '⚪')} <b>{esc(r['name'])}</b> ({esc(r['slug'])}) · {esc(r['filed'][5:])}"
        if d.get("q"):
            line += "\n   " + esc(_short_line(d["q"]))
        elif r.get("summary"):
            line += "\n   " + esc(r["summary"][:140])
        out.append(line)
    if len(rows) > 60:
        out.append(f"… and {len(rows) - 60} more.")
    return "\n".join(out)


def one(br, cfg: dict, sym: str) -> str:
    """/results SYMBOL: open the company page now and show its latest quarter."""
    slug = sym.upper()
    try:
        comp = screener.fetch_company(br, slug)
    except Exception:
        alt = screener.resolve_slug(br, sym)
        if not alt:
            return f"❓ {esc(sym)}: not found on Screener."
        slug, comp = alt, screener.fetch_company(br, alt)
    qn = quarter_numbers(comp)
    known = db.latest_result(slug) or {}
    r = {"slug": slug, "name": comp.get("name") or slug, "url": known.get("url"), "summary": known.get("summary"),
         "data": {"q": qn, "kind": classify(qn, cfg), "name": comp.get("name")}}
    return block(r)


def as_json(r: dict) -> str:
    return json.dumps(r, default=str)
