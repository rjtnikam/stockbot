"""The daily run. The scheduler calls this at 6 am; you can also run it by hand.

    python run_daily.py                          # full autonomous run: hunt -> theses
    python run_daily.py --hunt-only              # just the hunt board (no theses)
    python run_daily.py --dry-run                # everything except LLM calls (zero AI cost)
    python run_daily.py --symbols 539730         # analyse specific stocks now (skips the hunt)
    python run_daily.py --calibrate-hunt         # health check of BSE feed + every Screener query
    python run_daily.py --calibrate-marketsmith  # one-time check of MarketSmith extraction
    python run_daily.py --gates TAC              # one company against the quality gates (free)
    python run_daily.py --results TAC            # latest quarterly result of one company (free)
    python run_daily.py --evening                # day-end report: today's important exchange filings
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from datetime import date

from bot import access, cards, fundamentals, marketsmith, prices, screener, store, story_stage, technicals, telegram, timeline
from bot.config import DATA, REPORTS, load_config, read_watchlist
from bot.lock import PAUSE, browser_lock
from bot.session import browser_session, marketsmith_logged_in, screener_logged_in

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout),
                              logging.FileHandler(DATA / "run.log", encoding="utf-8")])
log = logging.getLogger("run")


def extra_sources(br, cfg, logged_in: dict) -> list[tuple[str, str]]:
    """Optional extra candidate sources: MarketSmith lists and your saved Screener screens."""
    out: list[tuple[str, str]] = []
    if logged_in.get("marketsmith"):
        for i, url in enumerate(cfg["marketsmith"]["lists"]):
            try:
                out += [(r["symbol"], f"marketsmith:{i}") for r in marketsmith.fetch_list(br, url, tag=f"list{i}")]
            except Exception as e:
                log.warning("MarketSmith list failed: %s", e)
    if logged_in.get("screener"):
        for url in cfg["screener"]["screens"]:
            try:
                out += [(s, "screener") for s in screener.fetch_screen_symbols(br, url, cfg["screener"].get("max_pages_per_screen", 3))]
            except Exception as e:
                log.warning("Screener screen failed: %s", e)
    return out


def universe_rs(cfg, bench) -> dict:
    """Optional: RS scores for a reference universe (data/universe.txt) for true percentiles."""
    path = DATA / "universe.txt"
    if not path.exists() or bench.empty:
        return {}
    scores = {}
    for sym in [l.strip().upper() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]:
        try:
            px = prices.stock_history(sym, 2)
            if len(px) > 260:
                scores[sym] = technicals.rs_score(px["Close"], bench["Close"].reindex(px.index).ffill())
        except Exception:
            pass
    return scores


def _hunt_brief(h: dict | None) -> dict | None:
    """What the thesis writer needs from the hunter (no raw page data)."""
    if not h:
        return None
    return {"score": h["score"], "setups": h["setups"], "stage_hint": h["stage_hint"],
            "why_flagged": [{k: c[k] for k in ("label", "points", "url")} for c in h["components"]],
            "red_flags": h["red_flags"], "deep_check": h.get("deep_check"),
            "filing_documents": (h.get("documents") or {}).get("filings"),
            "management_quality": (h.get("documents") or {}).get("management")}


def analyse_stock(br, sym: str, source: str, cfg: dict, bench, rs_universe: dict, dry: bool,
                  hunt: dict | None = None) -> dict:
    if hunt and hunt.get("company"):
        company, numbers = hunt["company"], hunt["numbers"]
        sym = hunt.get("screener_slug") or sym
    else:
        company = screener.fetch_company(br, sym)
        numbers = fundamentals.build(company, cfg)
    # Technicals come from MarketSmith (its ratings, bases, lists, daily prices + benchmark).
    ms, px, bench_used, ms_error = None, None, bench, None
    codes = company.get("codes") or {}
    try:
        ms = marketsmith.stock_technicals(br, [codes.get("nse"), codes.get("bse_id"), sym if not sym.isdigit() else None])
    except Exception as e:
        log.warning("MarketSmith failed for %s: %s", sym, e)
        ms_error = "logged out" if "session expired" in str(e) else "could not read"
    if ms and ms.get("prices_daily") is not None and len(ms["prices_daily"]) >= 60:
        px = ms["prices_daily"]
        if ms.get("benchmark_daily") is not None:
            bench_used = ms["benchmark_daily"]
        log.info("technicals for %s from MarketSmith (%s)", sym, ms["ms_symbol"])
    else:  # backup: Screener closes, then Yahoo
        if company.get("company_id"):
            try:
                px = screener.price_history(br, company["company_id"], int(cfg["market"].get("price_history_years", 6) * 366))
            except Exception as e:
                log.warning("Screener prices failed for %s (%s)", sym, e)
        if px is None or px.empty:
            px = prices.stock_history(sym, cfg["market"].get("price_history_years", 6), codes)
        log.info("technicals for %s computed from %s prices (MarketSmith unavailable)", sym, "Screener/Yahoo")
    tech = technicals.analyse(px, bench_used)
    if ms:
        tech["marketsmith"] = {k: v for k, v in ms.items() if not k.startswith(("prices_", "benchmark_"))}
        tech["source"] = "MarketSmith ratings; stage/trend/base metrics below are derived from MarketSmith prices"
    else:
        tech["source"] = "computed by the bot (MarketSmith unavailable for this stock)"
    my_rs = tech.get("rs_score_vs_benchmark")
    rs_pct = (ms or {}).get("rs_rating")
    if rs_pct is None and rs_universe and my_rs is not None:
        rs_pct = technicals.rs_percentiles({**rs_universe, "__me__": my_rs})["__me__"]
    tech["rs_percentile"] = rs_pct
    tl = timeline.build(numbers["quarterly"], px, bench_used)
    chart_path = timeline.chart(sym, px, tl, REPORTS / f"{date.today()}_{sym}.png")

    research = {"x": {}, "web": {}, "valuepickr": {}}
    if not dry:
        rcfg = cfg["research"]
        name = numbers.get("name") or sym
        from bot import research_gemini, research_grok, valuepickr
        for key, fn in (("x", lambda: research_grok.x_research(name, sym, rcfg["lookback_days"])),
                        ("web", lambda: research_gemini.web_research(name, sym, rcfg["lookback_days"])),
                        ("valuepickr", lambda: research_gemini.digest_valuepickr(
                            name, valuepickr.fetch(name, sym, rcfg["valuepickr_max_posts"], rcfg["lookback_days"])))):
            try:
                research[key] = fn()
            except Exception as e:
                log.warning("%s research failed for %s: %s", key, sym, e)
                research[key] = {"error": str(e)}

    brief = _hunt_brief(hunt)
    stage = story_stage.assess(numbers, tech, rs_pct, research["x"], research["web"], research["valuepickr"],
                               hunt_setups=(brief or {}).get("setups"))
    q_per = (numbers.get("quarterly") or {}).get("periods") or [None]
    sources = {  # proof of access: shown on the card and in the memo file
        "screener": {"ok": True, "at": company.get("fetched_at"), "url": company.get("url"),
                     "what": f"{company.get('basis') or 'company'} page, results up to {q_per[-1]}"},
        "marketsmith": ({"ok": True, "at": ms.get("fetched_at"), "url": marketsmith.EVAL_URL.format(sym=ms["ms_symbol"].lower()),
                         "what": f"RS {ms.get('rs_rating')}, EPS {ms.get('eps_rating')}, Master {ms.get('master_score')}"}
                        if ms else {"ok": False, "what": (ms_error or "no data for this stock")
                                    + "; technicals computed by the bot from " + ("Screener" if px is not None and px.attrs.get("source") == "screener" else "backup") + " prices"}),
    }
    dossier = {"symbol": sym, "as_of": date.today().isoformat(), "candidate_source": source, "hunt": brief,
               "data_sources": sources,
               "codes": codes, "numbers": numbers, "technicals": tech, "timeline": tl, "story_stage": stage,
               "research": research}
    (REPORTS / f"{date.today()}_{sym}_dossier.json").write_text(json.dumps(dossier, default=str, indent=1), encoding="utf-8")

    name = numbers.get("name") or sym
    discussion = None
    if dry:
        memo, card = _dry_memo(sym, numbers, tech, stage, tl, brief), {}
    else:
        # the council: Claude opens, Grok and Gemini answer and challenge, then Claude decides (never alone)
        from bot import council, thesis
        discussion = council.run("stock", topic=f"{name} ({sym})", brief=cards.stock_brief(dossier),
                                 facts=cards.stock_facts(dossier), sym=sym, name=name, cfg=cfg)
        try:
            memo, card = thesis.write_thesis(dossier, discussion)
        except council.NoDiscussion as e:
            memo = f"## Verdict\nNO DECISION. Claude does not decide alone, and {e}."
            card = {"verdict": "NO DECISION", "decision_reason": f"No decision: {e}. Claude does not decide alone."}
        council.record_stock_decision(discussion, card)
    md_path = REPORTS / f"{date.today()}_{sym}.md"
    talk = f"\n\n## The discussion (transcript)\n\n{discussion.transcript()}\n" if discussion else ""
    md_path.write_text(f"# {name} ({sym}) — {date.today()}\n\n{memo}\n{talk}", encoding="utf-8")
    html_path = cards.memo_file(REPORTS / f"{date.today()}_{sym}.html", name, sym, memo, numbers, card,
                                discussion, dry=dry, sources=sources)
    if card.get("verdict") != "NO DECISION":  # no decision = try again on the next run
        store.log_analysis(sym, tech.get("price"), stage["stage"], card,
                           numbers["reverse_dcf"].get("implied_growth_10y"), source, str(md_path))
    return {"memo": memo, "card": card, "md": str(md_path), "html": html_path, "chart": chart_path,
            "stage": stage, "name": name, "sym": sym, "numbers": numbers, "discussion": discussion,
            "sources": sources}


def _dry_memo(sym, n, t, s, tl, brief=None) -> str:
    q, a = n["quarterly"], n["annual"]
    lines = [f"DRY RUN (no LLMs) — {n.get('name')}"]
    if brief:
        lines.append(f"Hunt score {brief['score']} · setups: {', '.join(brief['setups']) or '-'}")
        lines.append("Why flagged: " + "; ".join(c["label"] for c in brief["why_flagged"][:6]))
    lines += [
        f"Stage (rules): {s['stage']} ({s['stage_confidence']:.0%}), runner-up {s['runner_up']}; price vs story: {s['price_vs_story']}",
        f"PAT trend: {q.get('pat_trend')} | last PAT YoY: {(q.get('pat_yoy') or [None])[-1]}",
        f"ROTNA: {a.get('rotna_band')} | RRxROIIC: {a.get('implied_growth_rr_x_roiic')} | CFO/PAT 5y: {a.get('cfo_to_pat_5y')}",
        f"Interest coverage: {a.get('interest_coverage')} | D/E: {a.get('debt_to_equity')}",
        f"Reverse DCF implied 10y growth: {n['reverse_dcf'].get('implied_growth_10y')} vs delivered {n['expectations_gap'].get('delivered_growth_used')}",
        (lambda m: f"MarketSmith: RS {m.get('rs_rating')} · EPS {m.get('eps_rating')} · Master {m.get('master_score')} · A/D {m.get('acc_dis_rating')} · group {m.get('industry_group_rank')} · market {m.get('market_direction')} · base {m.get('latest_base')} · lists {', '.join(m.get('in_lists') or [])}" if m else "MarketSmith: not available for this stock")(t.get("marketsmith")),
        f"Derived: Weinstein {(t.get('weinstein') or {}).get('stage')} | trend template {t.get('trend_template_passed')}/8",
        f"Price followed numbers: {tl.get('price_followed_numbers_share')} | next result ~{tl.get('next_result_expected_around')}",
        f"Evidence: {json.dumps(s['evidence'])}",
    ]
    return "\n".join(lines)


def build_queue(cfg, limits, hunt_res, extra) -> list[tuple[str, str, dict | None]]:
    """Priority: watchlist names with new filings > hunt finalists > watchlist refresh > extra sources."""
    watch = read_watchlist(cfg)
    days = limits["reanalyse_after_days"]
    hunted = {f["slug"]: f for f in (hunt_res.finalists if hunt_res else [])}
    fresh = hunt_res.fresh_slugs if hunt_res else set()
    q: list[tuple[str, str, dict | None]] = []
    q += [(s, "watchlist: new filing", hunted.get(s)) for s in watch if s in fresh]
    n_hunt = int(cfg.get("hunt", {}).get("thesis_top", 4))
    q += [(s, "hunt", f) for s, f in list(hunted.items()) if not store.recently_analysed(s, days)][:n_hunt]
    q += [(s, "watchlist", None) for s in watch if not store.recently_analysed(s, days)]
    q += [(s, src, None) for s, src in extra if not store.recently_analysed(s, days)]
    seen, out = set(), []
    for s, src, h in q:
        if s not in seen:
            seen.add(s)
            out.append((s, src, h))
    return out[: limits["max_stocks_per_run"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", help="analyse these instead of hunting")
    ap.add_argument("--dry-run", action="store_true", help="skip all LLM calls")
    ap.add_argument("--hunt-only", action="store_true", help="send the hunt board, no theses")
    ap.add_argument("--weekly", action="store_true", help="force the weekly Screener market scan today")
    ap.add_argument("--gates", metavar="SYMBOL", help="check one company against the quality gates (free)")
    ap.add_argument("--results", metavar="SYMBOL", help="latest quarterly result of one company (free)")
    ap.add_argument("--evening", action="store_true", help="day-end filing observer: today's important filings")
    ap.add_argument("--now", action="store_true", help="run even when paused (a request you sent from Telegram)")
    ap.add_argument("--calibrate-hunt", action="store_true")
    ap.add_argument("--calibrate-marketsmith", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    limits = cfg["limits"]
    scheduled = not (args.symbols or args.gates or args.results or args.calibrate_hunt or args.calibrate_marketsmith)
    if scheduled and PAUSE.exists() and not args.now:
        log.info("paused (send /resume in Telegram); nothing done")
        return
    with browser_lock():
        mode = ("analyse " + " ".join(args.symbols)) if args.symbols else ("gates " + args.gates) if args.gates else \
            ("results " + args.results) if args.results else "calibrate" if (args.calibrate_hunt or args.calibrate_marketsmith) \
            else "evening filings" if args.evening else "morning run" if not args.hunt_only else "scan only"
        access.start_run(mode)
        _main(args, cfg, limits)


def _gates_one(br, cfg, sym: str) -> None:
    from hunt import confirm, db, gates
    slug = sym.upper()
    try:
        comp = screener.fetch_company(br, slug)
    except Exception:
        alt = screener.resolve_slug(br, sym)
        if not alt:
            telegram.send(f"❓ {sym}: not found on Screener.")
            return
        slug, comp = alt, screener.fetch_company(br, alt)
    nums = fundamentals.build(comp, cfg)
    c = confirm.deep_check(comp, nums, cfg)
    db.doc_put(f"deep:{slug.upper()}", c)
    telegram.send(gates.report(comp.get("name") or slug, slug, c["gate"]))


def _main(args, cfg, limits):
    with browser_session(tuple(limits["page_delay_seconds"]),
                         require_login=not (args.symbols or args.gates or args.results)) as br:
        if args.calibrate_marketsmith:
            ok = marketsmith_logged_in(br, cfg)
            print("MarketSmith logged in:", ok)
            for i, url in enumerate(cfg["marketsmith"]["lists"]):
                rows = marketsmith.fetch_list(br, url, calibrate=True, tag=f"list{i}")
                print(f"list {i}: {len(rows)} stocks -> {[r['symbol'] for r in rows[:15]]}")
            print("Debug screenshots/data saved in data/raw/")
            return
        if args.gates:
            _gates_one(br, cfg, args.gates)
            return
        if args.results:
            from hunt import results
            telegram.send_html(results.one(br, cfg, args.results))
            return
        if args.calibrate_hunt:
            from hunt import runner
            print("Screener logged in:", screener_logged_in(br))
            rep = runner.calibrate(br, cfg)
            print(rep)
            telegram.send(rep)
            return

        if args.evening:
            from hunt import observer
            if not screener_logged_in(br):
                telegram.send("⚠️ Evening filings: the Screener login expired. Run login_local.py on your laptop "
                              "and copy session.json to the server.")
                return
            out = observer.run(br, cfg, set(read_watchlist(cfg)), dry=args.dry_run)
            telegram.send_html(out["digest"])
            return

        if args.symbols:
            queue = [(s.upper(), "manual", None) for s in args.symbols]
        else:
            logged_in = {"screener": screener_logged_in(br), "marketsmith": marketsmith_logged_in(br, cfg)}
            expired = [k for k, v in logged_in.items() if not v and (k != "marketsmith" or cfg["marketsmith"]["lists"])]
            if expired:
                telegram.send(f"⚠️ Login expired for: {', '.join(expired)}. Run login_local.py on your laptop "
                              f"and copy session.json to the server. Continuing with what works today.")
            hunt_res = None
            if cfg.get("hunt", {}).get("enabled", True):
                try:
                    from hunt import runner
                    hunt_res = runner.run(br, cfg, read_watchlist(cfg), dry=args.dry_run,
                                          screener_ok=logged_in["screener"], force_numbers=args.weekly,
                                          logins=logged_in)
                    telegram.send_html(hunt_res.board)
                    if hunt_res.results_digest:
                        telegram.send_html(hunt_res.results_digest)
                except Exception as e:
                    log.error("hunt failed: %s\n%s", e, traceback.format_exc())
                    telegram.send(f"⚠️ Today's scan failed: {e}")
            if args.hunt_only:
                return
            queue = build_queue(cfg, limits, hunt_res, extra_sources(br, cfg, logged_in))

        log.info("analysing %s", [(s, src) for s, src, _ in queue])
        if not queue:
            telegram.send_html("😴 No stock passed the quality checks today. No memo written, no AI tokens spent.")
            return

        bench = prices.benchmark_history(cfg)
        rs_universe = universe_rs(cfg, bench)
        failed = []
        for sym, src, hunt in queue:
            try:
                r = analyse_stock(br, sym, src, cfg, bench, rs_universe, args.dry_run, hunt=hunt)
                telegram.send_html(cards.stock_card(r["name"], r["sym"], r["numbers"], r["card"], r["discussion"],
                                                    dry=args.dry_run, stage=r["stage"]["stage"], sources=r["sources"]))
                if r["discussion"] is not None and cfg.get("telegram", {}).get("send_discussions", True):
                    telegram.send_html(cards.discussion_folded(r["discussion"]))  # every morning, folded
                if not cfg.get("telegram", {}).get("quiet", True) and r["chart"]:
                    telegram.send_file(r["chart"], caption=f"{r['name']} — price and results timeline", photo=True)
                telegram.send_file(r["html"], caption=f"{r['name']} — full memo and the bots' discussion")
            except Exception as e:
                log.error("failed %s: %s\n%s", sym, e, traceback.format_exc())
                failed.append(f"• {telegram.esc(sym)}: {telegram.esc(str(e)[:150])}")
        if failed:
            telegram.send_html("⚠️ <b>Could not finish</b>\n" + "\n".join(failed))

if __name__ == "__main__":
    main()
