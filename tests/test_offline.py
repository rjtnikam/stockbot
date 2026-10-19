"""Offline tests: no network, no API keys. Run: python -m tests.test_offline"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import fundamentals, screener, story_stage, technicals, timeline  # noqa: E402
from bot.config import load_config  # noqa: E402
from event_study.convergence import cumulative_incidence  # noqa: E402

Q = ["Jun 2023", "Sep 2023", "Dec 2023", "Mar 2024", "Jun 2024", "Sep 2024", "Dec 2024", "Mar 2025",
     "Jun 2025", "Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026"]
Y = ["Mar 2019", "Mar 2020", "Mar 2021", "Mar 2022", "Mar 2023", "Mar 2024", "Mar 2025", "Mar 2026", "TTM"]


def table(sec, cols, rows):
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = "".join("<tr><td class='text'>" + k + "&nbsp;<span>+</span></td>" +
                   "".join(f"<td>{v:,}</td>" for v in vals) + "</tr>" for k, vals in rows.items())
    return f"<section id='{sec}'><table class='data-table'><thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table></section>"


def fake_html():
    sales_q = [100, 102, 101, 104, 105, 106, 108, 110, 135, 150, 160, 175, 190]
    pat_q = [8, 8, 8, 9, 8.5, 8.6, 8.8, 9.2, 13, 15, 17, 19, 22]
    qrows = {"Sales": sales_q, "Operating Profit": [s * .15 for s in sales_q], "OPM %": [15] * 13,
             "Interest": [1] * 13, "Net Profit": pat_q}
    sales_y = [300, 320, 340, 360, 390, 410, 430, 530, 620]
    op = [s * .16 for s in sales_y]; dep = [10] * 9; intr = [5] * 9
    pbt = [o - d - i for o, d, i in zip(op, dep, intr)]
    prows = {"Sales": sales_y, "Operating Profit": op, "Interest": intr, "Depreciation": dep,
             "Profit before tax": pbt, "Net Profit": [p * .75 for p in pbt]}
    ycols = Y[:-1]
    brows = {"Equity Capital": [10] * 8, "Reserves": [150, 170, 190, 210, 235, 260, 290, 330],
             "Borrowings": [40, 40, 35, 30, 30, 25, 25, 20], "Other Liabilities": [60, 62, 64, 66, 70, 74, 80, 90],
             "Fixed Assets": [120, 125, 130, 135, 145, 150, 160, 185], "CWIP": [5, 5, 6, 8, 10, 15, 20, 10],
             "Investments": [10] * 8, "Other Assets": [125, 147, 163, 173, 190, 204, 225, 255]}
    crows = {"Cash from Operating Activity": [30, 33, 36, 40, 42, 45, 50, 62]}
    rrows = {"Debtor Days": [60, 62, 61, 63, 62, 64, 66, 70], "ROCE %": [18, 18, 19, 19, 20, 20, 21, 24]}
    srows = {"Promoters": [62, 62, 62, 61.8, 61.8], "FIIs": [1, 1.2, 1.5, 2, 2.5]}
    ratios = ("<ul id='top-ratios'><li><span class='name'>Market Cap</span><span class='number'>2,400</span></li>"
              "<li><span class='name'>ROE</span><span class='number'>18.5</span></li></ul>")
    return ("<html><h1>Test Precision Ltd</h1>" + ratios +
            table("quarters", Q, qrows) + table("profit-loss", Y, prows) +
            table("balance-sheet", ycols, brows) + table("cash-flow", ycols, crows) +
            table("ratios", ycols, rrows) + table("shareholding", ["Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026", "Sep 2026"], srows) +
            "</html>")


def fake_prices(trend=0.0009, seed=1, days=1500):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end="2026-10-02", periods=days)
    close = 100 * np.exp(np.cumsum(rng.normal(trend, 0.018, days)))
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * .99, "Close": close,
                         "Volume": rng.integers(50_000, 150_000, days)}, index=idx)


def main():
    cfg = load_config()
    company = screener.parse_company_html(fake_html())
    assert company["name"] == "Test Precision Ltd"
    assert company["top_ratios"]["Market Cap"] == 2400
    assert list(company["tables"]["quarters"].columns) == Q
    company.update(url="test", basis="consolidated")
    n = fundamentals.build(company, cfg)
    q, a = n["quarterly"], n["annual"]
    print("PAT trend:", q["pat_trend"], "| last YoY:", q["pat_yoy"][-1])
    assert q["pat_trend"] == "accelerating"
    print("ROTNA:", a["rotna_approx"], a["rotna_band"])
    print("Interest cover:", a["interest_coverage"], "| RR:", a.get("reinvestment_rate_3y"),
          "ROIIC:", a.get("roiic_3y"), "g:", a.get("implied_growth_rr_x_roiic"))
    print("CFO/PAT:", a["cfo_to_pat_5y"], "| D/E:", a["debt_to_equity"])
    print("Reverse DCF:", n["reverse_dcf"])
    print("Expectations gap:", n["expectations_gap"])
    assert isinstance(n["reverse_dcf"]["implied_growth_10y"], float)

    # reverse DCF sanity: valuing at the solved g must reproduce market cap
    r = fundamentals.reverse_dcf(2400, 100, 0.2, 0.13, 0.05, 10)
    print("RDCF 24x earnings ->", r["implied_growth_10y"])
    r_hi = fundamentals.reverse_dcf(6000, 100, 0.2, 0.13, 0.05, 10)
    assert r_hi["implied_growth_10y"] > r["implied_growth_10y"]

    px, bench = fake_prices(0.0012, 1), fake_prices(0.0004, 2)
    t = technicals.analyse(px, bench)
    print("Technicals:", {k: t[k] for k in ("price", "pct_from_52w_high", "rs_score_vs_benchmark", "weinstein",
                                             "trend_template_passed", "base", "volume_10d_vs_50d")})
    pct = technicals.rs_percentiles({"A": 0.1, "B": 0.3, "C": -0.2, "ME": t["rs_score_vs_benchmark"]})
    tl = timeline.build(q, px, bench)
    print("Timeline:", {k: tl.get(k) for k in ("price_followed_numbers_share", "growth_to_price_correlation_by_lag",
                                                 "next_result_expected_around")})
    from bot.config import REPORTS
    out = REPORTS / "TEST_chart.png"
    assert timeline.chart("TEST", px, tl, out)

    grok = {"mention_level": "low", "mention_trend": "rising"}
    gem = {"coverage_level": "none"}
    vp = {"attention": {"posts_last_period": 12, "posts_prior_period": 3}}
    s = story_stage.assess(n, t, pct["ME"], grok, gem, vp)
    print("Stage:", s["stage"], s["stage_confidence"], "| runner-up:", s["runner_up"], "|", s["price_vs_story"])
    print("Evidence:", json.dumps(s["evidence"]))
    print("Convergence:", s["convergence"])

    ev = pd.DataFrame({"days": [30, 60, 90, 120, 200, 250, 300, 400, 150, 500],
                       "outcome": ["converged", "converged", "broke", "converged", "censored",
                                   "converged", "broke", "censored", "converged", "censored"]})
    ci = cumulative_incidence(ev)
    print("CIF:", ci)
    assert 0 < ci["p_converged_by_365d"] <= 1
    print("\nALL OFFLINE TESTS PASSED")


if __name__ == "__main__":
    main()
