"""Offline test of the quarterly results reader: fake Screener Results feed + fake company pages.

Run: python -m tests.test_results
"""
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hunt.db as hdb  # noqa: E402

hdb.DB = Path(tempfile.mkdtemp()) / "test.db"
from bot import ai, telegram  # noqa: E402
from bot.config import load_config  # noqa: E402
from hunt import results  # noqa: E402
from tests.htmlcheck import check_html  # noqa: E402
from tests.test_offline import Y, table  # noqa: E402

TODAY = date(2026, 10, 16)


def quarters(last: str, n: int = 13) -> list[str]:
    d = results._qdate(last)
    out = []
    y, m = d.year, d.month
    for _ in range(n):
        out.append(date(y, m, 1).strftime("%b %Y"))
        m -= 3
        if m <= 0:
            m += 12
            y -= 1
    return out[::-1]


def company(name: str, last_q: str, sales_q, pat_q, opm_q, mcap=1500, promoters=60):
    Q = quarters(last_q)
    qrows = {"Sales": sales_q, "OPM %": opm_q, "Net Profit": pat_q, "EPS in Rs": [p / 10 for p in pat_q]}
    sales_y = [300, 320, 340, 360, 390, 410, 430, 530, 620]
    op = [s * .16 for s in sales_y]
    dep, intr = [10] * 9, [5] * 9
    pbt = [o - d - i for o, d, i in zip(op, dep, intr)]
    prows = {"Sales": sales_y, "Operating Profit": op, "Interest": intr, "Depreciation": dep,
             "Profit before tax": pbt, "Net Profit": [p * .75 for p in pbt]}
    yc = Y[:-1]
    brows = {"Equity Capital": [10] * 8, "Reserves": [150, 170, 190, 210, 235, 260, 290, 330],
             "Borrowings": [40, 40, 35, 30, 30, 25, 25, 20], "Other Liabilities": [60] * 8,
             "Fixed Assets": [120, 125, 130, 135, 145, 150, 160, 185], "CWIP": [5] * 8, "Investments": [10] * 8,
             "Other Assets": [125, 147, 163, 173, 190, 204, 225, 255]}
    ratios = (f"<ul id='top-ratios'><li><span class='name'>Market Cap</span><span class='number'>{mcap:,}</span></li>"
              "<li><span class='name'>Stock P/E</span><span class='number'>24.5</span></li></ul>")
    return (f"<html><h1>{name}</h1>{ratios}" + table("quarters", Q, qrows) + table("profit-loss", Y, prows) +
            table("balance-sheet", yc, brows) + table("cash-flow", yc, {"Cash from Operating Activity": [30, 33, 36, 40, 42, 45, 50, 62]}) +
            table("ratios", yc, {"Debtor Days": [60] * 8, "ROCE %": [18, 18, 19, 19, 20, 20, 21, 24]}) +
            table("shareholding", ["Sep 2025", "Dec 2025", "Mar 2026", "Jun 2026", "Sep 2026"], {"Promoters": [promoters] * 5}) +
            "</html>")


flat = [100] * 9
PAGES = {
    # you follow it (watchlist); page already shows the Sep 2026 quarter
    "FOLLOWCO": company("Follow Co Ltd", "Sep 2026", flat + [100, 104, 108, 118], [8] * 9 + [8, 8.5, 9, 10.5], [15] * 13),
    # strong: sales +60%, profit +150%, margin up; passes the gates -> signal for the scan
    "STRONGCO": company("Strong Co Ltd", "Sep 2026", flat + [100, 120, 140, 160], [6] * 9 + [6, 9, 12, 15], [14] * 9 + [15, 16, 18, 20]),
    # strong numbers but promoters at 20% -> fails a hard gate
    "PROMOCO": company("Promo Co Ltd", "Sep 2026", flat + [100, 130, 150, 170], [5] * 9 + [5, 8, 11, 14], [12] * 13, promoters=20),
    # Screener has not added the Sep quarter yet -> retry next run
    "STALECO": company("Stale Co Ltd", "Jun 2026", flat + [100, 101, 102, 103], [5] * 13, [12] * 13),
}


def item(slug, name, head, summary, when=None):
    t = f'<span class="ink-600 smaller"><time datetime="{when}">3h</time> ago</span>' if when else ""
    return (f'<div class="bordered rounded padding-12-18 announcement-item margin-top-12"><div class="flex">'
            f'<a class="sub-link" href="/company/{slug}/consolidated/"><span>{name}</span></a>'
            f'<a href="https://www.bseindia.com/x/{slug}.pdf">{head}<i class="icon-file-pdf"></i>{t}'
            f'<div class="sub">{summary}</div></a></div></div>')


