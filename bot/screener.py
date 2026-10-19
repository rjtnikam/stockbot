"""Reads Screener.in exactly as you would: your saved screens and company pages."""
from __future__ import annotations

import logging
import re
from datetime import datetime

import pandas as pd
from bs4 import BeautifulSoup

from . import access
from .config import RAW

log = logging.getLogger(__name__)
BASE = "https://www.screener.in"
SECTIONS = ["quarters", "profit-loss", "balance-sheet", "cash-flow", "ratios", "shareholding"]


def _num(text: str):
    t = (text or "").replace(",", "").replace("%", "").replace("₹", "").replace("Cr.", "").strip()
    if t in ("", "-", "--"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _clean_label(text: str) -> str:
    t = (text or "").replace("\xa0", " ").replace("+", "").strip()
    return re.sub(r"\s+", " ", t)


def _parse_table(section) -> pd.DataFrame:
    """Screener section -> DataFrame (rows = line items, columns = periods)."""
    table = section.find("table") if section else None
    if table is None:
        return pd.DataFrame()
    head = [th.get_text(strip=True) for th in table.find("thead").find_all("th")] if table.find("thead") else []
    periods = head[1:]
    rows = {}
    for tr in table.find("tbody").find_all("tr") if table.find("tbody") else []:
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        label = _clean_label(tds[0].get_text(" "))
        if not label:
            continue
        vals = [_num(td.get_text(strip=True)) for td in tds[1:]]
        rows[label] = vals[: len(periods)] + [None] * (len(periods) - len(vals))
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_dict(rows, orient="index", columns=periods)


def parse_company_html(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out: dict = {"tables": {}}
    h1 = soup.find("h1")
    out["name"] = h1.get_text(strip=True) if h1 else None
    ratios = {}
    for li in soup.select("#top-ratios li"):
        n, v = li.select_one(".name"), li.select_one(".number")
        if n and v:
            ratios[_clean_label(n.get_text())] = _num(v.get_text())
    out["top_ratios"] = ratios
    about = soup.select_one(".company-profile .about, .about")
    out["about"] = about.get_text(" ", strip=True)[:1500] if about else ""
    for sec in SECTIONS:
        out["tables"][sec] = _parse_table(soup.find("section", id=sec))
    out["documents"] = _documents(soup)
    out["codes"] = _codes(soup)
    cid = soup.select_one("[data-company-id]")
    out["company_id"] = cid["data-company-id"] if cid else None
    return out


def _codes(soup) -> dict:
    """Exchange codes Screener shows on the company page (used to find price data)."""
    codes = {}
    for a in soup.find_all("a", href=True):
        h = a["href"]
        m = re.search(r"nseindia\.com/.*symbol=([A-Z0-9&\-]+)", h)
        if m:
            codes.setdefault("nse", m.group(1))
        m = re.search(r"bseindia\.com/stock-share-price/[^/]+/([^/]+)/(\d{6})", h)
        if m:
            codes.setdefault("bse_id", m.group(1).upper())
            codes.setdefault("bse", m.group(2))
    return codes


def _documents(soup) -> dict:
    """Concall transcripts / PPTs and annual reports listed on the company page (newest first)."""
    sec = soup.find("section", id="documents")
    out = {"concalls": [], "annual_reports": []}
    if not sec:
        return out
    box = sec.select_one(".concalls")
    for li in (box.select("li") if box else []):
        links = {a.get_text(" ", strip=True).lower(): a["href"] for a in li.find_all("a", href=True)}
        first = li.find("div")
        out["concalls"].append({"date": (first.get_text(" ", strip=True) if first else "")[:20], "links": links})
    if not out["concalls"]:  # layout fallback: any 'Transcript' link in the documents section
        out["concalls"] = [{"date": "", "links": {"transcript": a["href"]}} for a in sec.find_all("a", href=True)
                           if "transcript" in a.get_text(" ", strip=True).lower()]
    ar = sec.select_one(".annual-reports")
    if ar:
        out["annual_reports"] = [{"title": a.get_text(" ", strip=True)[:80], "url": a["href"]}
                                 for a in ar.select("a[href]")][:5]
    return out


def resolve_slug(br, name_or_code: str) -> str | None:
    """Find a company's Screener page name via Screener's own search box API."""
    import json as _json
    from urllib.parse import quote
    try:
        br.goto(f"{BASE}/api/company/search/?q={quote(name_or_code)}&v=3&fts=1")
        txt = br.page.inner_text("body")
        hits = _json.loads(txt)
    except Exception as e:
        log.warning("screener search failed for %s: %s", name_or_code, e)
        return None
    for h in hits or []:
        m = re.match(r"/company/([^/]+)/", h.get("url") or "")
        if m:
            return m.group(1).upper()
    return None


def fetch_company(br, symbol: str) -> dict:
    """Consolidated first; fall back to standalone if consolidated is empty."""
    for suffix in ("consolidated/", ""):
        url = f"{BASE}/company/{symbol}/{suffix}"
        html = br.goto(url)
        if "/login" in br.page.url and "company" not in br.page.url:
            raise RuntimeError("Screener redirected to login")
        data = parse_company_html(html)
        q = data["tables"].get("quarters")
        if q is not None and not q.empty and q.notna().sum().sum() > 4:
            data["url"] = url
            data["basis"] = "consolidated" if suffix else "standalone"
            data["fetched_at"] = datetime.now().isoformat(timespec="seconds")
            (RAW / f"screener_{symbol}.html").write_text(html, encoding="utf-8")
            cols = list(q.columns)
            access.record("screener", "company", symbol, True,
                          f"{data['basis']} page: {len(cols)} quarters up to {cols[-1]}, "
                          f"{len(data.get('top_ratios') or {})} key ratios", url)
            return data
    access.record("screener", "company", symbol, False, "page opened but no financials found", f"{BASE}/company/{symbol}/")
    raise RuntimeError(f"Screener returned no financials for {symbol} (page layout changed?)")


def fetch_screen_symbols(br, screen_url: str, max_pages: int = 3) -> list[str]:
    syms: list[str] = []
    for page in range(1, max_pages + 1):
        url = screen_url + ("&" if "?" in screen_url else "?") + f"page={page}"
        soup = BeautifulSoup(br.goto(url), "lxml")
        found = []
        for a in soup.select("table a[href^='/company/']"):
            m = re.match(r"/company/([^/]+)/", a["href"])
            if m:
                found.append(m.group(1).upper())
        new = [s for s in dict.fromkeys(found) if s not in syms]
        if not new:
            break
        syms.extend(new)
    log.info("screen %s -> %d stocks", screen_url, len(syms))
    return syms


def price_history(br, company_id: str, days: int = 2200):
    """Daily close + volume from Screener's own price chart (needs no other website)."""
    import json as _json
    import pandas as pd
    url = f"{BASE}/api/company/{company_id}/chart/?q=Price-Volume&days={days}"
    resp = br.page.context.request.get(url, timeout=60_000)
    if not resp.ok:
        access.record("screener", "prices", str(company_id), False, f"HTTP {resp.status}", url)
        raise RuntimeError(f"Screener chart -> HTTP {resp.status}")
    data = _json.loads(resp.body())
    series = {}
    for ds in data.get("datasets", []):
        metric = (ds.get("metric") or "").lower()
        vals = {}
        for row in ds.get("values", []):
            try:
                vals[pd.Timestamp(row[0])] = float(row[1])
            except (TypeError, ValueError, IndexError):
                pass
        if metric.startswith("price"):
            series["Close"] = vals
        elif metric.startswith("volume"):
            series["Volume"] = vals
    if not series.get("Close"):
        raise RuntimeError("Screener chart had no prices")
    df = pd.DataFrame(series).sort_index()
    df["Close"] = df["Close"].ffill()
    df = df.dropna(subset=["Close"])
    df["Volume"] = df.get("Volume", pd.Series(0, index=df.index)).fillna(0)
    df["Open"] = df["High"] = df["Low"] = df["Close"]  # close-only data
    df.attrs["source"] = "screener"
    access.record("screener", "prices", str(company_id), True, f"{len(df)} daily prices", url)
    return df[["Open", "High", "Low", "Close", "Volume"]]
