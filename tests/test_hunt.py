"""End-to-end offline test of the hunter: fake BSE feed + fake Screener, no network, no API keys.

Run: python -m tests.test_hunt
"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hunt.db as hdb  # noqa: E402

hdb.DB = Path(tempfile.mkdtemp()) / "test.db"  # never touch the real database
import bot.store as bstore  # noqa: E402
bstore.DB = hdb.DB

from bot.config import load_config  # noqa: E402
from hunt import documents, runner, scoring  # noqa: E402
from hunt.http import Http  # noqa: E402
from tests.test_offline import fake_prices, table  # noqa: E402

TODAY = date.today()
Q = [(TODAY.replace(day=1) - timedelta(days=30 * i)).strftime("%b %Y") for i in range(13)][::-1]
Y = [f"Mar {TODAY.year - 8 + i}" for i in range(8)] + ["TTM"]


def company_html(name, sales_q, pat_q, opm_q, sales_y, fa_y, cwip_y, mcap, concalls=0, debt_y=None):
    eps_q = [p / 10 for p in pat_q]
    qrows = {"Sales": sales_q, "OPM %": opm_q, "Net Profit": pat_q, "EPS in Rs": eps_q}
    op = [s * .18 for s in sales_y]
    dep = [max(f * .06, 1) for f in fa_y] + [fa_y[-1] * .06]
    intr = [2] * 9
    pbt = [o - d - i for o, d, i in zip(op, dep, intr)]
    prows = {"Sales": sales_y, "Operating Profit": op, "Interest": intr, "Depreciation": dep,
             "Profit before tax": pbt, "Net Profit": [p * .75 for p in pbt]}
    yc = Y[:-1]
    debt_y = debt_y or [20] * 8
    res = [100 + 25 * i for i in range(8)]
    brows = {"Equity Capital": [10] * 8, "Reserves": res, "Borrowings": debt_y,
             "Other Liabilities": [50] * 8, "Fixed Assets": fa_y, "CWIP": cwip_y, "Investments": [5] * 8,
             "Other Assets": [80 + 10 * i for i in range(8)]}
    crows = {"Cash from Operating Activity": [30 + 8 * i for i in range(8)]}
    docs = "".join(f"<li><div class='ink-600'>{Q[-1 - (2 * i) % 13]}</div><a class='concall-link' href='https://www.bseindia.com/t{i}.pdf'>Transcript</a></li>"
                   for i in range(concalls))
    ratios = (f"<ul id='top-ratios'><li><span class='name'>Market Cap</span><span class='number'>{mcap:,}</span></li>"
              "<li><span class='name'>ROE</span><span class='number'>19</span></li></ul>")
    return (f"<html><h1>{name}</h1>{ratios}" + table("quarters", Q, qrows) + table("profit-loss", Y, prows) +
            table("balance-sheet", yc, brows) + table("cash-flow", yc, crows) +
            table("ratios", yc, {"Debtor Days": [60] * 8}) +
            table("shareholding", Q[-4:], {"Promoters": [60, 60, 61, 61]}) +
            f"<section id='documents'><div class='documents concalls'><ul class='list-links'>{docs}</ul></div></section></html>")


flat = [100] * 9
COMPANIES = {
    # capacity doubled over 2 years, revenue flat until the latest quarters -> conversion starting
    "CAPEXCO": company_html("Capex Co Ltd", [90, 92, 91, 93, 95, 94, 96, 97, 99, 100, 112, 128, 150],
                            [6, 6, 6, 6, 6.2, 6.1, 6.3, 6.4, 6.5, 7, 8.5, 10, 13], [14] * 9 + [15, 16, 17, 19],
                            [300, 320, 340, 350, 360, 370, 380, 390, 489], [150, 155, 160, 165, 170, 180, 260, 330],
                            [5, 5, 5, 8, 10, 80, 90, 40], 2400, concalls=1),
    "INFLECT": company_html("Inflect Industries", [100, 101, 102, 103, 104, 106, 108, 110, 112, 120, 130, 140, 155],
                            [8, 8, 8, 8, 8.2, 8.4, 8.6, 8.8, 9, 11, 13, 15, 18], [15] * 9 + [17, 18, 19, 20],
                            [380, 390, 400, 410, 420, 430, 440, 470, 545], [200] * 8, [5] * 8, 3100, concalls=6),
    # shell revival: no sales for years, then a jump (the Kalind pattern) -> must be stopped by the gates
    "KALIND": company_html("Kalind Ltd", [0] * 9 + [30, 33, 33, 35], [0] * 9 + [5, 9, 12, 15], [0] * 9 + [55, 60, 62, 63],
                           [0, 0, 0, 0, 0, 0, 0, 40, 101], [5] * 6 + [60, 200], [0] * 8, 900),
    "SPAMCO": company_html("Spam Co", [50] * 13, [2] * 13, [8] * 13, [200] * 9, [100] * 8, [2] * 8, 600),
}

MASTER = [
    {"SCRIP_CD": "543210", "Scrip_Name": "Capex Co Ltd", "ISIN_NUMBER": "INE000A01001", "Mktcap": "2400", "scrip_id": "CAPEXCO"},
    {"SCRIP_CD": "543211", "Scrip_Name": "Spam Co", "ISIN_NUMBER": "INE000A01002", "Mktcap": "600", "scrip_id": "SPAMCO"},
    {"SCRIP_CD": "543212", "Scrip_Name": "Bad Co", "ISIN_NUMBER": "INE000A01003", "Mktcap": "900", "scrip_id": "BADCO"},
    {"SCRIP_CD": "539730", "Scrip_Name": "Fredun Pharmaceuticals", "ISIN_NUMBER": "INE000A01004", "Mktcap": "700", "scrip_id": "FREDUN"},
    {"SCRIP_CD": "500001", "Scrip_Name": "Giant Corp", "ISIN_NUMBER": "INE000A01005", "Mktcap": "250000", "scrip_id": "GIANT"},
] + [{"SCRIP_CD": str(600000 + i), "Scrip_Name": f"Filler {i}", "ISIN_NUMBER": f"INE9{i:05d}", "Mktcap": "1000"} for i in range(1600)]
NSE_CSV = "SYMBOL,NAME OF COMPANY, SERIES, ISIN NUMBER\nCAPEXCO,Capex Co Ltd,EQ,INE000A01001\nSPAMCO,Spam Co,EQ,INE000A01002\nBADCO,Bad Co,EQ,INE000A01003\nGIANT,Giant,EQ,INE000A01005\n"


def ann(i, code, name, headline, subcat="", attach="x.pdf"):
    return {"NEWSID": f"N{i}", "SCRIP_CD": int(code), "SLONGNAME": name, "HEADLINE": headline, "NEWSSUB": f"{name} - {code} - {subcat}",
            "SUBCATNAME": subcat, "CATEGORYNAME": None, "MORE": "", "ATTACHMENTNAME": attach,
            "NEWS_DT": TODAY.isoformat() + "T10:00:00", "TotalPageCnt": 1}


TODAY_FILINGS = [
    ann(1, "543210", "Capex Co Ltd", "Commencement of commercial production at the new Dahej plant", "Commencement of Commercial Production"),
    ann(2, "543210", "Capex Co Ltd", "Intimation of Earnings Conference Call for Q2 FY27", "Earnings Call"),
    ann(3, "543210", "Capex Co Ltd", "Capex Co bags order worth Rs. 150 Crore from a leading OEM", "Award of Order / Receipt of Order"),
    ann(4, "543212", "Bad Co", "Resignation of Statutory Auditor M/s XYZ & Associates", "Resignation of Statutory Auditors"),
    ann(5, "539730", "Fredun Pharmaceuticals", "Resignation of Mr A as Chief Financial Officer", "Change in Management"),
    ann(6, "500001", "Giant Corp", "Giant bags order worth Rs 5,000 crore", "Award of Order"),  # outside band, not watchlist
    ann(7, "543211", "Spam Co", "Closure of Trading Window", "Trading Window"),
    ann(8, "543211", "Spam Co", "Spam Co receives order worth Rs 1.2 crore", "Award of Order"),
]


def fake_get(self, url, params=None, kind="json", referer=None, warm=None):
    params = params or {}
    if "ListofScripData" in url:
        return MASTER
    if "EQUITY_L" in url:
        return NSE_CSV
    if "AnnSubCategoryGetData" in url:
        if params.get("strScrip"):
            return {"Table": [ann(99, params["strScrip"], "x", "Outcome of board meeting")], "Table1": [{"ROWCNT": 1}]}
        return {"Table": TODAY_FILINGS, "Table1": [{"ROWCNT": len(TODAY_FILINGS)}]}
    raise RuntimeError(f"unexpected url {url}")


Http.get = fake_get


def screen_html(rows):
    body = "".join(f"<tr><td>{i}.</td><td><a href='/company/{s}/consolidated/'>{n}</a></td><td>{m:,}</td><td>40.0</td></tr>"
                   for i, (s, n, m) in enumerate(rows, 1))
    return ("<table class='data-table'><thead><tr><th>S.No.</th><th>Name</th><th>Mar Cap Rs.Cr.</th><th>Qtr Sales Var %</th></tr></thead>"
            f"<tbody>{body}<tr><td></td><td>Median: {len(rows)} Co.</td><td>0</td><td>0</td></tr></tbody></table>")


HITS = {
    "YOY Quarterly sales growth > 30": [("CAPEXCO", "Capex Co Ltd", 2400), ("INFLECT", "Inflect Industries", 3100),
                                        ("KALIND", "Kalind Ltd", 900)],
    "OPM latest quarter": [("INFLECT", "Inflect Industries", 3100), ("CAPEXCO", "Capex Co Ltd", 2400),
                           ("KALIND", "Kalind Ltd", 900)],
    "EPS latest quarter > 1.3": [("INFLECT", "Inflect Industries", 3100), ("KALIND", "Kalind Ltd", 900)],
    "YOY Quarterly profit growth": [("KALIND", "Kalind Ltd", 900)],
    "Capital work in progress": [("CAPEXCO", "Capex Co Ltd", 2400)],
}


class FakePage:
    url = ""

    def inner_text(self, _):
        return "[]"


class FakeBr:
    def __init__(self):
        self.page = FakePage()

    def goto(self, url, wait="domcontentloaded"):
        from urllib.parse import parse_qs, urlparse
        self.page.url = url
        if "/screen/raw/" in url:
            qs = parse_qs(urlparse(url).query)
            q, page = qs["query"][0], int(qs.get("page", ["1"])[0])
            if "Gross block preceding year" in q:
                return "<html><div class='alert'>Invalid query: unknown ratio 'Gross block preceding year'</div></html>"
            rows = next((v for k, v in HITS.items() if k in q), [])
            return screen_html(rows if page == 1 else rows)
        for slug, html in COMPANIES.items():
            if f"/company/{slug}/" in url:
                return html
        return "<html><h1>?</h1></html>"


def main():
    cfg = load_config()
    cfg["hunt"]["filings"]["source"] = "bse"  # this test fakes the BSE feed
    # history before today: capex announced 200 days ago; Spam Co's stream of tiny orders
    hdb.add_signal("CAPEXCO", "Capex Co Ltd", "capex_announced", 1.5, (TODAY - timedelta(days=200)).isoformat(),
                   {"headline": "Board approves 2x capacity expansion"}, "https://bse/capex.pdf", "old1")
    for i, d in enumerate((10, 25, 40, 60, 75)):
        hdb.add_signal("SPAMCO", "Spam Co", "order_win", 1.0, (TODAY - timedelta(days=d)).isoformat(),
                       {"order_cr": 0.8 + i * 0.1}, None, f"spam{i}")

    br = FakeBr()
    res = runner.run(br, cfg, watchlist=["539730"], dry=True)
    print(res.board)
    print("\nstats:", {k: v for k, v in res.stats.items() if k != "universe"})

    f = res.stats["filings"]
    assert f["out_of_band"] == 1 and f["junk"] == 1, f
    assert f["tagged"].get("first_concall") == 1, f["tagged"]
    ranked = scoring.score_all(cfg, TODAY, {"539730"},
                               confirm={x["slug"]: x["deep_check"] for x in res.finalists})
    by = {r["slug"]: r for r in ranked}
    cap = by["CAPEXCO"]
    print("\nCAPEXCO setups:", cap["setups"], "| stage hint:", cap["stage_hint"], "| score", cap["score"])
    print("CAPEXCO capex check:", res.finalists[0]["deep_check"]["capex"])
    assert "Capex → revenue conversion" in cap["setups"]
    assert any(s.startswith("Coming-out") for s in cap["setups"])
    assert any(s.startswith("Material order") for s in cap["setups"]), cap["setups"]
    assert "Earnings inflection" in by["INFLECT"]["setups"]
    assert by["BADCO"]["excluded"] and "statutory auditor resigned" in by["BADCO"]["kill_reasons"]
    assert any("tiny order" in c["label"] for c in by["SPAMCO"]["components"])
    assert "GIANT" not in by
    assert ranked[0]["slug"] == "CAPEXCO"
    assert "🪳 <b>Warnings</b>" in res.board and "Fredun" in res.board
    assert "gross_block_expansion" in res.board
    assert res.finalists and res.finalists[0]["company"] is not None

    # the gates: Kalind (shell revival) is stopped before any paid call; the board says why
    by_all = {r["slug"]: r for r in scoring.score_all(cfg, TODAY, set(), confirm={s: hdb.doc_get(f"deep:{s}") for s in ("KALIND", "CAPEXCO", "INFLECT") if hdb.doc_get(f"deep:{s}")})}
    assert by_all["KALIND"]["excluded"] and any("real business" in k for k in by_all["KALIND"]["kill_reasons"]), by_all["KALIND"]
    assert "KALIND" not in [f["slug"] for f in res.finalists]
    assert "Stopped by the quality checks" in res.board and "Kalind" in res.board
    assert "no real sales history" in res.board  # plain words, not internal names
    growth = sum(c["points"] for c in by_all["KALIND"]["components"] if c.get("bucket") == "growth")
    assert growth <= 3.0 + 1e-9, growth  # 4 growth queries count as one event

    # documents adjust the score: promoter warrants at a premium + strong say-do management
    docs = {"CAPEXCO": {"filings": [{"tag": "warrants_preferential", "date": TODAY.isoformat(), "headline": "x", "url": "u",
                                     "extracted": {"expensive_signal": True, "promoter_share_of_issue_pct": 80, "n_allottees": 3}}],
                        "management": {"say_do_score": 8, "candor_score": 8, "expensive_signals": [{"signal": "a"}, {"signal": "b"}],
                                       "guidance_policy": "specific_numeric"}}}
    r2 = {r["slug"]: r for r in scoring.score_all(cfg, TODAY, set(), confirm={x["slug"]: x["deep_check"] for x in res.finalists}, docs=docs)}
    print("\nCAPEXCO with documents:", r2["CAPEXCO"]["score"], "vs", cap["score"], r2["CAPEXCO"]["setups"])
    assert r2["CAPEXCO"]["score"] > cap["score"] + 4 and "Skin in the game" in r2["CAPEXCO"]["setups"]

    # the thesis queue: watchlist with new filings first, then hunt finalists
    import run_daily
    run_daily.read_watchlist = lambda cfg: ["539730"]  # never depend on your real watchlist file
    q = run_daily.build_queue(cfg, cfg["limits"], res, [])
    print("\nqueue:", [(s, src) for s, src, _ in q])
    assert q[0][0] == "539730" and q[1][0] == "CAPEXCO" and "SPAMCO" not in [x[0] for x in q]

    # dry thesis on a hunted stock (company page reused, prices faked)
    from bot import prices
    prices.stock_history = lambda s, y=6, codes=None: fake_prices(0.0012, 3)
    out = run_daily.analyse_stock(br, "CAPEXCO", "hunt", cfg, fake_prices(0.0004, 2), {}, True, hunt=res.finalists[0])
    print("\n" + out["memo"])
    assert "Hunt score" in out["memo"]

    # document reader plumbing (Gemini + PDF download mocked)
    calls = []

    def fake_ask(prompt, pdfs):
        calls.append((prompt[:40], len(pdfs)))
        if "MANAGEMENT" in prompt:
            return {"guidance_policy": "specific_numeric", "say_do_score": 8, "candor_score": 7,
                    "expensive_signals": [{"signal": "capex before demand", "quote": "we built ahead"}]}
        return {"order_value_cr": 150, "customer": "OEM", "capacity": "2x"}

    documents._ask = fake_ask
    documents.fetch_pdf = lambda http, url: b"%PDF-1.4 fake"
    d = documents.read_all(Http(br), "CAPEXCO", "Capex Co Ltd", ["https://www.bseindia.com/t0.pdf"])
    print("\ndocuments read:", [(f["tag"], f["extracted"]) for f in d["filings"]], "| mgmt:", d["management"]["say_do_score"])
    assert {f["tag"] for f in d["filings"]} == {"commissioning", "order_win"} and d["management"]["say_do_score"] == 8
    d2 = documents.read_all(Http(br), "CAPEXCO", "Capex Co Ltd", ["https://www.bseindia.com/t0.pdf"])
    assert len(calls) == 3 and d2["management"]["say_do_score"] == 8, calls  # second read comes from cache
    print("\nALL HUNT TESTS PASSED")


if __name__ == "__main__":
    main()
