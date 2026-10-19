"""Turns raw signals into a ranked hunt list.

score = numbers hits (current) + filing signals (fading with age)
        + setup bonuses (combinations that tell a story)
        + deep-check / document adjustments (finalists only)
        - red flags; kill-level red flags remove the company.
Every point carries a label and a source link, so the ranking is explainable.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

from . import db, gates
from .tags import info

INFLECTION_QUERIES = {"revenue_jump", "eps_acceleration", "margin_expansion", "profit_inflection"}
CAPEX_QUERIES = {"capex_ahead_of_demand", "gross_block_expansion"}
DEFAULT_SETUP_BONUS = {
    "capex_conversion": 3.0, "capex_digestion": 1.0, "earnings_inflection": 2.0, "coming_out": 2.0,
    "skin_in_game": 1.5, "material_order": 2.0, "new_vector": 1.0, "turnaround": 1.0,
}


def _age(d: str, today: date) -> int:
    try:
        return max(0, (today - date.fromisoformat(d[:10])).days)
    except Exception:
        return 0


def score_all(cfg: dict, today: date | None = None, watch: set[str] | None = None,
              confirm: dict | None = None, docs: dict | None = None) -> list[dict]:
    today = today or date.today()
    watch = watch or set()
    confirm, docs = confirm or {}, docs or {}
    h = cfg["hunt"]
    half = float(h.get("half_life_days", 45))
    overrides = h.get("tag_weights") or {}
    bonus = {**DEFAULT_SETUP_BONUS, **(h.get("setup_bonus") or {})}
    numw = {q["name"]: float(q.get("weight", 1.0)) for q in h.get("numbers_queries") or []}
    lo, hi = h.get("market_cap_cr", [100, 25000])

    sigs = db.signals_since((today - timedelta(days=730)).isoformat())
    num_dates = [s["run_date"] for s in sigs if s["signal"].startswith("num:")]
    latest_num = max(num_dates) if num_dates else None
    fresh_num = latest_num and _age(latest_num, today) <= int(h.get("numbers_every_days", 7)) + 2
    growth_cap = float(h.get("growth_bucket_cap", 3.0))

    by = defaultdict(list)
    for s in sigs:
        by[s["slug"]].append(s)

    out = []
    for slug, ss in by.items():
        name = next((s["name"] for s in sorted(ss, key=lambda x: x["event_date"], reverse=True) if s["name"]), slug)
        comp: list[dict] = []
        flags, kills = [], []

        def add(label, pts, url=None, kind="pos"):
            comp.append({"label": label, "points": round(pts, 2), "url": url, "kind": kind})

        # ---- numbers (only the latest scan counts: it reflects today's data)
        num_hits, mcap = set(), None
        for s in ss:
            if s["signal"].startswith("num:") and fresh_num and s["run_date"] == latest_num:
                q = s["signal"][4:]
                if q not in num_hits:
                    num_hits.add(q)
                    pts = numw.get(q, 1.0)
                    if q in INFLECTION_QUERIES:  # rev / EPS / margin / profit = one growth event, not four
                        used = sum(x["points"] for x in comp if x["kind"] == "num" and x.get("bucket") == "growth")
                        pts = max(0.0, min(pts, growth_cap - used))
                    add(f"numbers: {q.replace('_', ' ')}", pts, s["url"], "num")
                    if q in INFLECTION_QUERIES:
                        comp[-1]["bucket"] = "growth"
                mcap = mcap or s["detail"].get("mcap")
        m = db.master_by_slug(slug)
        mcap = mcap or (m or {}).get("mcap") or (confirm.get(slug) or {}).get("market_cap_cr")
        if mcap and not (lo <= mcap <= hi) and slug not in watch:
            continue

        # ---- filings
        events = defaultdict(list)
        for s in ss:
            if s["signal"].startswith("num:"):
                continue
            ti = info(s["signal"], overrides)
            age = _age(s["event_date"], today)
            if age <= ti["window"]:
                events[s["signal"]].append((age, s))
        for tag, evs in events.items():
            ti = info(tag, overrides)
            evs.sort(key=lambda x: x[0])
            if ti["kind"] == "red":
                flags.append({"label": ti["label"], "date": evs[0][1]["event_date"], "url": evs[0][1]["url"]})
                if ti.get("kill"):
                    kills.append(ti["label"])
            if tag == "order_win":
                continue
            for age, s in evs[: (1 if ti["kind"] == "info" else 2)]:  # cap repeats: no spam inflation
                d = s["detail"] or {}
                mult = 1.0
                if tag == "capex_announced" and d.get("capex_cr") and (confirm.get(slug) or {}).get("ttm_sales_cr"):
                    mult = 1 + 3 * min(float(d["capex_cr"]) / confirm[slug]["ttm_sales_cr"], 0.5)
                if ti["kind"] == "pos" and d.get("change") is False:
                    mult *= 0.3  # the filing reader judged it routine
                if ti["kind"] == "pos" and isinstance(d.get("importance"), (int, float)):
                    mult *= 0.6 + 0.08 * float(d["importance"])  # reader's importance: 0 -> x0.6, 5 -> x1.0, 10 -> x1.4
                note = f": {d['note']}" if d.get("note") else ""
                add(f"{ti['label']}{note} ({s['event_date']})", ti["w"] * mult * 0.5 ** (age / half), s["url"],
                    "red" if ti["kind"] == "red" else "filing")

        # ---- orders: materiality = order value / TTM revenue
        ttm = (confirm.get(slug) or {}).get("ttm_sales_cr")
        orders = []
        for age, s in events.get("order_win", []):
            v = s["detail"].get("order_cr")
            orders.append({"age": age, "value": v, "mat": (v / ttm) if v and ttm else None, "s": s})
        orders.sort(key=lambda o: (o["mat"] or 0, o["value"] or 0), reverse=True)
        w_order = info("order_win", overrides)["w"]
        for o in orders[:2]:
            mult = 1 + 4 * min(o["mat"], 0.5) if o["mat"] is not None else 1.0
            val = f" ₹{o['value']:,.0f} Cr" if o["value"] else ""
            mat = f" = {o['mat']:.0%} of revenue" if o["mat"] is not None else ""
            add(f"order win{val}{mat} ({o['s']['event_date']})", w_order * mult * 0.5 ** (o["age"] / half),
                o["s"]["url"], "filing")
        recent = [o for o in orders if o["age"] <= 90]
        mats = [o["mat"] for o in recent if o["mat"] is not None]
        vs_mcap = [o["value"] / mcap for o in recent if o["value"] and mcap]  # fallback before the deep check
        tiny = (mats and statistics.median(mats) < 0.03) or (not mats and vs_mcap and statistics.median(vs_mcap) < 0.01)
        if len(recent) >= 4 and tiny:
            add("many tiny order announcements (promotion pattern)", -1.5, None, "red")
            flags.append({"label": "order-announcement spam", "date": today.isoformat(), "url": None})
        max_mat = max([o["mat"] for o in orders if o["age"] <= 180 and o["mat"] is not None], default=None)

        # ---- deep check (finalists)
        c = confirm.get(slug) or {}
        cap, qd = c.get("capex") or {}, c.get("quarterly") or {}
        if cap.get("capacity_built"):
            g_s = cap.get("sales_growth_2y_to_ttm")
            rev = f"revenue {g_s:+.0%}" if g_s is not None else "revenue n/a"
            if cap.get("capex_ahead_of_demand"):
                add(f"confirmed: assets +{cap['assets_incl_cwip_growth_2y']:.0%} vs {rev} over 2y "
                    f"(capacity ahead of demand)", 1.0, None, "confirm")
            elif cap.get("conversion_started"):
                add(f"confirmed: assets +{cap['assets_incl_cwip_growth_2y']:.0%}, {rev}, latest quarter "
                    f"{(qd.get('sales_yoy_last') or 0):+.0%} YoY (conversion underway)", 1.0, None, "confirm")
            if cap.get("self_funded"):
                add("capex largely funded from operating cash (expensive signal)", 0.5, None, "confirm")
        if cap.get("debt_to_equity") is not None and cap["debt_to_equity"] > 1.0:
            add(f"leverage: D/E {cap['debt_to_equity']}", -1.0, None, "red")
        if qd.get("eps_accelerating"):
            add(f"confirmed: EPS growth accelerating ({qd.get('eps_yoy_prev'):+.0%} → {qd.get('eps_yoy_last'):+.0%} YoY)",
                0.5, None, "confirm")

        # ---- documents (finalists)
        d = docs.get(slug) or {}
        promoter_warrants = False
        for f in d.get("filings") or []:
            x = f.get("extracted") or {}
            if f["tag"] == "warrants_preferential":
                if x.get("expensive_signal") is True:
                    promoter_warrants = True
                    add("promoters paying for warrants/shares at or above market (expensive signal)", 2.0, f["url"], "doc")
                ps, n = x.get("promoter_share_of_issue_pct"), x.get("n_allottees")
                try:
                    if (ps is not None and float(ps) < 20) and (n is not None and int(n) >= 10):
                        add(f"preferential issue mostly to {n} non-promoter allottees (operator risk)", -2.0, f["url"], "red")
                        flags.append({"label": "preferential issue to many non-promoters", "date": f["date"], "url": f["url"]})
                except (TypeError, ValueError):
                    pass
            if f["tag"] == "order_win" and max_mat is None and ttm and x.get("order_value_cr"):
                try:
                    max_mat = float(x["order_value_cr"]) / ttm
                except (TypeError, ValueError):
                    pass
        mq = d.get("management") or {}

        def num_or_none(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        sd, cd = num_or_none(mq.get("say_do_score")), num_or_none(mq.get("candor_score"))
        if sd is not None:
            add(f"management say-do score {sd:.0f}/10", 1.5 if sd >= 7 else -1.5 if sd <= 3 else 0.0, None,
                "doc" if sd > 3 else "red")
        if cd is not None:
            add(f"management candor {cd:.0f}/10", 1.0 if cd >= 7 else -1.0 if cd <= 3 else 0.0, None,
                "doc" if cd > 3 else "red")
        es = mq.get("expensive_signals") or []
        if len(es) >= 2:
            add(f"{len(es)} expensive signals in concalls", 1.0, None, "doc")

        # ---- setups
        def has(tag, days):
            return any(a <= days for a, _ in events.get(tag, []))

        capex_sig = bool(num_hits & CAPEX_QUERIES) or has("capex_announced", 365) or cap.get("capacity_built")
        # conversion needs proof: plant commissioned, or the deep check sees assets AND revenue up.
        # A revenue jump alone is not proof (Kalind: revenue from zero, paid by new shares).
        conversion = capex_sig and (has("commissioning", 270) or bool(cap.get("conversion_started")))
        n_infl = len(num_hits & INFLECTION_QUERIES)
        setups = []
        if capex_sig and conversion:
            setups.append(("capex_conversion", "Capex → revenue conversion", "Inflection"))
        elif capex_sig:
            setups.append(("capex_digestion", "Capex digestion (built, not yet in revenue)", "Hidden"))
        if n_infl >= 2:
            setups.append(("earnings_inflection", "Earnings inflection", "Inflection"))
        few_calls = (c.get("concalls_listed_on_screener") is not None and c["concalls_listed_on_screener"] <= 1
                     and has("concall", 150))  # Screener lists at most one call ever = a first concall
        if (has("first_concall", 150) or few_calls) and (num_hits or conversion):
            setups.append(("coming_out", "Coming-out party: first concall + numbers moving", "Recognition starting"))
        if "promoter_buying" in num_hits or promoter_warrants:
            setups.append(("skin_in_game", "Skin in the game", None))
        if max_mat is not None and max_mat >= 0.25:
            setups.append(("material_order", f"Material order ({max_mat:.0%} of revenue)", None))
        if (has("new_product", 180) or has("new_geography", 180)) and num_hits:
            setups.append(("new_vector", "New growth vector + numbers moving", None))
        if "turnaround" in num_hits and n_infl >= 1:
            setups.append(("turnaround", "Turnaround", "Inflection"))
        for key, label, _ in setups:
            add(f"SETUP: {label}", bonus.get(key, 0.0), None, "setup")

        # ---- quality gates (deep check)
        gate = c.get("gate") or {}
        if gate.get("status") == "fail":
            kills += [f"gate: {x.split(' (rule')[0]}" for x in gate.get("hard_fail", [])[:3]]
        story = [t for t, evs in events.items()
                 if info(t, overrides)["kind"] == "pos" and any(a <= 90 and (s["detail"] or {}).get("change") is not False
                                                                for a, s in evs)]

        score = round(sum(x["points"] for x in comp), 2)
        if score <= 0 and not flags and slug not in watch:
            continue
        comp.sort(key=lambda x: abs(x["points"]), reverse=True)
        out.append({
            "slug": slug, "name": name, "score": score, "mcap": mcap, "components": comp,
            "setups": [s[1] for s in setups], "stage_hint": next((s[2] for s in setups if s[2]), None),
            "red_flags": flags, "excluded": bool(kills), "kill_reasons": kills,
            "num_hits": sorted(num_hits), "watch": slug in watch,
            "gate": gate.get("status"), "gate_line": gate.get("line"), "story_filings": story,
            "gate_reasons": gates.plain_reasons(gate) if gate else [],
        })
    out.sort(key=lambda r: (not r["excluded"], r["score"]), reverse=True)
    return out
