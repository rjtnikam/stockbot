"""Your codex as code: every number the LLMs see comes from here, never from an LLM.

Approximations are labelled as such; Screener's summary statements don't split
cash out of 'Other Assets', so capital-based ratios are close, not exact.
"""
from __future__ import annotations

import math
import re

import pandas as pd


def _row(df: pd.DataFrame, *names: str) -> pd.Series | None:
    if df is None or df.empty:
        return None
    for n in names:
        for idx in df.index:
            if idx.lower().startswith(n.lower()):
                return pd.to_numeric(df.loc[idx], errors="coerce")
    return None


def _annual(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    cols = [c for c in df.columns if re.match(r"^[A-Za-z]{3} \d{4}$", str(c))]
    return df[cols]


def _cagr(s: pd.Series | None, years: int) -> float | None:
    if s is None:
        return None
    s = s.dropna()
    if len(s) <= years:
        return None
    a, b = s.iloc[-years - 1], s.iloc[-1]
    if a is None or b is None or a <= 0 or b <= 0:
        return None
    return round(float((b / a) ** (1 / years) - 1), 4)


def _safe_div(a, b):
    try:
        if a is None or b is None or b == 0 or (isinstance(b, float) and math.isnan(b)):
            return None
        r = a / b
        return None if math.isnan(r) else r
    except Exception:
        return None


def _r(x, n=3):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def quarterly(q: pd.DataFrame) -> dict:
    sales, pat = _row(q, "Sales", "Revenue"), _row(q, "Net Profit")
    opm = _row(q, "OPM")
    out = {"periods": list(q.columns)}
    for name, s in (("sales", sales), ("pat", pat)):
        if s is None:
            continue
        yoy = (s / s.shift(4) - 1).where(s.shift(4) > 0)
        out[f"{name}_values"] = [_r(v, 2) for v in s.tolist()]
        out[f"{name}_yoy"] = [_r(v) for v in yoy.tolist()]
        recent = yoy.dropna()
        if len(recent) >= 6:
            last2, prev4 = recent.iloc[-2:].mean(), recent.iloc[-6:-2].mean()
            out[f"{name}_acceleration"] = _r(last2 - prev4)
            out[f"{name}_trend"] = ("accelerating" if last2 - prev4 > 0.10 else
                                   "decelerating" if last2 - prev4 < -0.10 else "steady")
    if pat is not None and pat.dropna().size >= 4:
        p = pat.dropna()
        out["ttm_pat"] = _r(p.iloc[-4:].sum(), 2)
        if p.size >= 8 and p.iloc[-8:-4].sum() > 0:
            out["ttm_pat_growth"] = _r(p.iloc[-4:].sum() / p.iloc[-8:-4].sum() - 1)
    if sales is not None and sales.dropna().size >= 4:
        out["ttm_sales"] = _r(sales.dropna().iloc[-4:].sum(), 2)
    if opm is not None:
        out["opm_pct"] = [_r(v, 1) for v in opm.tolist()]
    return out


def annual(pl: pd.DataFrame, bs: pd.DataFrame, cf: pd.DataFrame, ratios: pd.DataFrame, tax_rate: float) -> dict:
    pl, bs, cf, ratios = _annual(pl), _annual(bs), _annual(cf), _annual(ratios)
    sales, op, interest = _row(pl, "Sales", "Revenue"), _row(pl, "Operating Profit"), _row(pl, "Interest")
    dep, pbt, pat = _row(pl, "Depreciation"), _row(pl, "Profit before tax"), _row(pl, "Net Profit")
    fa, cwip = _row(bs, "Fixed Assets", "Net Block"), _row(bs, "CWIP")
    oa, ol = _row(bs, "Other Assets"), _row(bs, "Other Liabilities")
    eq, res, debt = _row(bs, "Equity Capital"), _row(bs, "Reserves"), _row(bs, "Borrowings")
    cfo = _row(cf, "Cash from Operating")
    out: dict = {"years": list(pl.columns) if pl is not None else []}

    out["sales_cagr_3y"], out["sales_cagr_5y"] = _cagr(sales, 3), _cagr(sales, 5)
    out["pat_cagr_3y"], out["pat_cagr_5y"] = _cagr(pat, 3), _cagr(pat, 5)

    ebit = (op - dep) if op is not None and dep is not None else None
    if ebit is not None:
        nopat = ebit * (1 - tax_rate)
        out["ebit"] = [_r(v, 1) for v in ebit.tolist()]
    else:
        nopat = None

    # Buffett interest coverage: pre-interest pretax earnings / interest (NOT EBITDA)
    if pbt is not None and interest is not None:
        cov = (pbt + interest) / interest.where(interest > 0)
        out["interest_coverage"] = _r(cov.dropna().iloc[-1], 1) if cov.dropna().size else "no debt cost"

    # Invested capital ~ tangible net assets: fixed assets + CWIP + net working capital (incl. cash)
    ic = None
    if fa is not None and oa is not None and ol is not None:
        ic = fa + (cwip if cwip is not None else 0) + (oa - ol)
        out["invested_capital"] = [_r(v, 1) for v in ic.tolist()]
    if ebit is not None and ic is not None:
        avg_ic = (ic + ic.shift(1)) / 2
        rotna = (ebit / avg_ic).dropna()
        out["rotna_approx"] = [_r(v) for v in rotna.tolist()[-5:]]
        latest = rotna.iloc[-1] if len(rotna) else None
        out["rotna_band"] = (None if latest is None else
                             "terrific (25%+)" if latest >= 0.25 else
                             "good (12-20%+)" if latest >= 0.12 else "weak (<12%)")

    # Mauboussin: g = reinvestment rate x ROIIC (3-year windows)
    if nopat is not None and ic is not None and nopat.dropna().size >= 5 and ic.dropna().size >= 5:
        n, i = nopat.dropna(), ic.dropna()
        rr = _safe_div(i.iloc[-1] - i.iloc[-4], n.iloc[-3:].sum())
        roiic = _safe_div(n.iloc[-1] - n.iloc[-4], i.iloc[-2] - i.iloc[-5])
        out["reinvestment_rate_3y"] = _r(rr)
        out["roiic_3y"] = _r(roiic)
        out["implied_growth_rr_x_roiic"] = _r(rr * roiic) if rr is not None and roiic is not None else None

    if cfo is not None and pat is not None:
        c5, p5 = cfo.dropna().iloc[-5:].sum(), pat.dropna().iloc[-5:].sum()
        out["cfo_to_pat_5y"] = _r(_safe_div(c5, p5), 2)
    if cfo is not None and fa is not None and dep is not None:
        capex = (fa.diff() + dep + (cwip.diff() if cwip is not None else 0))
        fcf = (cfo - capex).dropna()
        out["fcf_5y_sum_approx"] = _r(fcf.iloc[-5:].sum(), 1)
        out["fcf_by_year_approx"] = [_r(v, 1) for v in fcf.tolist()[-5:]]
    if debt is not None and eq is not None and res is not None:
        out["debt_to_equity"] = _r(_safe_div(debt.iloc[-1], (eq + res).iloc[-1]), 2)
    for lbl, key in (("Debtor Days", "debtor_days"), ("Inventory Days", "inventory_days"),
                     ("Cash Conversion Cycle", "cash_conversion_cycle"), ("ROCE", "roce_pct")):
        s = _row(ratios, lbl)
        if s is not None:
            out[key] = [_r(v, 1) for v in s.tolist()[-6:]]
    return out


def shareholding(sh: pd.DataFrame) -> dict:
    out = {}
    for lbl, key in (("Promoters", "promoters"), ("FIIs", "fii"), ("DIIs", "dii"), ("No. of Shareholders", "shareholders")):
        s = _row(sh, lbl)
        if s is not None and s.dropna().size:
            s = s.dropna()
            out[key] = {"latest": _r(s.iloc[-1], 2), "first_in_table": _r(s.iloc[0], 2), "change": _r(s.iloc[-1] - s.iloc[0], 2)}
    return out


def reverse_dcf(mcap_cr: float | None, ttm_pat: float | None, roe_for_reinvestment: float | None,
                r: float, g_terminal: float, years: int) -> dict:
    """What earnings growth does today's price imply? Solves for g by bisection.

    FCF = earnings x (1 - g / return_on_new_capital): growing faster needs reinvestment.
    """
    if not mcap_cr or not ttm_pat or ttm_pat <= 0:
        return {"error": "needs positive TTM profit and market cap"}
    # growth only creates value if new capital earns above the cost of equity; floor it there
    roe = min(max(roe_for_reinvestment or 0.18, r + 0.03), 0.60)

    def value(g: float) -> float:
        """Growth g in year 1 fading linearly to terminal growth by the final year."""
        pv, e = 0.0, ttm_pat
        for t in range(1, years + 1):
            gt = g + (g_terminal - g) * (t - 1) / max(years - 1, 1)
            e *= 1 + gt
            pv += e * (1 - gt / roe) / (1 + r) ** t   # negative FCF allowed: growth needs capital
        tv = e * (1 + g_terminal) * (1 - g_terminal / roe) / (r - g_terminal)
        return pv + tv / (1 + r) ** years

    grid = [x / 1000 for x in range(-200, 1501, 5)]
    vals = [value(x) for x in grid]
    bracket = next(((grid[i], grid[i + 1]) for i in range(len(grid) - 1)
                    if (vals[i] - mcap_cr) * (vals[i + 1] - mcap_cr) <= 0), None)
    base = {"assumed_return_on_new_capital": round(roe, 3), "cost_of_equity": r,
            "terminal_growth": g_terminal, "ttm_pat_cr": round(float(ttm_pat), 2), "mcap_cr": mcap_cr,
            "trailing_pe": round(mcap_cr / ttm_pat, 1),
            "model": f"year-1 growth fading linearly to {g_terminal:.0%} over {years}y; FCF = E x (1 - g/ROIIC)"}
    if bracket is None:
        side = ">150%" if vals[-1] < mcap_cr else "<-20%"
        return {"implied_growth_10y": side, "note": "price outside model range", **base}
    lo, hi = bracket
    for _ in range(60):
        mid = (lo + hi) / 2
        if (value(lo) - mcap_cr) * (value(mid) - mcap_cr) <= 0:
            hi = mid
        else:
            lo = mid
    g = (lo + hi) / 2
    # report the 10y average growth the fade path implies (comparable with historical CAGR)
    e = 1.0
    for t in range(1, years + 1):
        e *= 1 + g + (g_terminal - g) * (t - 1) / max(years - 1, 1)
    return {"implied_growth_10y": round(e ** (1 / years) - 1, 4), "implied_year1_growth": round(g, 4), **base}


def build(company: dict, cfg: dict) -> dict:
    t = company["tables"]
    val = cfg["valuation"]
    q = quarterly(t.get("quarters"))
    a = annual(t.get("profit-loss"), t.get("balance-sheet"), t.get("cash-flow"), t.get("ratios"), val["tax_rate"])
    tr = company.get("top_ratios", {})
    mcap = tr.get("Market Cap")
    roe = tr.get("ROE")
    roiic = a.get("roiic_3y")
    reinvest_return = roiic if roiic and 0.05 < roiic < 1 else (roe / 100 if roe else None)
    rdcf = reverse_dcf(mcap, q.get("ttm_pat"), reinvest_return, val["cost_of_equity"],
                       val["terminal_growth"], val["explicit_years"])
    delivered = a.get("pat_cagr_5y") or a.get("pat_cagr_3y")
    implied = rdcf.get("implied_growth_10y")
    gap = gap_recent = None
    if isinstance(implied, float) and delivered is not None:
        gap = round(implied - delivered, 4)
    if isinstance(implied, float) and q.get("ttm_pat_growth") is not None:
        gap_recent = round(implied - q["ttm_pat_growth"], 4)
    return {
        "name": company.get("name"), "basis": company.get("basis"), "screener_url": company.get("url"),
        "about": company.get("about"), "top_ratios": tr,
        "quarterly": q, "annual": a, "shareholding": shareholding(t.get("shareholding")),
        "reverse_dcf": rdcf,
        "expectations_gap": {"implied_minus_delivered_growth": gap,
                             "delivered_growth_used": delivered,
                             "implied_minus_ttm_growth": gap_recent,
                             "ttm_pat_growth": q.get("ttm_pat_growth"),
                             "note": "gap vs 5y history = has the business ever done this; gap vs TTM = is the new run-rate already enough",
                             "reading": (None if gap is None else
                                         "price expects MORE than delivered (story must accelerate)" if gap > 0.03 else
                                         "price expects LESS than delivered (story ahead of price)" if gap < -0.03 else
                                         "price roughly in line with delivered growth")},
    }
