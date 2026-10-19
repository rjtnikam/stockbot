"""The numbers net: runs your Screener queries across the whole market, from your login.

Each query in config.yaml (hunt.numbers_queries) is one hypothesis. A company
that shows up in several of them scores higher. Screener does the heavy lifting
server-side, so one page load scans ~5,000 companies.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from bot.config import RAW
from . import db

log = logging.getLogger(__name__)
RAW_URL = "https://www.screener.in/screen/raw/"


def _num(text: str):
    t = (text or "").replace(",", "").replace("%", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def parse_results(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    table = soup.select_one("table.data-table") or soup.find("table")
    if table is None:
        err = soup.select_one(".alert, .error, .errorlist, .invalid-feedback, .flash-error, .messages")
        msg = err.get_text(" ", strip=True) if err else None
        body = soup.get_text(" ", strip=True)
        if not msg and re.search(r"\b(error|invalid|unknown|not a valid|could not)\b", body[:8000], re.I):
            msg = "query error: " + body[:300]
        return {"rows": [], "error": msg}
    heads = [th.get_text(" ", strip=True) for th in table.select("thead th")]
    body_rows = table.select("tbody tr") or table.find_all("tr")
    if not heads and body_rows:
        heads = [th.get_text(" ", strip=True) for th in body_rows[0].find_all("th")]
    rows = []
    for tr in body_rows:
        a = tr.select_one("a[href^='/company/']")
        if not a:
            continue  # header / median rows
        m = re.match(r"/company/([^/]+)/", a["href"])
        if not m:
            continue
        cells = tr.find_all("td")
        cols = {}
        for h, td in zip(heads, cells):
            if h and h not in ("S.No.", "Name"):
                cols[h] = _num(td.get_text(strip=True))
        mcap = next((v for k, v in cols.items() if k.lower().startswith("mar cap")), None)
        rows.append({"slug": m.group(1).upper(), "name": a.get_text(" ", strip=True), "mcap": mcap, "cols": cols})
    return {"rows": rows, "error": None}


def run_query(br, query: str, max_pages: int = 4, tag: str = "q") -> dict:
    from bot import access
    res = _run_query(br, query, max_pages, tag)
    access.record("screener", "market search", tag, not res["error"],
                  res["error"][:150] if res["error"] else f"{len(res['rows'])} companies matched", RAW_URL)
    return res


def _run_query(br, query: str, max_pages: int = 4, tag: str = "q") -> dict:
    rows, seen, capped = [], set(), False
    for page in range(1, max_pages + 1):
        url = RAW_URL + "?" + urlencode({"sort": "", "order": "", "source_id": "", "query": query, "page": page})
        html = br.goto(url)
        if "/login" in (br.page.url or ""):
            return {"rows": [], "error": "Screener session expired (log in again)"}
        res = parse_results(html)
        if res["error"]:
            (RAW / f"screen_{tag}.html").write_text(html, encoding="utf-8")
            if page == 1:
                return {"rows": [], "error": res["error"]}
            break
        new = [r for r in res["rows"] if r["slug"] not in seen]
        if not new:
            break
        for r in new:
            seen.add(r["slug"])
            rows.append(r)
        capped = page == max_pages
    return {"rows": rows, "error": None, "capped": capped}


def with_band(query: str, band) -> str:
    lo, hi = band
    return f"({query}) AND Market Capitalization > {lo} AND Market Capitalization < {hi}"


def run_all(br, cfg: dict, record: bool = True, today: date | None = None) -> dict:
    today = today or date.today()
    h = cfg["hunt"]
    band = h.get("market_cap_cr", [100, 25000])
    out = {}
    db.prune_numbers()
    for spec in h.get("numbers_queries") or []:
        name, weight = spec["name"], float(spec.get("weight", 1.0))
        try:
            res = run_query(br, with_band(spec["query"], band), int(h.get("max_pages_per_query", 4)), tag=name)
        except Exception as e:
            res = {"rows": [], "error": str(e)}
        out[name] = res
        if res["error"]:
            log.warning("numbers query %s failed: %s", name, res["error"])
            continue
        if res.get("capped"):
            log.warning("numbers query %s hit the page cap; consider tightening it", name)
        if record:
            for r in res["rows"]:
                db.add_signal(r["slug"], r["name"], f"num:{name}", weight, today.isoformat(),
                              detail={"cols": r["cols"], "mcap": r["mcap"]},
                              url=f"https://www.screener.in/company/{r['slug']}/", ref=today.isoformat())
    return out
