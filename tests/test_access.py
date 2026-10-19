"""Offline test of the proof-of-access log. Run: python -m tests.test_access"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot import access, cards, screener, session  # noqa: E402
from hunt import runner  # noqa: E402
from tests.htmlcheck import check_html  # noqa: E402
from tests.test_offline import fake_html  # noqa: E402


class Resp:
    def __init__(self, status):
        self.status = status


class FakePage:
    """Behaves like a Playwright page: goto() may redirect to the login page or fail."""
    def __init__(self, logged_in=True):
        self.url, self.logged_in, self.html = "", logged_in, ""

    def goto(self, url, wait_until=None, timeout=None):
        if "broken" in url:
            raise TimeoutError("Timeout 60000ms exceeded")
        if not self.logged_in and ("/dash/" in url or "/screen/" in url or "marketsmith" in url):
            self.url = "https://www.screener.in/login/?next=/dash/"
            return Resp(200)
        self.url = url
        self.html = fake_html() if "/company/" in url else "<html><body>ok</body></html>"
        return Resp(200)

    def content(self):
        return self.html


def main():
    access.start_run("test run")
    br = session.Browser(FakePage(logged_in=True), (0, 0))
    assert session.screener_logged_in(br) is True
    comp = screener.fetch_company(br, "TESTP")
    assert comp["fetched_at"] and comp["basis"] == "consolidated"
    try:
        br.goto("https://www.screener.in/company/broken/")
    except TimeoutError:
        pass

    acc = access.summary(run=access.RUN["id"])
    s = acc["screener"]
    print("summary:", {k: v for k, v in s.items() if k != "data"})
    assert s["login"] is True and s["pages"] == 3 and s["ok"] == 2 and s["failed"] == 1
    company = [r for r in s["data"] if r["kind"] == "company"][0]
    assert company["target"] == "TESTP" and "13 quarters up to Jun 2026" in company["detail"], company

    # logged out: the log must say so (this is the proof that data was NOT read)
    br2 = session.Browser(FakePage(logged_in=False), (0, 0))
    assert session.screener_logged_in(br2) is False
    acc = access.summary(run=access.RUN["id"])
    assert acc["screener"]["login"] is False and acc["screener"]["logged_out"] == 1

    msg = cards.access_message()
    print("\n" + msg)
    check_html(msg)
    assert "❌ LOGGED OUT" in msg and "company: TESTP" in msg and "MarketSmith</b>: ❌ not opened today" in msg
    one = cards.access_message("testp")
    check_html(one)
    assert "Data access — TESTP" in one and "13 quarters up to Jun 2026" in one and "open</a>" in one

    # the stock card and the morning scan show the proof
    sources = {"screener": {"ok": True, "at": comp["fetched_at"], "what": "consolidated page, results up to Jun 2026"},
               "marketsmith": {"ok": False, "what": "no data for this stock; technicals computed by the bot"}}
    line = cards.sources_line(sources)
    check_html(line)
    assert "Screener ✅" in line and "today" in line and "MarketSmith ❌" in line, line
    board_line = runner.access_line({"logins": {"screener": True, "marketsmith": False}, "access": access.summary(run=access.RUN["id"])})
    print("\n" + board_line)
    assert board_line.startswith("🔐 Screener ✅ logged in · 4 pages read, 2 failed") and "MarketSmith ❌ LOGGED OUT" in board_line
    print("\nALL ACCESS TESTS PASSED")


if __name__ == "__main__":
    main()
