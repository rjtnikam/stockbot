import json
from bot.session import browser_session
from bot.config import RAW
d = RAW / "ms_probe"
d.mkdir(exist_ok=True)
caught = []
def grab(r):
    try:
        if "json" in (r.headers.get("content-type") or ""):
            caught.append({"url": r.url, "data": r.json()})
    except Exception:
        pass
with browser_session((1, 2)) as br:
    br.page.on("response", grab)
    br.goto("https://marketsmithindia.com/mstool/eval/fredun/evaluation.jsp#/", wait="domcontentloaded")
    br.page.wait_for_timeout(15000)
    print("landed on:", br.page.url)
    (d / "page.html").write_text(br.page.content())
    (d / "data.json").write_text(json.dumps(caught, default=str)[:20000000])
    br.page.screenshot(path=str(d / "shot.png"), full_page=True)
print("saved", len(caught), "data responses")
