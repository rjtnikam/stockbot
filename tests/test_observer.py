"""Offline test of the evening filing observer. Run: python -m tests.test_observer"""
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import ai, telegram  # noqa: E402
from bot.config import load_config  # noqa: E402
from hunt import db, observer  # noqa: E402
from tests.htmlcheck import check_html  # noqa: E402
from tests.test_results import company, flat  # noqa: E402


def item(slug, name, head, summary):
    return (f'<div class="bordered rounded padding-12-18 announcement-item margin-top-12"><div class="flex">'
            f'<a class="sub-link" href="/company/{slug}/"><span>{name}</span></a>'
            f'<a href="https://www.bseindia.com/x/{slug}-{abs(hash(head)) % 9999}.pdf">{head}<i class="icon-file-pdf"></i>'
            f'<div class="sub">{summary}</div></a></div></div>')


FEED = ('<div class="card card-medium"><div class="sub margin-bottom-16">Today</div>'
        + item("AUDCO", "Audit Co Ltd", "Resignation of Statutory Auditor", "Statutory auditor resigned citing unpaid fees.")
        + item("ORDCO", "Order Co Ltd", "Bagging/Receiving of orders/contracts", "Won Rs 120 crore railway order; order book Rs 400 crore.")
        + item("FOLLOWCO", "Follow Co Ltd", "Appointment of Chief Financial Officer", "Appointed a new CFO from a large pharma company.")
        + item("JUNKCO", "Junk Co Ltd", "Closure of Trading Window", "Trading window closed.")
        + item("SMALLCO", "Small Co Ltd", "Receipt of order", "Received order worth Rs 0.5 crore.")
        + "</div>")
PAGES = {"ORDCO": company("Order Co Ltd", "Sep 2026", flat + [100, 110, 120, 130], [8] * 9 + [8, 9, 10, 11], [15] * 13),
         "FOLLOWCO": company("Follow Co Ltd", "Sep 2026", flat + [100, 104, 108, 118], [8] * 13, [15] * 13)}
IMP = {"Audit Co": 9, "Order Co": 8, "Follow Co": 5, "Small Co": 3}
TAGS = {"Audit Co": ["auditor_exit"], "Order Co": ["order_win"], "Follow Co": ["new_leadership"], "Small Co": ["order_win"]}


class FakePage:
    url = ""


class FakeBr:
    def __init__(self):
        self.page = FakePage()

    def goto(self, url, wait="domcontentloaded"):
        self.page.url = url
        if "/announcements/" in url:
            return FEED
        m = re.search(r"/company/([^/]+)/", url)
        return PAGES.get(m.group(1), "<html></html>") if m else "<html></html>"


def fake_claude(system, messages, model=None, max_tokens=2000):
    text = messages if isinstance(messages, str) else messages[-1]["content"]
    if '"importance"' in text and "LINES:" in text:  # the reader (Haiku)
        items = []
        for line in text.split("LINES:")[1].splitlines():
            m = re.match(r"^(\d+) \| ([^|]+)\|", line)
            if m:
                score = next((v for k, v in IMP.items() if k in m.group(2)), 1)
                tags = next((v for k, v in TAGS.items() if k in m.group(2)), [])
                items.append({"i": int(m.group(1)), "tags": tags, "importance": score, "why": f"score {score}",
                              "note": m.group(2).strip(), "change": score >= 6})
        return json.dumps({"items": items})
    if "Pick the ones Rajat must read" in text:  # the editor (Sonnet)
        idx = {}
        for line in text.split("FILINGS:")[1].splitlines():
            m = re.match(r"^(\d+) \| ([^(]+)\(", line)
            if m:
                idx[m.group(2).strip()] = int(m.group(1))
        assert "Small Co Ltd" not in idx, "a low-score filing must not reach the editor"
        picks = [{"i": idx["Audit Co Ltd"], "section": "red", "score": 9, "what": "Auditor resigned; cites unpaid fees.",
                  "why": "Auditor exits often come before accounting trouble."},
                 {"i": idx["Order Co Ltd"], "section": "story", "score": 8, "what": "₹120 Cr railway order, about 25% of yearly sales.",
                  "why": "One order adds a quarter of a year's sales."},
                 {"i": idx["Follow Co Ltd"], "section": "follow", "score": 5, "what": "New CFO from a large pharma company.",
                  "why": "Better finance control."}]
        return json.dumps({"picks": picks, "summary": "One red flag and one big order."})
    raise AssertionError("unexpected Claude call: " + text[:100])


def fake_grok(system, user, search=True):
    i = int(re.search(r"^(\d+) \|", user.split("COMPANIES:")[1].strip(), re.M).group(1))
    return json.dumps({"notes": [{"i": i, "x": "no posts found"}]})


def main():
    ai.claude, ai.grok = fake_claude, fake_grok
    cfg = load_config()
    cfg["hunt"]["filings"]["source"] = "screener"
    out = observer.run(FakeBr(), cfg, {"FOLLOWCO"}, today=date.today())
    d = out["digest"]
    print(telegram.plain(d))
    check_html(d)
    assert "🔴 <b>Red flags</b>" in d and "Audit Co Ltd" in d
    assert "🟢 <b>Business changes</b>" in d and "Order Co Ltd" in d and "✅ quality checks" in d and "🐦 X: no posts found" in d
    assert "👀 <b>Stocks you follow</b>" in d and "Follow Co Ltd" in d
    assert "Junk Co" not in d and "Small Co" not in d
    assert "Passed the quality checks: Order Co Ltd" in d
    sig = [s for s in db.signals_since("2026-01-01") if s["slug"] == "ORDCO"]
    assert sig and sig[0]["detail"].get("importance") == 8, sig

    # the same evening again: nothing is sent twice
    out2 = observer.run(FakeBr(), cfg, {"FOLLOWCO"}, today=date.today())
    print("\nsecond run:\n" + telegram.plain(out2["digest"]))
    assert "Nothing important today." in out2["digest"]
    print("\nALL OBSERVER TESTS PASSED")


if __name__ == "__main__":
    main()
