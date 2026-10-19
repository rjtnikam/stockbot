"""Orchestrates the daily hunt and builds the hunt board."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from bot import access, fundamentals, screener
from bot.config import REPORTS
from bot.telegram import esc, link
from . import db, documents, filings, numbers_scan, results, scoring, universe
from .http import Http
from .tags import info as tag_info

log = logging.getLogger(__name__)


@dataclass
class HuntResult:
    board: str
    finalists: list[dict] = field(default_factory=list)   # ranked, with deep-check + documents attached
    fresh_slugs: set = field(default_factory=set)          # companies with new signals today
    red_alerts: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    results_digest: str | None = None                      # quarterly results message (None = nothing filed)


def _fetch_company(br, slug: str, name: str):
    try:
        return slug, screener.fetch_company(br, slug)
    except Exception as e:
        log.info("screener page for %s failed (%s); trying search", slug, e)
    alt = screener.resolve_slug(br, name or slug)
    if alt and alt != slug:
        try:
            return alt, screener.fetch_company(br, alt)
        except Exception as e:
            log.warning("screener page for %s/%s failed: %s", slug, alt, e)
    return slug, None


def _deep(br, cfg: dict, r: dict):
    """Screener company page -> deep-check metrics + quality gates. Cached for the week."""
    from hunt import confirm as confirm_mod
    slug, comp = _fetch_company(br, r["slug"], r["name"])
    if comp is None:
        return None
    nums = fundamentals.build(comp, cfg)
    c = confirm_mod.deep_check(comp, nums, cfg)
    db.doc_put(f"deep:{r['slug']}", c)
    return c, (slug, comp, nums)


def run(br, cfg: dict, watchlist: list[str], dry: bool = False, screener_ok: bool = True,
        today: date | None = None, force_numbers: bool = False, logins: dict | None = None) -> HuntResult:
    today = today or date.today()
    h = cfg["hunt"]
    watch = set(watchlist)
    http = Http(br, delay=tuple(h.get("api_delay_seconds", [1.0, 2.5])))
    stats: dict = {"problems": [], "logins": logins or {"screener": screener_ok}}

    # 1. who is who
    try:
        stats["universe"] = universe.refresh(http)
    except Exception as e:
        log.info("BSE/NSE security master unavailable (optional): %s", e)

    # 2. filings net
    try:
        stats["filings"] = filings.ingest(http, cfg, watch, dry=dry, today=today)
    except Exception as e:
        log.exception("filings failed")
        stats["problems"].append(f"BSE filings: {e}")

    # 2b. quarterly results (Screener Results feed + company pages); strong results become signals
    res_digest = None
    if (cfg.get("results") or {}).get("enabled", True) and screener_ok:
        try:
            rr = results.run(br, cfg, watch, today=today, dry=dry)
            stats["results"], res_digest = rr["stats"], rr["digest"]
        except Exception as e:
            log.exception("results failed")
            stats["problems"].append(f"Quarterly results: {str(e)[:160]}")

    # 3. numbers net: the whole-market Screener scan runs once a week (second trigger)
    every = int(h.get("numbers_every_days", 7))
    last_scan = db.get_state("last_numbers_scan")
    due = force_numbers or not last_scan or (today - date.fromisoformat(last_scan)).days >= every
    stats["numbers_scan"] = "ran today" if due else f"weekly (last {last_scan})"
    if due and screener_ok:
        res = numbers_scan.run_all(br, cfg, today=today)
        stats["queries"] = {k: (len(v["rows"]) if not v["error"] else f"ERROR: {v['error'][:120]}") for k, v in res.items()}
        stats["problems"] += [f"Screener query '{k}': {v['error'][:160]}" for k, v in res.items() if v["error"]]
        if not any(v["error"] for v in res.values()):
            db.set_state("last_numbers_scan", today.isoformat())
    elif due:
        stats["problems"].append("Screener logged out: numbers scan skipped today")

    # 4. first ranking
    ranked = scoring.score_all(cfg, today, watch)
    fresh = {s["slug"] for s in db.signals_since(today.isoformat())  # meaningful new filings today
             if s["run_date"] == today.isoformat() and not s["signal"].startswith("num:")
             and tag_info(s["signal"])["kind"] != "info"}

    # 5. deep check + QUALITY GATES on the shortlist (free: Screener company page, cached 7 days)
    #    Story filings first (the main trigger), then the best numbers-only names.
    n_deep = int(h.get("deep_confirm_top", 20))
    min_deep, min_final = float(h.get("min_score_deep_check", 1.0)), float(h.get("min_score_finalist", 2.5))
    live = [r for r in ranked if not r["excluded"] and r["score"] >= min_deep]
    short = [r for r in live if r["story_filings"]][:n_deep]
    short += [r for r in live if r not in short][: max(0, n_deep - len(short))]
    short += [r for r in ranked if r["watch"] and r["slug"] in fresh and r not in short]
    confirm, companies = {}, {}
    stats["pages_read"] = 0
    if screener_ok:
        for r in short:
            cached = db.doc_get(f"deep:{r['slug']}", max_age_days=int(h.get("gate_cache_days", 7)))
            if cached is not None:
                confirm[r["slug"]] = cached
                continue
            got = _deep(br, cfg, r)
            stats["pages_read"] += 1
            if got:
                confirm[r["slug"]], companies[r["slug"]] = got
    ranked = scoring.score_all(cfg, today, watch, confirm=confirm)
    stats["gates"] = {k: sum(1 for c in confirm.values() if (c.get("gate") or {}).get("status") == k)
                      for k in ("pass", "watch", "fail")}

    def eligible(r):  # may get documents read + a thesis
        return (not r["excluded"] and r["slug"] in confirm and r["gate"] == "pass" and r["score"] >= min_final
                and (r["story_filings"] or r["setups"]))

    # 6. documents for finalists
    docs = {}
    n_docs = int(h.get("documents_top", 8))
    finalists = [r for r in ranked if eligible(r)][:n_docs]
    if not dry:
        for r in finalists:
            docs[r["slug"]] = documents.read_all(http, r["slug"], r["name"], confirm[r["slug"]].get("transcripts", []))
        ranked = scoring.score_all(cfg, today, watch, confirm=confirm, docs=docs)

    db.save_scores(today.isoformat(), {r["slug"]: r["score"] for r in ranked[:300]})

    # 7. attach details for the thesis stage
    final = []
    for r in [x for x in ranked if eligible(x)][:n_docs]:
        if r["slug"] not in companies:  # gate result came from the cache: read the page once for the thesis
            got = _deep(br, cfg, r)
            if not got:
                continue
            companies[r["slug"]] = got[1]
        slug_used, comp, nums = companies[r["slug"]]
        final.append({**r, "screener_slug": slug_used, "company": comp, "numbers": nums,
                      "deep_check": confirm[r["slug"]], "documents": docs.get(r["slug"])})

    red = [r for r in ranked if r["red_flags"] and (r["watch"] or r["slug"] in fresh)
           and any(f["date"] >= (today - timedelta(days=3)).isoformat() for f in r["red_flags"])]
    stats["access"] = access.summary(run=access.RUN["id"])
    board = render_board(ranked, today, stats, fresh, red)
    (REPORTS / f"{today}_hunt_board.txt").write_text(board, encoding="utf-8")
    return HuntResult(board=board, finalists=final, fresh_slugs=fresh, red_alerts=red, stats=stats,
                      results_digest=res_digest)


def _delta(slug: str, score: float, today: date) -> str:
    prev = db.score_on_or_before(slug, (today - timedelta(days=7)).isoformat())
    if prev is None:
        return "NEW"
    d = score - prev
    return f"▲{d:.1f}" if d > 0.05 else f"▼{abs(d):.1f}" if d < -0.05 else "="


SETUP_PLAIN = {
    "Capex → revenue conversion": "New capacity is turning into sales",
    "Capex digestion (built, not yet in revenue)": "New capacity built; sales not there yet",
    "Earnings inflection": "Profit growth is speeding up",
    "Coming-out party: first concall + numbers moving": "First-ever earnings call, and the numbers are moving",
    "Skin in the game": "Promoters are putting in their own money",
    "New growth vector + numbers moving": "New product or market, and the numbers are moving",
    "Turnaround": "Back to profit",
}
NUM_PLAIN = {
    "revenue jump": "Sales jumped in the latest quarter", "eps acceleration": "EPS growth is speeding up",
    "margin expansion": "Margins are widening", "profit inflection": "Profit jumped",
    "turnaround": "Back to profit", "capex ahead of demand": "Big capex under construction",
    "gross block expansion": "Plant and machinery up 30%+", "promoter buying": "Promoters bought shares",
}


def plain_label(label: str) -> str:
    if label.startswith("SETUP: "):
        x = label[7:]
        m = re.match(r"Material order \((\d+%) of revenue\)", x)
        return f"Big order: {m.group(1)} of yearly sales" if m else SETUP_PLAIN.get(x, x)
    if label.startswith("numbers: "):
        return NUM_PLAIN.get(label[9:], label[9:])
    label = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)$", "", label).replace("confirmed: ", "")
    return label[:1].upper() + label[1:]


def _reasons(r: dict, n: int = 2) -> str:
    out = []
    for c in sorted([c for c in r["components"] if c["points"] > 0], key=lambda c: (c["kind"] != "setup", -c["points"])):
        p = plain_label(c["label"])
        if p not in out:
            out.append(p)
        if len(out) >= n:
            break
    return " · ".join(out)


def _d(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%d %b")
    except (TypeError, ValueError):
        return iso or ""


def access_line(stats: dict) -> str:
    """'🔐 Screener ✅ logged in · 46 pages read · MarketSmith ✅ logged in' — proof the bot read the sites today."""
    logins, acc = stats.get("logins") or {}, stats.get("access") or {}
    bits = []
    for site, name in (("screener", "Screener"), ("marketsmith", "MarketSmith")):
        a = acc.get(site) or {}
        ok = logins.get(site)
        status = "✅ logged in" if ok else "❌ LOGGED OUT" if ok is False else "❔"
        n = a.get("pages", 0)
        pages = (f" · {n} page{'s' if n != 1 else ''} read" + (f", {a['failed']} failed" if a.get("failed") else "")) if a else ""
        bits.append(f"{name} {status}{pages}")
    return "🔐 " + " · ".join(bits) + " · /access"


def render_board(ranked: list[dict], today: date, stats: dict, fresh: set, red: list[dict], top: int = 6) -> str:
    """Today's scan, in plain words (Telegram HTML)."""
    f = stats.get("filings") or {}
    changes = f.get("changes")
    checked = (stats.get("gates") or {})
    n_checked = sum(checked.values()) if checked else 0
    head = [f"🔎 <b>Today's scan</b> · {today:%a %d %b}",
            f"Read {f.get('fetched', 0)} filings" + (f" · {changes} real business changes" if changes is not None else "")
            + f" · {n_checked} companies checked"]
    head.append(access_line(stats))
    if stats.get("numbers_scan") == "ran today":
        q = stats.get("queries") or {}
        head.append(f"Weekly market scan ran today ({sum(1 for v in q.values() if not str(v).startswith('ERROR'))}"
                    f"/{len(q)} searches worked)")
    parts = ["\n".join(head)]

    passed = [r for r in ranked if not r["excluded"] and r.get("gate") == "pass" and r["score"] > 0][:top]
    lines = ["✅ <b>Passed the quality checks</b>"]
    for i, r in enumerate(passed, 1):
        mcap = (" · " + esc("₹{:,.0f} Cr".format(r["mcap"]))) if r.get("mcap") else ""
        lines.append(f"{i}. <b>{esc(r['name'])}</b> ({esc(r['slug'])}){mcap}\n   {esc(_reasons(r))}")
    if not passed:
        lines.append("No company passed the quality checks today.")
    parts.append("\n".join(lines))

    watch_lane = [r for r in ranked if not r["excluded"] and r.get("gate") == "watch" and r["score"] > 0][:5]
    if watch_lane:
        parts.append("👀 <b>Good story, one weak point</b> (no deep study)\n" + "\n".join(
            f"• <b>{esc(r['name'])}</b>: {esc('; '.join(r.get('gate_reasons') or []) or 'one check failed')}"
            for r in watch_lane))
    stopped = [r for r in ranked if r["excluded"] and r.get("gate") == "fail"][:6]
    if stopped:
        parts.append("⛔ <b>Stopped by the quality checks</b>\n" + "\n".join(
            f"• <b>{esc(r['name'])}</b>: {esc('; '.join(r.get('gate_reasons') or []) or 'failed a hard check')}"
            for r in stopped))
    if red:
        lines = ["🪳 <b>Warnings</b> (stocks you follow / new filings)"]
        for r in red[:8]:
            fl = r["red_flags"][0]
            lines.append(f"• <b>{esc(r['name'])}</b>: {esc(fl['label'])} · {esc(_d(fl['date']))}"
                         + (f" · {link(fl['url'], 'PDF')}" if fl.get("url") else ""))
        parts.append("\n".join(lines))
    warned = {r["slug"] for r in red[:8]}
    killed = [r for r in ranked if r["excluded"] and r.get("gate") != "fail" and (r["slug"] in fresh or r["watch"])
              and r["slug"] not in warned]
    if killed:
        parts.append("❌ <b>Removed (serious filing)</b>\n" + "\n".join(
            f"• <b>{esc(r['name'])}</b>: {esc('; '.join(r['kill_reasons']))}" for r in killed[:6]))
    if stats.get("problems"):
        probs = [p if len(p) <= 120 else p[:119] + "…" for p in stats["problems"][:4]]
        parts.append("⚙️ <i>Data problems: " + esc(" · ".join(probs)) + "</i>")
    return "\n\n".join(parts)


