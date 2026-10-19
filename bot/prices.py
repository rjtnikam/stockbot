"""Daily price history (free Yahoo data)."""
from __future__ import annotations

import logging

import pandas as pd
import yfinance as yf

from .config import yahoo_ticker

log = logging.getLogger(__name__)


def history(ticker: str, years: int = 6) -> pd.DataFrame:
    df = yf.download(ticker, period=f"{years}y", interval="1d",
                     auto_adjust=True, progress=False, threads=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):  # newer yfinance returns (field, ticker)
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def stock_history(symbol: str, years: int = 6, codes: dict | None = None) -> pd.DataFrame:
    """Tries every ticker Yahoo might know the company by."""
    codes = codes or {}
    tries = [yahoo_ticker(symbol)]
    if codes.get("nse"):
        tries.append(f"{codes['nse']}.NS")
    if codes.get("bse"):
        tries.append(f"{codes['bse']}.BO")
    if codes.get("bse_id"):
        tries.append(f"{codes['bse_id']}.BO")
    if not symbol.isdigit():
        tries.append(f"{symbol}.BO")
    for t in dict.fromkeys(tries):
        df = history(t, years)
        if not df.empty:
            log.info("prices for %s from %s", symbol, t)
            return df
    log.warning("no price data on Yahoo for %s (tried %s)", symbol, ", ".join(dict.fromkeys(tries)))
    return pd.DataFrame()


def benchmark_history(cfg: dict) -> pd.DataFrame:
    years = cfg["market"].get("price_history_years", 6)
    for t in (cfg["market"].get("benchmark", "^CRSLDX"), "^NSEI"):
        df = history(t, years)
        if not df.empty:
            df.attrs["ticker"] = t
            return df
    return pd.DataFrame()
