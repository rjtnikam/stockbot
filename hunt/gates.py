"""Quality gates: free checks from the Screener company page, BEFORE any paid LLM call.

Result per company:
  status = "pass"  -> may get a thesis
           "watch" -> listed on the board only (soft gate failed, e.g. turnaround with weak CFO history)
           "fail"  -> removed (hard gate failed: shell, promoter exit, dilution, receivables)
Every check records its value and the threshold, so the board can show WHY.
Missing data is "unknown", never a silent pass.
"""
from __future__ import annotations

import logging

import pandas as pd

from bot.fundamentals import _annual, _row

log = logging.getLogger(__name__)

DEFAULTS = {
    "min_mcap_cr": 300,
    "min_years_of_sales": 3,          # sales > 0 in each of the last N fiscal years
    "min_sales_3y_ago_vs_ttm": 0.10,  # sales 3 years ago >= 10% of TTM (no restart from zero)
    "min_cfo_positive_years_of_5": 3,
    "min_cfo_to_pat_5y": 0.6,
    "min_promoter_pct": 35,
    "max_promoter_drop_pp": 10,       # over the shareholding table (~3 years)
    "max_dilution_3y": 0.15,          # equity capital growth not explained by a bonus issue
    "max_debtor_days": 120,
    "max_debtor_days_jump": 30,
    "min_roce_3y_avg": 12,
    "max_debt_to_equity": 1.0,
    "min_interest_coverage": 3,
    "max_unknown": 3,                 # more unknown checks than this -> watch, not pass
}
HARD = {"real_business", "min_mcap", "promoter_level", "promoter_exit", "dilution", "debtor_days", "debtor_jump",
        "leverage"}
SOFT = {"cfo_years", "cfo_to_pat", "roce", "interest_coverage"}


def _series(df, *names):
    s = _row(df, *names) if df is not None else None
    return None if s is None else pd.to_numeric(s, errors="coerce").dropna()


