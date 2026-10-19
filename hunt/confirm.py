"""Deep check of a shortlisted company from its Screener page.

Answers: is the capex really ahead of demand (assets up, revenue not yet)?
Has conversion started? How was it funded? Is the quarterly trend real?
"""
from __future__ import annotations

import math

from bot.fundamentals import _annual, _row


def _f(x):
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except (TypeError, ValueError):
        return None


def _r(x, n=3):
    x = _f(x)
    return None if x is None else round(x, n)


def _last(s, k=1):
    if s is None:
        return None
    s = s.dropna()
    return _f(s.iloc[-k]) if len(s) >= k else None


def quarter_details(q) -> dict:
    sales, opm, pat, eps = _row(q, "Sales", "Revenue"), _row(q, "OPM"), _row(q, "Net Profit"), _row(q, "EPS")
    out = {}
    if sales is not None and sales.dropna().size >= 6:
        s = sales.dropna()
        out["sales_yoy_last"] = _r(s.iloc[-1] / s.iloc[-5] - 1) if s.iloc[-5] > 0 else None
        out["sales_yoy_prev"] = _r(s.iloc[-2] / s.iloc[-6] - 1) if s.iloc[-6] > 0 else None
        out["sales_qoq"] = _r(s.iloc[-1] / s.iloc[-2] - 1) if s.iloc[-2] > 0 else None
    if opm is not None and opm.dropna().size >= 5:
        o = opm.dropna()
        out["opm_last"] = _r(o.iloc[-1], 1)
        out["opm_change_yoy_pp"] = _r(o.iloc[-1] - o.iloc[-5], 1)
        out["opm_change_qoq_pp"] = _r(o.iloc[-1] - o.iloc[-2], 1)
    for name, ser in (("eps", eps), ("pat", pat)):
        if ser is not None and ser.dropna().size >= 6:
            e = ser.dropna()
            last = e.iloc[-1] / e.iloc[-5] - 1 if e.iloc[-5] > 0 else None
            prev = e.iloc[-2] / e.iloc[-6] - 1 if e.iloc[-6] > 0 else None
            out[f"{name}_yoy_last"], out[f"{name}_yoy_prev"] = _r(last), _r(prev)
            if last is not None and prev is not None:
                out[f"{name}_accelerating"] = bool(last > prev + 0.10 and last > 0.20)
    return out


def capex_metrics(company: dict, ttm_sales: float | None) -> dict:
    t = company["tables"]
    pl, bs, cf = _annual(t.get("profit-loss")), _annual(t.get("balance-sheet")), _annual(t.get("cash-flow"))
    sales, dep = _row(pl, "Sales", "Revenue"), _row(pl, "Depreciation")
    fa, cwip = _row(bs, "Fixed Assets", "Net Block"), _row(bs, "CWIP")
    debt, eq, res = _row(bs, "Borrowings"), _row(bs, "Equity Capital"), _row(bs, "Reserves")
    cfo = _row(cf, "Cash from Operating")
    if fa is None or sales is None or fa.dropna().size < 3 or sales.dropna().size < 3:
        return {"available": False}
    cap = (fa.fillna(0) + (cwip.fillna(0) if cwip is not None else 0)).dropna()
    s = sales.dropna()
    a_now, a_2y = _f(cap.iloc[-1]), _f(cap.iloc[-3])
    s_now = ttm_sales or _f(s.iloc[-1])
    s_2y = _f(s.iloc[-3])
    g_assets = (a_now / a_2y - 1) if a_2y and a_2y > 0 else None
    g_sales = (s_now / s_2y - 1) if s_2y and s_2y > 0 else None
    gap = (g_assets - g_sales) if g_assets is not None and g_sales is not None else None
    out = {
        "available": True,
        "assets_incl_cwip_growth_2y": _r(g_assets),
        "sales_growth_2y_to_ttm": _r(g_sales),
        "capacity_gap": _r(gap),
        "cwip_to_fixed_assets": _r(_last(cwip) / _last(fa)) if cwip is not None and _last(fa) else None,
        "asset_turnover_now": _r(s_now / a_now) if a_now else None,
        "asset_turnover_2y_ago": _r(s_2y / a_2y) if a_2y else None,
    }
    # funding of the build-out
    if debt is not None and debt.dropna().size >= 3:
        out["debt_change_2y_cr"] = _r(debt.dropna().iloc[-1] - debt.dropna().iloc[-3], 1)
    if eq is not None and eq.dropna().size >= 3 and eq.dropna().iloc[-3] > 0:
        out["equity_capital_change_2y"] = _r(eq.dropna().iloc[-1] / eq.dropna().iloc[-3] - 1)
    if cfo is not None and cfo.dropna().size >= 2 and a_2y is not None:
        dep2 = _f(dep.dropna().iloc[-2:].sum()) if dep is not None and dep.dropna().size >= 2 else 0
        capex_2y = (a_now - a_2y) + (dep2 or 0)
        cfo_2y = _f(cfo.dropna().iloc[-2:].sum())
        out["capex_2y_approx_cr"] = _r(capex_2y, 1)
        out["cfo_2y_cr"] = _r(cfo_2y, 1)
        out["capex_funded_by_cfo_share"] = _r(cfo_2y / capex_2y) if capex_2y and capex_2y > 0 else None
    if debt is not None and eq is not None and res is not None:
        nw = _f((eq + res).dropna().iloc[-1]) if (eq + res).dropna().size else None
        out["debt_to_equity"] = _r(_last(debt) / nw, 2) if nw else None

    de = out.get("debt_to_equity")
    out["capacity_built"] = bool(g_assets is not None and g_assets > 0.40)
    out["capex_ahead_of_demand"] = bool(out["capacity_built"] and gap is not None and gap > 0.25
                                        and (de is None or de < 1.2))
    funded = out.get("capex_funded_by_cfo_share")
    out["self_funded"] = bool(funded is not None and funded >= 0.6)
    return out


def metrics(company: dict, numbers: dict) -> dict:
    q = quarter_details(company["tables"].get("quarters"))
    cap = capex_metrics(company, (numbers.get("quarterly") or {}).get("ttm_sales"))
    docs = company.get("documents") or {}
    concalls = docs.get("concalls") or []
    cap["conversion_started"] = bool(cap.get("capacity_built") and (q.get("sales_yoy_last") or 0) > 0.25)
    return {
        "quarterly": q, "capex": cap,
        "ttm_sales_cr": (numbers.get("quarterly") or {}).get("ttm_sales"),
        "market_cap_cr": (company.get("top_ratios") or {}).get("Market Cap"),
        "concalls_listed_on_screener": len(concalls),
        "transcripts": [c["links"]["transcript"] for c in concalls if "transcript" in c.get("links", {})][:3],
    }



def deep_check(company: dict, numbers: dict, cfg: dict) -> dict:
    """Deep-check metrics + quality gates from one Screener page (shared by the scan and the results reader)."""
    from . import gates
    c = metrics(company, numbers)
    c["transcripts"] = [u if u.startswith("http") else "https://www.screener.in" + u for u in c["transcripts"]]
    g = gates.evaluate(company, numbers, cfg)
    g["line"] = gates.one_line(g)
    c["gate"] = g
    return c
