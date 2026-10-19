"""Rebuilds the MarketSmith-style technical read from raw prices, transparently.

Everything here is deterministic Python, so it can be backtested later.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _ret(s: pd.Series, days: int) -> float | None:
    if len(s) <= days:
        return None
    return float(s.iloc[-1] / s.iloc[-days - 1] - 1)


def rs_score(stock: pd.Series, bench: pd.Series) -> float | None:
    """IBD-style weighted relative strength: 40% last quarter, 20% each earlier quarter."""
    parts = []
    for days, w in ((63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)):
        rs, rb = _ret(stock, days), _ret(bench, days)
        if rs is None or rb is None:
            return None
        parts.append(w * ((1 + rs) / (1 + rb) - 1))
    return float(sum(parts))


def weinstein_stage(close: pd.Series) -> dict:
    """Stage via the 30-week (150-day) MA and its slope."""
    ma = close.rolling(150).mean()
    if ma.notna().sum() < 30:
        return {"stage": None, "note": "not enough history"}
    slope = float(ma.iloc[-1] / ma.iloc[-21] - 1)          # ~1 month slope
    above = close.iloc[-1] > ma.iloc[-1]
    prior_up = float(ma.iloc[-21] / ma.iloc[-min(len(ma) - 1, 126)] - 1) > 0.05
    if above and slope > 0.01:
        st = 2
    elif not above and slope < -0.01:
        st = 4
    elif prior_up:
        st = 3
    else:
        st = 1
    return {"stage": st, "ma150_slope_1m": round(slope, 4), "price_above_ma150": bool(above)}


def base_detection(df: pd.DataFrame, min_weeks: int = 5, max_weeks: int = 65) -> dict:
    """Finds the current consolidation: longest recent window whose depth stays < 35%."""
    close = df["Close"]
    best = None
    for weeks in range(min_weeks, max_weeks + 1):
        win = df.iloc[-weeks * 5:]
        if len(win) < weeks * 5:
            break
        hi, lo = win["High"].max(), win["Low"].min()
        depth = (hi - lo) / hi
        if depth <= 0.35:
            best = {"weeks": weeks, "depth_pct": round(float(depth) * 100, 1), "pivot": round(float(hi), 2)}
    vol10, vol50 = df["Volume"].iloc[-10:].mean(), df["Volume"].iloc[-50:].mean()
    dryup = float(vol10 / vol50) if vol50 else None
    out = {"base": best, "volume_10d_vs_50d": round(dryup, 2) if dryup else None}
    if best:
        out["pct_from_pivot"] = round((close.iloc[-1] / best["pivot"] - 1) * 100, 1)
        out["breaking_out"] = bool(close.iloc[-1] >= best["pivot"] * 0.995 and dryup and df["Volume"].iloc[-1] > 1.4 * vol50)
    return out


def analyse(df: pd.DataFrame, bench: pd.DataFrame) -> dict:
    if df.empty or len(df) < 260:
        return {"error": "less than ~1 year of price history"}
    c = df["Close"]
    sma = {n: float(c.rolling(n).mean().iloc[-1]) for n in (50, 150, 200)}
    sma200_rising = bool(c.rolling(200).mean().iloc[-1] > c.rolling(200).mean().iloc[-22])
    hi52, lo52 = float(c.iloc[-252:].max()), float(c.iloc[-252:].min())
    price = float(c.iloc[-1])
    b = bench["Close"].reindex(c.index).ffill() if not bench.empty else None
    rs = rs_score(c, b) if b is not None else None
    rs_line = (c / b) if b is not None else None
    rs_line_new_high = bool(rs_line.iloc[-1] >= rs_line.iloc[-252:].max() * 0.98) if rs_line is not None else None
    # Minervini trend template (8 checks) — a disciplined definition of "uptrend"
    template = {
        "price>150&200dma": price > sma[150] and price > sma[200],
        "150dma>200dma": sma[150] > sma[200],
        "200dma rising 1m": sma200_rising,
        "50dma>150&200": sma[50] > sma[150] and sma[50] > sma[200],
        "price>50dma": price > sma[50],
        ">=30% above 52w low": price >= lo52 * 1.30,
        "within 25% of 52w high": price >= hi52 * 0.75,
        "RS line near high": bool(rs_line_new_high),
    }
    return {
        "price": round(price, 2),
        "sma": {k: round(v, 2) for k, v in sma.items()},
        "high_52w": round(hi52, 2), "low_52w": round(lo52, 2),
        "pct_from_52w_high": round((price / hi52 - 1) * 100, 1),
        "returns_pct": {k: (round(_ret(c, d) * 100, 1) if _ret(c, d) is not None else None)
                         for k, d in (("1m", 21), ("3m", 63), ("6m", 126), ("12m", 252))},
        "rs_score_vs_benchmark": round(rs, 4) if rs is not None else None,
        "rs_line_near_high": rs_line_new_high,
        "weinstein": weinstein_stage(c),
        "trend_template_passed": int(sum(bool(v) for v in template.values())),
        "trend_template": {k: bool(v) for k, v in template.items()},
        **base_detection(df),
    }


def rs_percentiles(scores: dict[str, float | None]) -> dict[str, int | None]:
    """Rank RS scores across today's candidates (true universe ranking needs the event-study dataset)."""
    valid = {k: v for k, v in scores.items() if v is not None}
    if not valid:
        return {k: None for k in scores}
    arr = np.array(list(valid.values()))
    return {k: (int(round((arr <= v).mean() * 99)) if v is not None else None) for k, v in scores.items()}
