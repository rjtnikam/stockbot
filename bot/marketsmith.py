"""Reads your MarketSmith India lists (Blue Dot / idea lists).

MarketSmith is a JavaScript app, so instead of guessing page layout we listen to
the data the page itself downloads (JSON) and pull stock symbols + ratings out of
it, with an HTML-table fallback. Run `python run_daily.py --calibrate-marketsmith`
once: it saves screenshots + raw data so extraction can be tuned if needed.
"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime

import pandas as pd

from . import access
from .config import RAW

log = logging.getLogger(__name__)
SYM_KEYS = re.compile(r"^(symbol|nsesymbol|nse_symbol|nsecode|nse_code|ticker|scripcode|sym)$", re.I)
RATING_KEYS = re.compile(r"(rs.?rating|eps.?rating|master.?score|smr|a.?d.?rating|composite)", re.I)
SYM_RE = re.compile(r"^[A-Z][A-Z0-9&\-]{1,19}$|^\d{6}$")


def _walk(obj, rows: list):
    if isinstance(obj, dict):
        sym = None
        for k, v in obj.items():
            if SYM_KEYS.match(str(k)) and isinstance(v, (str, int)) and SYM_RE.match(str(v).upper()):
                sym = str(v).upper()
        if sym:
            ratings = {k: v for k, v in obj.items() if RATING_KEYS.search(str(k)) and isinstance(v, (int, float, str))}
            rows.append({"symbol": sym, **ratings})
        for v in obj.values():
            _walk(v, rows)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, rows)


def fetch_list(br, url: str, calibrate: bool = False, tag: str = "list") -> list[dict]:
    captured: list = []

    def on_response(resp):
        try:
            if "json" in (resp.headers.get("content-type") or ""):
                captured.append(resp.json())
        except Exception:
            pass

    br.page.on("response", on_response)
    try:
        br.goto(url, wait="networkidle")
        br.page.wait_for_timeout(4000)
        html = br.page.content()
    finally:
        br.page.remove_listener("response", on_response)

    rows: list[dict] = []
    for blob in captured:
        _walk(blob, rows)

    if not rows:  # fallback: visible HTML tables with a Symbol column
        try:
            for df in pd.read_html(io.StringIO(html)):
                col = next((c for c in df.columns if "symbol" in str(c).lower()), None)
                if col is not None:
                    rows += [{"symbol": str(s).upper()} for s in df[col].dropna()]
        except ValueError:
            pass

    if calibrate or not rows:
        (RAW / f"ms_{tag}.html").write_text(html, encoding="utf-8")
        (RAW / f"ms_{tag}.json").write_text(json.dumps(captured, default=str)[:5_000_000], encoding="utf-8")
        br.page.screenshot(path=str(RAW / f"ms_{tag}.png"), full_page=True)
        log.warning("MarketSmith %s: %d rows. Debug files saved in data/raw/ms_%s.*", tag, len(rows), tag)

    dedup = {}
    for r in rows:
        dedup.setdefault(r["symbol"], r)
    return list(dedup.values())


# ------------------------------------------------------------------ one stock's technicals
EVAL_URL = "https://marketsmithindia.com/mstool/eval/{sym}/evaluation.jsp#/"
LIST_NAMES_OF_INTEREST = ("Growth 50", "Near Buy Point", "Recent Breakouts", "Potential High Tight Flag",
                          "Recent Additions", "Blue Dot", "Trend Template")


def _latest_base(patterns: dict) -> dict | None:
    bases = []
    for kind, items in (patterns or {}).items():
        if kind == "tightAreas" or not isinstance(items, list):
            continue
        for b in items:
            if isinstance(b, dict) and b.get("baseStartDt"):
                bases.append(b)
    if not bases:
        return None
    b = max(bases, key=lambda x: x.get("baseEndDt") or x.get("baseStartDt") or 0)
    info = {i.get("infoType"): i.get("infoValue") for i in b.get("infos") or []}
    ts = lambda ms: pd.Timestamp(ms, unit="ms").date().isoformat() if ms else None
    depth = info.get("BaseDepth")
    return {"type": b.get("patternType"), "stage": b.get("baseStage"), "status": b.get("baseStatus"),
            "weeks": b.get("baseLength"), "start": ts(b.get("baseStartDt")), "end": ts(b.get("baseEndDt")),
            "pivot_date": ts(b.get("pivotDt") or b.get("pivotPriceDt")),
            "depth_pct": round(float(depth) * 100, 1) if depth else None}


def stock_technicals(br, symbols: list[str]) -> dict | None:
    """Opens MarketSmith's evaluation page for the stock and reads MarketSmith's own ratings,
    patterns, lists, red flags and daily prices from the data the page loads.
    Returns None if MarketSmith doesn't know the stock or the session is logged out."""
    for sym in [s for s in dict.fromkeys(symbols) if s]:
        caught: dict = {}

        def grab(resp):
            try:
                if "json" in (resp.headers.get("content-type") or "") and "marketsmithindia.com/gateway" in resp.url:
                    caught.setdefault(resp.url.split("?")[0].rsplit("/", 1)[-1], []).append((resp.url, resp.json()))
            except Exception:
                pass

        br.page.on("response", grab)
        try:
            br.goto(EVAL_URL.format(sym=sym.lower()), wait="domcontentloaded")
            for _ in range(20):  # up to ~20 s for the ratings and the daily chart to arrive
                br.page.wait_for_timeout(1000)
                if "symboldetails.json" in caught and any("p=0" in u for u, _ in caught.get("details.json", [])):
                    break
        finally:
            br.page.remove_listener("response", grab)
        if "login" in (br.page.url or "").lower():
            access.record("marketsmith", "stock", sym, False, "LOGGED OUT (run login_local.py)", EVAL_URL.format(sym=sym.lower()))
            raise RuntimeError("MarketSmith session expired (log in again)")
        if "symboldetails.json" not in caught:
            log.info("MarketSmith has no data for %s", sym)
            access.record("marketsmith", "stock", sym, False, "no data for this symbol", EVAL_URL.format(sym=sym.lower()))
            continue
        out = _assemble(sym, caught)
        out["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        n = len(out["prices_daily"]) if out.get("prices_daily") is not None else 0
        access.record("marketsmith", "stock", sym, True,
                      f"RS {out.get('rs_rating')}, EPS {out.get('eps_rating')}, Master {out.get('master_score')}, "
                      f"{n} daily prices", EVAL_URL.format(sym=sym.lower()))
        return out
    return None


def _assemble(sym: str, caught: dict) -> dict:
    first = lambda k: (caught.get(k) or [(None, {})])[0][1] or {}
    sd = (first("symboldetails.json").get("response") or {})
    g = sd.get("detailsGeneralInformationHeader") or {}
    gb = sd.get("detailsGeneralInformationHeaderBlock") or {}
    wis = (first("wisdom.json").get("response") or {}).get("results") or []
    wv = {}
    for x in wis:
        wv.setdefault(x.get("name"), {"value": x.get("itemValue"), "title": x.get("title") or None})
        if x.get("title") and not wv[x["name"]].get("title"):
            wv[x["name"]]["title"] = x["title"]
    lists = [l.get("name") for l in (first("getStockUserInfo").get("stockLists") or [])]
    info = (caught.get("getStockUserInfo.json") or [(None, {})])[0][1].get("data") or {}
    flags = ((first("getRedFlags.json").get("response") or {}).get("results") or {})
    checks = ((first("checklists.json").get("response") or {}).get("checklistResults") or [])
    oneil = next((c for c in checks if "Neil" in (c.get("filterName") or "")), None)
    pats = first("patterns.json").get("patterns") or {}
    out = {
        "source": "MarketSmith India",
        "ms_symbol": sym.upper(),
        "rs_rating": g.get("rsNumericGrade"), "eps_rating": g.get("epsRank"),
        "master_score": wv.get("Master Score", {}).get("value") or g.get("masterScore"),
        "acc_dis_rating": g.get("accDisRating"),
        "industry_group": g.get("industryName"),
        "industry_group_rank": (wv.get("Industry Group Rank") or {}).get("value") or g.get("industryGroupRank"),
        "market_direction": (wv.get("Market Direction") or {}).get("value"),
        "up_down_volume_ratio": gb.get("upDownVol"),
        "eps_growth_rate_pct": gb.get("epsGrowthRate"), "earnings_stability": gb.get("earningsStability"),
        "in_lists": [n for n in lists if n] + (["Blue Dot"] if info.get("IsblueDotExists") else [])
                    + (["Trend Template"] if info.get("IsTrendTemplate") else []),
        "latest_base": _latest_base(pats),
        "oneil_checklist": f"{oneil.get('numCriteriaPassed')}/{oneil.get('numCriteria')}" if oneil else None,
        "ms_red_flags": [k[2:] for k, v in flags.items() if isinstance(v, int) and v == 1],
    }
    daily = next((d for u, d in caught.get("details.json", []) if "p=0" in u), None)
    weekly = next((d for u, d in caught.get("details.json", []) if "p=1" in u), None)
    for name, blob in (("daily", daily), ("weekly", weekly)):
        prices = ((blob or {}).get("chart") or {}).get("prices") or []
        if prices:
            df = pd.DataFrame(prices)
            df.index = pd.to_datetime(df["priceDate"], unit="ms").dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata").dt.tz_localize(None).dt.normalize()
            df = df.sort_index()
            px = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"})
            out[f"prices_{name}"] = px[["Open", "High", "Low", "Close", "Volume"]]
            if "bc" in df:
                out[f"benchmark_{name}"] = df[["bc"]].rename(columns={"bc": "Close"})
    return out