def evaluate(company: dict, nums: dict, cfg: dict | None = None) -> dict:
    g = {**DEFAULTS, **((cfg or {}).get("hunt", {}).get("gates") or {})}
    t = company.get("tables") or {}
    pl, bs, cf = _annual(t.get("profit-loss")), _annual(t.get("balance-sheet")), _annual(t.get("cash-flow"))
    a, q, sh = nums.get("annual") or {}, nums.get("quarterly") or {}, nums.get("shareholding") or {}
    checks: dict[str, dict] = {}

    def put(key, ok, value, rule):
        checks[key] = {"ok": ok, "value": value, "rule": rule}

    # ---- 1. real business: sales every year, no restart from zero
    sales = _series(pl, "Sales", "Revenue")
    if sales is not None and len(sales) >= g["min_years_of_sales"] + 1:
        last_n = sales.iloc[-g["min_years_of_sales"]:]
        ttm = q.get("ttm_sales") or float(sales.iloc[-1])
        ago = float(sales.iloc[-(g["min_years_of_sales"] + 1)]) if len(sales) > g["min_years_of_sales"] else None
        ok = bool((last_n > 0).all() and ago is not None and ttm and ago >= g["min_sales_3y_ago_vs_ttm"] * ttm)
        put("real_business", ok, f"sales 3y ago ₹{ago:,.0f} Cr vs TTM ₹{ttm:,.0f} Cr" if ago is not None and ttm else None,
            f"sales > 0 each of last {g['min_years_of_sales']}y and 3y-ago ≥ {g['min_sales_3y_ago_vs_ttm']:.0%} of TTM")
    else:
        put("real_business", None, None, "sales history")

    # ---- 2. size
    mcap = (company.get("top_ratios") or {}).get("Market Cap")
    put("min_mcap", None if mcap is None else mcap >= g["min_mcap_cr"], mcap, f"mcap ≥ ₹{g['min_mcap_cr']} Cr")

    # ---- 3. cash: CFO positive most years, CFO/PAT
    cfo = _series(cf, "Cash from Operating")
    if cfo is not None and len(cfo) >= 3:
        last5 = cfo.iloc[-5:]
        pos = int((last5 > 0).sum())
        need = min(g["min_cfo_positive_years_of_5"], len(last5))
        put("cfo_years", pos >= need, f"{pos}/{len(last5)} years positive", f"CFO > 0 in ≥ {need} of last {len(last5)}y")
    else:
        put("cfo_years", None, None, "CFO history")
    c2p = a.get("cfo_to_pat_5y")
    put("cfo_to_pat", None if c2p is None else c2p >= g["min_cfo_to_pat_5y"], c2p, f"CFO/PAT 5y ≥ {g['min_cfo_to_pat_5y']}")

    # ---- 4. promoters
    p = sh.get("promoters")
    if p:
        put("promoter_level", p["latest"] >= g["min_promoter_pct"], f"{p['latest']}%", f"promoter ≥ {g['min_promoter_pct']}%")
        put("promoter_exit", p["change"] > -g["max_promoter_drop_pp"], f"{p['change']:+.1f}pp",
            f"promoter drop < {g['max_promoter_drop_pp']}pp (~3y)")
    else:
        put("promoter_level", None, None, "promoter holding")
        put("promoter_exit", None, None, "promoter holding")

    # ---- 5. dilution: equity capital up, not explained by a bonus issue (reserves fall in a bonus)
    eq, res = _series(bs, "Equity Capital"), _series(bs, "Reserves")
    if eq is not None and len(eq) >= 4 and eq.iloc[-4] > 0:
        grow = float(eq.iloc[-1] / eq.iloc[-4] - 1)
        bonus = False
        if grow > 0 and res is not None and len(res) >= 4:
            added = float(eq.iloc[-1] - eq.iloc[-4])
            res_drop_years = (res.diff().iloc[-3:] < -0.5 * added).any()
            bonus = bool(res_drop_years)
        put("dilution", grow <= g["max_dilution_3y"] or bonus,
            f"equity capital {grow:+.0%} in 3y" + (" (bonus issue)" if bonus else ""),
            f"dilution ≤ {g['max_dilution_3y']:.0%} in 3y")
    else:
        put("dilution", None, None, "equity capital history")

    # ---- 6. receivables
    dd = [x for x in (a.get("debtor_days") or []) if x is not None]
    if dd:
        put("debtor_days", dd[-1] <= g["max_debtor_days"], dd[-1], f"debtor days ≤ {g['max_debtor_days']}")
        put("debtor_jump", None if len(dd) < 2 else (dd[-1] - dd[-2]) <= g["max_debtor_days_jump"],
            None if len(dd) < 2 else f"{dd[-1] - dd[-2]:+.0f} days", f"debtor days jump ≤ {g['max_debtor_days_jump']}")
    else:
        put("debtor_days", None, None, "debtor days")

    # ---- 7. returns and balance sheet
    roce = [x for x in (a.get("roce_pct") or []) if x is not None][-3:]
    if roce:
        avg = sum(roce) / len(roce)
        rising = len(roce) >= 2 and roce[-1] >= g["min_roce_3y_avg"] and roce[-1] > roce[0]
        put("roce", avg >= g["min_roce_3y_avg"] or rising, f"3y avg {avg:.1f}%, latest {roce[-1]:.1f}%",
            f"ROCE 3y avg ≥ {g['min_roce_3y_avg']}% (or latest ≥ and rising)")
    else:
        put("roce", None, None, "ROCE")
    de = a.get("debt_to_equity")
    put("leverage", None if de is None else de <= g["max_debt_to_equity"], de, f"D/E ≤ {g['max_debt_to_equity']}")
    ic = a.get("interest_coverage")
    put("interest_coverage", True if ic == "no debt cost" else (None if ic is None else ic >= g["min_interest_coverage"]),
        ic, f"interest cover ≥ {g['min_interest_coverage']}")

    hard_fail = [k for k, c in checks.items() if c["ok"] is False and k in HARD]
    soft_fail = [k for k, c in checks.items() if c["ok"] is False and k in SOFT]
    unknown = [k for k, c in checks.items() if c["ok"] is None]
    status = "fail" if hard_fail else "watch" if (soft_fail or len(unknown) > g["max_unknown"]) else "pass"

    def why(k):
        c = checks[k]
        return f"{k.replace('_', ' ')}: {c['value']} (rule: {c['rule']})"

    return {"status": status, "hard_fail": [why(k) for k in hard_fail], "soft_fail": [why(k) for k in soft_fail],
            "hard_keys": hard_fail, "soft_keys": soft_fail,
            "unknown": unknown, "passed": sum(1 for c in checks.values() if c["ok"]), "total": len(checks),
            "checks": checks}


