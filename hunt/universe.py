"""Who is who: maps BSE scrip codes to the Screener page name (NSE symbol, else BSE code).

BSE's security master also gives market cap, used to keep the hunt inside your
small/mid-cap band before spending any effort on a filing. Refreshed weekly.
"""
from __future__ import annotations

import io
import logging

import pandas as pd

from . import db

log = logging.getLogger(__name__)

BSE_LIST = "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
NSE_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
BSE_GROUPS = ["A", "B", "T", "X", "XT", "Z", "M", "MT", "MS", "P", "R", "W", "ZP", "Y"]


def _f(x):
    try:
        return float(str(x).replace(",", ""))
    except Exception:
        return None


def _bse_rows(http) -> list[dict]:
    def call(group: str):
        data = http.get(BSE_LIST, params={"Group": group, "Scripcode": "", "industry": "",
                                          "segment": "Equity", "status": "Active"},
                        warm="https://www.bseindia.com/")
        if isinstance(data, dict):
            data = data.get("Table") or data.get("data") or []
        return data or []

    rows = call("")
    if len(rows) < 1500:  # empty group filter not honoured -> go group by group
        seen = {str(r.get("SCRIP_CD")) for r in rows}
        for g in BSE_GROUPS:
            try:
                for r in call(g):
                    if str(r.get("SCRIP_CD")) not in seen:
                        seen.add(str(r.get("SCRIP_CD")))
                        rows.append(r)
            except Exception as e:
                log.warning("BSE group %s failed: %s", g, e)
    return rows


def _nse_isin_map(http) -> dict[str, str]:
    txt = http.get(NSE_LIST, kind="text", referer="https://www.nseindia.com/", warm="https://www.nseindia.com/")
    df = pd.read_csv(io.StringIO(txt))
    df.columns = [str(c).strip().upper() for c in df.columns]
    return {str(i).strip(): str(s).strip() for i, s in zip(df["ISIN NUMBER"], df["SYMBOL"])}


def refresh(http, force: bool = False) -> dict:
    if not force and db.master_age_days() < 7 and db.master_count() > 1000:
        return {"master": db.master_count(), "refreshed": False}
    rows = _bse_rows(http)
    try:
        nse = _nse_isin_map(http)
    except Exception as e:
        log.warning("NSE symbol list unavailable (%s); BSE codes will be resolved on Screener later", e)
        nse = {}
    out = []
    for r in rows:
        code = str(r.get("SCRIP_CD") or "").strip()
        if not code:
            continue
        isin = str(r.get("ISIN_NUMBER") or "").strip()
        sym = nse.get(isin, "")
        out.append((code, sym or code, isin, r.get("Scrip_Name") or r.get("Issuer_Name") or "",
                    _f(r.get("Mktcap")), sym, r.get("INDUSTRY") or "", r.get("scrip_id") or ""))
    if out:
        db.save_master(out)
    return {"master": len(out), "nse_symbols": len(nse), "refreshed": True}
