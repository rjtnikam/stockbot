"""Invisible (headless) Chrome that is already logged in as you.

session.json is created ONCE on your laptop by login_local.py and copied to the
server. Every run reuses it and saves the refreshed cookies back, which keeps the
login alive longer.
"""
from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager

from playwright.sync_api import sync_playwright, Page

from . import access
from .config import SESSION_FILE

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


class Browser:
    def __init__(self, page: Page, delay: tuple[float, float]):
        self.page = page
        self.delay = delay

    def goto(self, url: str, wait: str = "domcontentloaded") -> str:
        """Polite navigation: random human-like pause before every page. Every page goes into the access log."""
        time.sleep(random.uniform(*self.delay))
        log.info("open %s", url)
        site = access.site_of(url)
        try:
            resp = self.page.goto(url, wait_until=wait, timeout=60_000)
        except Exception as e:
            access.record(site, "page", None, False, f"could not open: {str(e)[:150]}", url)
            raise
        status = getattr(resp, "status", None)
        out = access.is_login_page(site, self.page.url)
        access.record(site, "page", None, (status is None or status < 400) and not out,
                      (f"HTTP {status}" if status else "loaded") + (" · LOGGED OUT" if out else ""), url)
        return self.page.content()


@contextmanager
def browser_session(delay=(3, 7), require_login: bool = True):
    has_session = SESSION_FILE.exists()
    if require_login and not has_session:
        raise FileNotFoundError(
            "session.json missing. Run login_local.py on your laptop and copy "
            "session.json to the server (see README step 4).")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(storage_state=str(SESSION_FILE) if has_session else None, user_agent=UA,
                            viewport={"width": 1440, "height": 900},
                            locale="en-IN", timezone_id="Asia/Kolkata")
        page = ctx.new_page()
        try:
            yield Browser(page, tuple(delay))
        finally:
            try:
                if has_session:
                    ctx.storage_state(path=str(SESSION_FILE))  # keep cookies fresh
            except Exception as e:  # never lose the run over this
                log.warning("could not refresh session.json: %s", e)
            ctx.close()
            b.close()


def screener_logged_in(br: Browser) -> bool:
    try:
        br.goto("https://www.screener.in/dash/")
        ok = "/login" not in br.page.url
    except Exception:
        ok = False
    access.record("screener", "login", None, ok, "logged in as you" if ok else "LOGGED OUT (run login_local.py)")
    return ok


def marketsmith_logged_in(br: Browser, cfg: dict) -> bool:
    ms = cfg.get("marketsmith", {})
    url = ms.get("logged_in_check_url")
    if not url:
        return False
    try:
        br.goto(url, wait="networkidle")
        ok = ms.get("login_url_fragment", "login") not in br.page.url.lower()
    except Exception:
        ok = False
    access.record("marketsmith", "login", None, ok, "logged in as you" if ok else "LOGGED OUT (run login_local.py)")
    return ok