PLAIN = {
    "real_business": "no real sales history (business restarted from almost zero)",
    "min_mcap": "too small",
    "cfo_years": "cash flow negative in too many years",
    "cfo_to_pat": "profits not turning into cash",
    "promoter_level": "promoter stake low",
    "promoter_exit": "promoters selling",
    "dilution": "many new shares issued",
    "debtor_days": "customers pay very late",
    "debtor_jump": "customers paying much slower",
    "roce": "low return on capital",
    "leverage": "high debt",
    "interest_coverage": "interest burden high",
}


def plain_reasons(r: dict, n: int = 2) -> list[str]:
    """'promoters selling (-56.6pp)' style reasons for the Telegram messages."""
    keys = (r.get("hard_keys") or []) + (r.get("soft_keys") or [])
    out = []
    for k in keys[:n]:
        v = ((r.get("checks") or {}).get(k) or {}).get("value")
        if k == "cfo_to_pat" and v is not None:
            v = f"5-yr cash ÷ profit = {v}"
        elif k == "debtor_days" and v is not None:
            v = f"{v:.0f} days" if isinstance(v, (int, float)) else v
        elif k == "leverage" and v is not None:
            v = f"debt/equity {v}"
        out.append(PLAIN.get(k, k.replace("_", " ")) + (f" ({v})" if v not in (None, "") else ""))
    if not out:  # results cached by an older version: only the formatted strings exist
        out = [f.split(" (rule")[0] for f in (r.get("hard_fail") or []) + (r.get("soft_fail") or [])][:n]
    if not out and r.get("status") == "watch" and r.get("unknown"):
        out.append(f"{len(r['unknown'])} checks had no data")
    return out


def one_line(r: dict) -> str:
    if r["status"] == "pass":
        return f"✅ gates {r['passed']}/{r['total']}"
    fails = r["hard_fail"] or r["soft_fail"] or [f"{len(r['unknown'])} checks unknown"]
    return ("❌ " if r["status"] == "fail" else "👀 ") + "; ".join(f.split(" (rule")[0] for f in fails[:3])


def report(name: str, slug: str, r: dict) -> str:
    """Full gate checklist for Telegram (/gates)."""
    head = {"pass": "✅ PASS", "watch": "👀 WATCH LANE", "fail": "❌ STOPPED"}[r["status"]]
    lines = [f"🧱 GATES — {name} ({slug}): {head} · {r['passed']}/{r['total']} passed", ""]
    for k, c in r["checks"].items():
        mark = "✅" if c["ok"] else "❔" if c["ok"] is None else ("❌" if k in HARD else "⚠️")
        val = "no data" if c["value"] is None else c["value"]
        lines.append(f"{mark} {k.replace('_', ' ')}: {val}\n     rule: {c['rule']}")
    lines += ["", "❌ = hard gate (stops the stock) · ⚠️ = soft gate (watch lane) · ❔ = no data"]
    return "\n".join(lines)
