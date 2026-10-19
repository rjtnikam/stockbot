"""SQLite log of every analysis: the raw material for the feedback loop."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from .config import DATA

DB = DATA / "stockbot.db"


def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS analyses(
        symbol TEXT, run_date TEXT, price REAL, stage TEXT, verdict TEXT, conviction INTEGER,
        price_vs_story TEXT, implied_growth REAL, source TEXT, report_path TEXT, meta TEXT)""")
    return c


def recently_analysed(symbol: str, days: int) -> bool:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    with _conn() as c:
        return c.execute("SELECT 1 FROM analyses WHERE symbol=? AND run_date>=?", (symbol, cutoff)).fetchone() is not None


def log_analysis(symbol: str, price, stage, meta: dict, implied, source: str, path: str) -> None:
    with _conn() as c:
        c.execute("INSERT INTO analyses VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            symbol, date.today().isoformat(), price, meta.get("stage") or stage, meta.get("verdict"),
            meta.get("conviction"), meta.get("price_vs_story"),
            implied if isinstance(implied, (int, float)) else None, source, path, json.dumps(meta)))
