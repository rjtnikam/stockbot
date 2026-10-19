"""The timeline: did price follow the numbers, with what lag, and what's next.

Result dates are approximated (quarter end + 45 days, + 60 for March year-end)
because Screener's summary table doesn't carry announcement dates.
"""
from __future__ import annotations

from datetime import timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


def _qend(label: str) -> pd.Timestamp | None:
    try:
        return pd.Timestamp(pd.to_datetime(label, format="%b %Y")) + pd.offsets.MonthEnd(0)
    except Exception:
        return None


def _announce(qend: pd.Timestamp) -> pd.Timestamp:
    return qend + timedelta(days=60 if qend.month == 3 else 45)


def _at(series: pd.Series, when: pd.Timestamp) -> float | None:
    s = series[series.index >= when]
    return float(s.iloc[0]) if len(s) else None


def build(quarterly: dict, px: pd.DataFrame, bench: pd.DataFrame) -> dict:
    periods, yoy = quarterly.get("periods", []), quarterly.get("pat_yoy", [])
    if px.empty or not periods:
        return {"error": "no price or quarterly data"}
    c = px["Close"]
    b = bench["Close"].reindex(c.index).ffill() if not bench.empty else c * 0 + 1
    rel = c / b
    rows = []
    for i, p in enumerate(periods):
        qe = _qend(p)
        if qe is None:
            continue
        ann = _announce(qe)
        start, end = _at(rel, ann - timedelta(days=5)), _at(rel, ann + timedelta(days=90))
        rows.append({
            "quarter": p, "approx_result_date": ann.date().isoformat(),
            "pat_yoy": yoy[i] if i < len(yoy) else None,
            "rel_return_90d_after": (round(end / start - 1, 4) if start and end and ann + timedelta(days=90) <= c.index[-1] else None),
        })
    df = pd.DataFrame(rows)
    out: dict = {"quarters": rows}

    pair = df.dropna(subset=["pat_yoy", "rel_return_90d_after"])
    if len(pair) >= 4:
        agree = (np.sign(pair["pat_yoy"]) == np.sign(pair["rel_return_90d_after"])).mean()
        out["price_followed_numbers_share"] = round(float(agree), 2)
        # cross-correlation: does price react to growth with a lag of k quarters?
        lags = {}
        for k in range(0, 4):
            x = df["pat_yoy"].astype(float)
            y = df["rel_return_90d_after"].astype(float).shift(-k)
            m = x.notna() & y.notna()
            if m.sum() >= 4:
                lags[f"lag_{k}q"] = round(float(np.corrcoef(x[m], y[m])[0, 1]), 2)
        out["growth_to_price_correlation_by_lag"] = lags
        out["sample_quarters"] = int(len(pair))
        out["caution"] = "small sample (one stock, ~3 years); treat as a hint, not a base rate"

    last_qe = _qend(periods[-1])
    if last_qe is not None:
        next_qe = last_qe + pd.offsets.QuarterEnd(1)
        out["next_result_expected_around"] = _announce(next_qe).date().isoformat()
    return out


def chart(symbol: str, px: pd.DataFrame, tl: dict, path) -> str | None:
    if px.empty:
        return None
    d = px.iloc[-750:]
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=130)
    ax.plot(d.index, d["Close"], color="#2b2b2b", lw=1.3, label="Price")
    ax.plot(d.index, px["Close"].rolling(50).mean().reindex(d.index), color="#1D9E75", lw=1, label="50 DMA")
    ax.plot(d.index, px["Close"].rolling(200).mean().reindex(d.index), color="#7F77DD", lw=1, label="200 DMA")
    top = d["Close"].max()
    for q in tl.get("quarters", []):
        t = pd.Timestamp(q["approx_result_date"])
        if t < d.index[0] or q.get("pat_yoy") is None:
            continue
        col = "#1D9E75" if q["pat_yoy"] > 0 else "#D85A30"
        ax.axvline(t, color=col, alpha=0.35, lw=1, ls="--")
        ax.text(t, top * 1.02, f"{q['pat_yoy']*100:+.0f}%", color=col, fontsize=7, ha="center", rotation=90, va="bottom")
    ax.set_ylim(top=top * 1.14)
    ax.set_title(f"{symbol}: price vs results (dashed = approx result date, label = PAT YoY)", fontsize=10, pad=10)
    ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(alpha=0.15)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return str(path)