FEED = ('<div class="card card-medium"><div class="sub margin-bottom-16">Today</div>'
        + item("STRONGCO", "Strong Co Ltd", "Financial Results For Sep 2026", "Q2 revenue up 60% YoY; PAT up 150%.", "2026-10-16T09:10:00+05:30")
        + item("PROMOCO", "Promo Co Ltd", "Financial Results", "Q2 sales rose 70%.", "2026-10-16T08:00:00+05:30")
        + '</div><div class="card card-medium"><div class="sub margin-bottom-16">Yesterday</div>'
        + item("FOLLOWCO", "Follow Co Ltd", "Outcome of Board Meeting", "Board approved Q2 FY27 results; revenue up 18%.")
        + item("STALECO", "Stale Co Ltd", "Financial Results", "Q2 results approved.")
        + item("WEAKCO", "Weak Co Ltd", "Financial Results", "Q2 revenue fell 20%; loss widened.")
        + item("NOTICECO", "Notice Co Ltd", "Board Meeting Intimation", "Board meeting on 30 Oct to consider Q2 results.")
        + "</div>")


class FakePage:
    url = ""


class FakeBr:
    def __init__(self):
        self.page = FakePage()
        self.opened = []

    def goto(self, url, wait="domcontentloaded"):
        self.page.url = url
        if "/announcements/results/" in url:
            return FEED
        m = re.search(r"/company/([^/]+)/", url)
        if m and m.group(1) in PAGES:
            self.opened.append(m.group(1))
            return PAGES[m.group(1)]
        return "<html><h1>?</h1></html>"


def fake_claude(system, messages, model=None, max_tokens=2000):
    text = messages if isinstance(messages, str) else messages[-1]["content"]
    assert "quarterly-result filings" in text
    items = []
    for line in text.splitlines():
        m = re.match(r"^(\d+) \| ([^|]+)\|", line)
        if m:
            name = m.group(2)
            tone = "weak" if "Weak" in name else "strong" if ("Strong" in name or "Promo" in name) else "unclear"
            items.append({"i": int(m.group(1)), "tone": tone, "sales_yoy": 0.6 if tone == "strong" else None,
                          "profit_yoy": None, "is_result": "Notice" not in name})
    import json
    return json.dumps({"items": items})


def main():
    cfg = load_config()
    ai.claude = fake_claude
    assert results.expected_quarter(date(2026, 10, 16)) == "Sep 2026"
    assert results.expected_quarter(date(2027, 1, 20)) == "Dec 2026"
    assert results.expected_quarter(date(2027, 5, 28)) == "Mar 2027"

    br = FakeBr()
    out = results.run(br, cfg, {"FOLLOWCO"}, today=TODAY)
    print(telegram.plain(out["digest"]))
    print("\nstats:", out["stats"], "| pages opened:", br.opened)
    check_html(out["digest"])
    d = out["digest"]
    assert "WEAKCO" not in br.opened and "NOTICECO" not in br.opened, "weak / notice items must not cost a page"
    assert br.opened[0] == "FOLLOWCO", "your stocks are opened first"
    assert "<b>Stocks you follow</b>" in d and "Follow Co Ltd" in d and "Sep 2026 quarter" in d
    assert "🟢 <b>Strong results</b>" in d and "Strong Co Ltd" in d
    assert "Strong, but failed a quality check" in d and "promoter stake low" in d
    assert "Stale Co" not in d and out["stats"]["pending"] == 1
    sig = [s for s in hdb.signals_since("2026-01-01") if s["signal"] == "strong_result"]
    assert sorted(s["slug"] for s in sig) == ["FOLLOWCO", "STRONGCO"], sig  # strong + passed the gates; not PROMOCO

    # next run: Screener has updated Stale Co -> it is read now
    PAGES["STALECO"] = company("Stale Co Ltd", "Sep 2026", flat + [100, 101, 102, 103], [5] * 9 + [5, 5, 5, 4], [12] * 13)
    br2 = FakeBr()
    results.run(br2, cfg, {"FOLLOWCO"}, today=TODAY + timedelta(days=1))
    print("\nsecond run opened:", br2.opened)
    assert br2.opened == ["STALECO"], br2.opened
    lst = results.list_recent(5)
    check_html(lst)
    assert "Weak Co Ltd" in lst and "Notice Co" not in lst
    blk = results.block(hdb.latest_result("STRONGCO"))
    check_html(blk)
    assert "Sales ₹160 Cr (+60% YoY, +14% QoQ)" in blk, blk
    print("\nALL RESULTS TESTS PASSED")


if __name__ == "__main__":
    main()
