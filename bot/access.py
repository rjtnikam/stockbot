"""Proof of access: every time the bot reads Screener or MarketSmith, it writes it down here.

Two kinds of lines in the access log (table access_log in data/stockbot.db):
  page  — every page the hidden browser opened: site, address, HTTP status, logged in or not, time
  data  — what it actually read from it, e.g. "TAC company page: consolidated, results to Sep 2026"
You see it in Telegram: /access (today), /access SYMBOL, the 'Data read' line on every stock card,
and the login line on the morning scan.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .config import DATA

log = logging.getLogger(__name__)
DB = DATA / "stockbot.db"
RUN = {"id": "manual"}
SCHEMA = """CREATE TABLE IF NOT EXISTS access_log(ts TEXT, run TEXT, site TEXT, kind TEXT, target TEXT,
    ok INTEGER, detail TEXT, url TEXT);
CREATE INDEX IF NOT EXISTS access_ts ON access_log(ts);
CREATE INDEX IF NOT EXISTS access_target ON access_log(target);"""
SITE_NAME = {"screener": "Screener", "marketsmith": "MarketSmith"}


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def start_run(mode: str) -> str:
    """Call once per process: every line written after this belongs to this run."""
    RUN["id"] = f"{datetime.now():%Y-%m-%d %H:%M:%S} {mode}"
    try:
        with _conn() as c:  # keep 90 days
            c.execute("DELETE FROM access_log WHERE ts < ?", ((datetime.now() - timedelta(days=90)).isoformat(),))
    except Exception as e:
        log.debug("access log prune failed: %s", e)
    return RUN["id"]


def site_of(url: str) -> str:
    h = (urlparse(url or "").hostname or "").lower()
    if h.endswith("screener.in"):
        return "screener"
    if "marketsmithindia" in h:
        return "marketsmith"
    for k in ("bseindia", "nseindia"):
        if k in h:
            return k.replace("india", "")
    return h or "other"


def path_of(url: str) -> str:
    u = urlparse(url or "")
    return (u.path or "/") + (("?" + u.query[:60]) if u.query else "")


def is_login_page(site: str, final_url: str) -> bool:
    f = (final_url or "").lower()
    return ("/login" in f) if site == "screener" else ("login" in f) if site == "marketsmith" else False


def record(site: str, kind: str, target: str | None, ok: bool, detail: str = "", url: str = "") -> None:
    """Never breaks a run: a failed write only goes to the log file."""
    try:
        with _conn() as c:
            c.execute("INSERT INTO access_log VALUES (?,?,?,?,?,?,?,?)",
                      (datetime.now().isoformat(timespec="seconds"), RUN["id"], site, kind, (target or "").upper(),
                       int(bool(ok)), (detail or "")[:500], (url or "")[:300]))
    except Exception as e:
        log.warning("access log write failed: %s", e)


def _rows(where: str, args: tuple, limit: int = 2000) -> list[dict]:
    try:
        with _conn() as c:
            return [dict(r) for r in c.execute(f"SELECT * FROM access_log WHERE {where} ORDER BY ts LIMIT ?",
                                               (*args, limit))]
    except Exception:
        return []


def summary(since: str | None = None, run: str | None = None) -> dict:
    """Per site: pages opened / ok / failed / logged out, first and last time, and the 'data' lines."""
    if run:
        rows = _rows("run = ?", (run,))
    else:
        rows = _rows("ts >= ?", (since or datetime.now().strftime("%Y-%m-%dT00:00:00"),))
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r["site"], {"pages": 0, "ok": 0, "failed": 0, "logged_out": 0, "first": r["ts"],
                                        "last": r["ts"], "data": [], "login": None})
        s["last"] = r["ts"]
        if r["kind"] == "page":
            s["pages"] += 1
            s["ok" if r["ok"] else "failed"] += 1
            s["logged_out"] += int("LOGGED OUT" in (r["detail"] or ""))
        elif r["kind"] == "login":
            s["login"] = bool(r["ok"])
            s["data"].append(r)
        else:
            s["data"].append(r)
    return out


def for_target(target: str, n: int = 12) -> list[dict]:
    rows = _rows("target = ? AND kind != 'page'", (target.upper(),), limit=100000)
    return rows[-n:]


def hhmm(ts: str | None) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except (TypeError, ValueError):
        return "time unknown"


def when(ts: str | None) -> str:
    """'06:04 today' / '06:04, 15 Oct'."""
    try:
        t = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return "time unknown"
    return f"{t:%H:%M} today" if t.date() == datetime.now().date() else f"{t:%H:%M}, {t:%d %b}"