def calibrate(br, cfg: dict) -> str:
    """One-time / on-demand health check of every hunting source."""
    http = Http(br)
    rep = ["HUNT CALIBRATION"]
    try:
        u = universe.refresh(http, force=True)
        rep.append(f"✅ security master: {u['master']} BSE companies, {u.get('nse_symbols', 0)} NSE symbols mapped")
    except Exception as e:
        rep.append(f"➖ BSE security master (optional, market caps): {str(e)[:80]}")
    try:
        rows = filings.fetch_screener(br, date.today() - timedelta(days=1), cfg["hunt"]["filings"].get("screener_feed", "important"), max_pages=2)
        sample = [f"{r['SLONGNAME']}: {r['HEADLINE'][:50]}" for r in rows[:3]]
        rep.append(f"✅ Screener filings: {len(rows)} on first pages, e.g. {sample}")
    except Exception as e:
        rep.append(f"❌ Screener filings: {e}")
    try:
        rows = filings.fetch_screener(br, date.today() - timedelta(days=2), "results", max_pages=2)
        sample = [f"{r['SLONGNAME']}: {r['MORE'][:50]}" for r in rows[:3]]
        rep.append(f"✅ Screener results feed: {len(rows)} filings in 2 days, e.g. {sample}" if rows else
                   "⚠️ Screener results feed: 0 filings in 2 days (normal outside results season)")
    except Exception as e:
        rep.append(f"❌ Screener results feed: {e}")
    try:
        rows = filings.fetch_range(http, date.today() - timedelta(days=1), date.today(), max_pages=1)
        rep.append(f"✅ BSE filings (backup source): {len(rows)} rows")
    except Exception as e:
        rep.append(f"➖ BSE filings (backup source, not needed): {str(e)[:80]}")
    band = cfg["hunt"].get("market_cap_cr", [100, 25000])
    for spec in cfg["hunt"].get("numbers_queries") or []:
        try:
            r = numbers_scan.run_query(br, numbers_scan.with_band(spec["query"], band), 2, tag=spec["name"])
            if r["error"]:
                rep.append(f"❌ query {spec['name']}: {r['error'][:200]}")
            else:
                names = ", ".join(x["name"] for x in r["rows"][:3])
                more = "+ (2 pages checked)" if r.get("capped") else ""
                rep.append(f"✅ query {spec['name']}: {len(r['rows'])}{more} companies, e.g. {names}")
        except Exception as e:
            rep.append(f"❌ query {spec['name']}: {e}")
    return "\n".join(rep)
