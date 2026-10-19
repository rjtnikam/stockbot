"""RUN THIS ON YOUR LAPTOP (not the server), once.

It opens a real Chrome window. Log in to Screener and MarketSmith normally
(tick "remember me" where offered). Then come back to this terminal and press
Enter. Your login is saved to session.json, which you copy to the server.

    pip install playwright && playwright install chromium
    python login_local.py
    scp -i <your-lightsail-key.pem> session.json ubuntu@<server-ip>:~/stockbot/
"""
from playwright.sync_api import sync_playwright

SITES = [
    "https://www.screener.in/login/",
    "https://marketsmithindia.com/",
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    ctx = browser.new_context(viewport={"width": 1366, "height": 850})
    for url in SITES:
        ctx.new_page().goto(url)
    input("\nLog in on BOTH tabs, then press Enter here to save the session... ")
    ctx.storage_state(path="session.json")
    print("Saved session.json. Copy it to the server (see README step 4).")
    browser.close()
