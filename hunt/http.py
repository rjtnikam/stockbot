"""Polite HTTP for exchange data.

Tries a plain request first (fast). If the exchange blocks it (common for cloud
servers), retries through the headless browser, which carries real browser
cookies after visiting the exchange's homepage once.
"""
from __future__ import annotations

import json
import logging
import random
import time

import requests

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
           "Accept-Language": "en-US,en;q=0.9",
           "Origin": "https://www.bseindia.com", "Referer": "https://www.bseindia.com/"}


class Http:
    def __init__(self, br=None, delay=(1.0, 2.5)):
        self.br = br
        self.delay = tuple(delay)
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._warmed: set[str] = set()
        self.calls = 0

    def _sleep(self):
        time.sleep(random.uniform(*self.delay))

    def get(self, url: str, params: dict | None = None, kind: str = "json",
            referer: str | None = None, warm: str | None = None):
        """kind: 'json' | 'text' | 'bytes'."""
        self._sleep()
        self.calls += 1
        params = {k: str(v) for k, v in (params or {}).items()}
        extra = {"Referer": referer} if referer else {}
        try:
            r = self.s.get(url, params=params, headers=extra, timeout=45)
            if r.status_code == 200 and r.content:
                return self._decode(r.content, kind)
            log.info("plain GET %s -> %s; retrying via browser", url, r.status_code)
        except Exception as e:  # blocked, reset, bad JSON...
            log.info("plain GET %s failed (%s); retrying via browser", url, e)
        if self.br is None:
            raise RuntimeError(f"GET {url} failed and no browser fallback available")
        if warm and warm not in self._warmed:
            try:
                self.br.page.goto(warm, wait_until="domcontentloaded", timeout=60_000)
                self._warmed.add(warm)
            except Exception as e:
                log.warning("warm-up %s failed: %s", warm, e)
        resp = self.br.page.context.request.get(url, params=params, headers={**HEADERS, **extra}, timeout=60_000)
        if resp.ok:
            return self._decode(resp.body(), kind)
        log.info("browser-request GET %s -> %s; retrying from inside the page", url, resp.status)
        # Last resort: fetch from inside a real page on the exchange's own site. This uses Chrome's
        # own network stack and cookies, which exchange firewalls treat like a person's browser.
        return self._decode(self._page_fetch(url, params, warm), kind)

    def _page_fetch(self, url: str, params: dict, warm: str | None) -> bytes:
        import base64
        from urllib.parse import urlencode
        page = self.br.page
        site = warm or "https://www.bseindia.com/"
        if not (page.url or "").startswith(site.split("/", 3)[0] + "//" + site.split("/", 3)[2]):
            page.goto(site, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(2500)
        full = url + ("?" + urlencode(params) if params else "")
        res = page.evaluate("""async (u) => {
            const r = await fetch(u, {credentials: 'include', headers: {'Accept': 'application/json, text/plain, */*'}});
            const b = new Uint8Array(await r.arrayBuffer());
            let s = ''; for (let i = 0; i < b.length; i += 32768) s += String.fromCharCode.apply(null, b.subarray(i, i + 32768));
            return {status: r.status, body: btoa(s)};
        }""", full)
        if res["status"] != 200:
            raise RuntimeError(f"GET {url} -> HTTP {res['status']} (blocked even from a real page)")
        return base64.b64decode(res["body"])

    @staticmethod
    def _decode(body: bytes, kind: str):
        if kind == "json":
            return json.loads(body.decode("utf-8-sig"))
        if kind == "text":
            return body.decode("utf-8", "replace")
        return body
