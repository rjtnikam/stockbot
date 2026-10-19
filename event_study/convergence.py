"""Base rates: when the story runs ahead of price, how often and how fast does price catch up?

Step 1 (slow, run overnight once a quarter):
    python -m event_study.convergence --build        # reads Screener pages for data/universe.txt
Step 2:
    python -m event_study.convergence --analyse      # writes data/base_rates.json

EVENT  = two consecutive quarters of PAT YoY >= 25% after four quarters averaging < 10%
         (an earnings inflection), while the stock's 6-month return lagged the benchmark.
OUTCOMES (competing, measured from the approx result date):
  converged  = stock outperforms the benchmark by >= 20% from the event date
  broke      = a later quarter prints negative PAT YoY before convergence
  censored   = neither happened by the end of the data
Cumulative incidence (competing-risks Kaplan-Meier) gives P(converged by day d).

KNOWN BIASES (stated in the output): Screener pages show ~13 quarters, so this covers
roughly the last 3 years only, i.e. one market regime; companies that delisted or
collapsed are missing (survivorship bias), which makes convergence look likelier.
Treat early numbers as a first estimate and widen the universe/history over time.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import fundamentals, prices, screener  # noqa: E402
from bot.config import BASE_RATES_FILE, DATA, load_config  # noqa: E402
from bot.session import browser_session  # noqa: E402
from bot.timeline import _announce, _qend  # noqa: E402

log = logging.getLogger("event_study")
STORE = DATA / "event_study"
STORE.mkdir(parents=True, exist_ok=True)
HORIZONS = (90, 180, 365)


def build(cfg):
    syms = [l.strip().upper() for l in (DATA / "universe.txt").read_text().splitlines()
            if l.strip() and not l.startswith("#")]
    with browser_session(tuple(cfg["limits"]["page_delay_seconds"]), require_login=False) as br:
        for s in syms:
            out = STORE / f"{s}.json"
            if out.exists():
                continue
            try:
                c = screener.fetch_company(br, s)
                q = fundamentals.quarterly(c["tables"]["quarters"])
                out.write_text(json.dumps({"symbol": s, "mcap": c["top_ratios"].get("Market Cap"), "quarterly": q}))
            except Exception as e:
                log.warning("%s: %s", s, e)


def _bucket(mcap):
    return ("micro (<1,000 Cr)" if mcap and mcap < 1000 else
            "small (1,000-10,000 Cr)" if mcap and mcap < 10000 else "mid+ (>10,000 Cr)")


def find_events(rec: dict, px: pd.DataFrame, bench: pd.Series) -> list[dict]:
    q = rec["quarterly"]
    periods, yoy = q.get("periods", []), q.get("pat_yoy", [])
    rel = px["Close"] / bench.reindex(px.index).ffill()
    events = []
    for i in range(5, len(periods)):
        y = yoy[i - 5:i + 1]
        if any(v is None for v in y):
            continue
        if not (y[-1] >= 0.25 and y[-2] >= 0.25 and np.mean(y[:4]) < 0.10):
            continue
        qe = _qend(periods[i])
        if qe is None:
            continue
        a = _announce(qe)
        r = rel[rel.index >= a]
        r6 = rel[rel.index <= a - timedelta(days=182)]
        if len(r) < 5 or not len(r6):
            continue
        if r.iloc[0] / r6.iloc[-1] - 1 > 0:      # price already led -> not a lagging setup
            continue
        path = r / r.iloc[0] - 1
        hit = path[path >= 0.20]
        conv_day = (hit.index[0] - a).days if len(hit) else None
        broke_day = None
        for j in range(i + 1, len(periods)):
            if yoy[j] is not None and yoy[j] < 0:
                broke_day = (_announce(_qend(periods[j])) - a).days
                break
        end_day = (r.index[-1] - a).days
        if conv_day is not None and (broke_day is None or conv_day <= broke_day):
            outcome, t = "converged", conv_day
        elif broke_day is not None and broke_day <= end_day:
            outcome, t = "broke", broke_day
        else:
            outcome, t = "censored", end_day
        events.append({"symbol": rec["symbol"], "quarter": periods[i], "bucket": _bucket(rec.get("mcap")),
                       "outcome": outcome, "days": int(t)})
        break  # one event per stock per inflection run
    return events


def cumulative_incidence(ev: pd.DataFrame) -> dict:
    """Aalen-Johansen estimate with two competing outcomes."""
    times = sorted(ev["days"].unique())
    surv, cif = 1.0, {"converged": 0.0, "broke": 0.0}
    curve = []
    for t in times:
        at_risk = (ev["days"] >= t).sum()
        if at_risk == 0:
            break
        d = ev[ev["days"] == t]["outcome"].value_counts()
        for k in cif:
            cif[k] += surv * d.get(k, 0) / at_risk
        surv *= 1 - (d.get("converged", 0) + d.get("broke", 0)) / at_risk
        curve.append((t, cif["converged"], cif["broke"]))
    out = {"n_events": int(len(ev)),
           "n_converged": int((ev.outcome == "converged").sum()),
           "n_broke": int((ev.outcome == "broke").sum()),
           "n_censored": int((ev.outcome == "censored").sum())}
    for h in HORIZONS:
        pts = [c for c in curve if c[0] <= h]
        out[f"p_converged_by_{h}d"] = round(pts[-1][1], 3) if pts else 0.0
        out[f"p_story_broke_by_{h}d"] = round(pts[-1][2], 3) if pts else 0.0
    conv = ev[ev.outcome == "converged"]["days"]
    out["median_days_when_converged"] = int(conv.median()) if len(conv) else None
    return out


def analyse(cfg):
    bench_df = prices.benchmark_history(cfg)
    bench = bench_df["Close"]
    events = []
    for f in STORE.glob("*.json"):
        rec = json.loads(f.read_text())
        px = prices.stock_history(rec["symbol"], 6)
        if px.empty:
            continue
        events += find_events(rec, px, bench)
    ev = pd.DataFrame(events)
    if ev.empty:
        print("No events found. Grow data/universe.txt.")
        return
    ev.to_csv(DATA / "event_study_events.csv", index=False)
    buckets = {"story ahead of price|all": cumulative_incidence(ev)}
    for b, g in ev.groupby("bucket"):
        if len(g) >= 15:   # don't publish odds from tiny samples
            buckets[f"story ahead of price|{b}"] = cumulative_incidence(g)
    BASE_RATES_FILE.write_text(json.dumps({
        "definition": __doc__.split("Cumulative")[0].strip(),
        "biases": "~3 years of quarters (one regime); survivorship bias inflates convergence odds",
        "buckets": buckets}, indent=1))
    print(json.dumps(buckets, indent=1))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--analyse", action="store_true")
    a = ap.parse_args()
    cfg = load_config()
    if a.build:
        build(cfg)
    if a.analyse:
        analyse(cfg)
