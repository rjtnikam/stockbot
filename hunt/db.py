"""SQLite storage for the hunter (same file as the analysis log)."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from bot.config import DATA

DB = DATA / "stockbot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS master(code TEXT PRIMARY KEY, slug TEXT, isin TEXT, name TEXT, mcap REAL,
    nse TEXT, industry TEXT, bse_id TEXT, updated TEXT);
CREATE INDEX IF NOT EXISTS master_slug ON master(slug);
CREATE TABLE IF NOT EXISTS filings(newsid TEXT PRIMARY KEY, code TEXT, slug TEXT, name TEXT, dt TEXT,
    subcat TEXT, headline TEXT, more TEXT, url TEXT, tags TEXT, order_cr REAL, note TEXT);
CREATE INDEX IF NOT EXISTS filings_code ON filings(code, dt);
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT, event_date TEXT,
    slug TEXT, name TEXT, signal TEXT, weight REAL, detail TEXT, url TEXT, ref TEXT,
    UNIQUE(slug, signal, ref));
CREATE INDEX IF NOT EXISTS signals_slug ON signals(slug, event_date);
CREATE TABLE IF NOT EXISTS concall_hist(code TEXT PRIMARY KEY, has_prior INTEGER, checked TEXT);
CREATE TABLE IF NOT EXISTS scores(run_date TEXT, slug TEXT, score REAL, PRIMARY KEY(run_date, slug));
CREATE TABLE IF NOT EXISTS docs(key TEXT PRIMARY KEY, created TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS results(slug TEXT, filed TEXT, name TEXT, quarter TEXT, url TEXT, summary TEXT,
    status TEXT, tries INTEGER DEFAULT 0, data TEXT, updated TEXT, PRIMARY KEY(slug, filed));
CREATE INDEX IF NOT EXISTS results_filed ON results(filed);
CREATE TABLE IF NOT EXISTS filing_scores(newsid TEXT PRIMARY KEY, importance INTEGER, why TEXT, change INTEGER,
    scored TEXT);
CREATE TABLE IF NOT EXISTS observer_sent(newsid TEXT PRIMARY KEY, sent TEXT);
"""


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


# ---------- state ----------
def get_state(key: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None


def set_state(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, value))


# ---------- master (BSE code <-> Screener slug) ----------
def save_master(rows: list[tuple]) -> None:
    now = date.today().isoformat()
    with conn() as c:
        c.executemany("INSERT OR REPLACE INTO master VALUES (?,?,?,?,?,?,?,?,?)", [(*r, now) for r in rows])


def master_count() -> int:
    with conn() as c:
        return c.execute("SELECT COUNT(*) FROM master").fetchone()[0]


def master_age_days() -> float:
    with conn() as c:
        r = c.execute("SELECT MAX(updated) FROM master").fetchone()[0]
    return 999 if not r else (date.today() - date.fromisoformat(r)).days


def master_map() -> dict[str, dict]:
    with conn() as c:
        return {r["code"]: dict(r) for r in c.execute("SELECT * FROM master")}


def master_by_slug(slug: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM master WHERE slug=? OR code=? LIMIT 1", (slug, slug)).fetchone()
        return dict(r) if r else None


# ---------- filings ----------
def filing_exists(newsid: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM filings WHERE newsid=?", (newsid,)).fetchone() is not None


def save_filing(f: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            f["newsid"], f["code"], f.get("slug"), f.get("name"), f["dt"], f.get("subcat"), f.get("headline"),
            f.get("more"), f.get("url"), json.dumps(sorted(f.get("tags", []))), f.get("order_cr"), f.get("note")))


def has_concall_before(code: str, before_iso: str) -> bool:
    with conn() as c:
        rows = c.execute("SELECT tags FROM filings WHERE code=? AND dt<?", (code, before_iso)).fetchall()
    return any("concall" in json.loads(r["tags"] or "[]") for r in rows)


def filings_for(slug: str, since_iso: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM filings WHERE slug=? AND dt>=? ORDER BY dt DESC", (slug, since_iso))]


def concall_cache_get(code: str) -> sqlite3.Row | None:
    with conn() as c:
        return c.execute("SELECT * FROM concall_hist WHERE code=?", (code,)).fetchone()


def concall_cache_set(code: str, has_prior: bool) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO concall_hist VALUES (?,?,?)", (code, int(has_prior), date.today().isoformat()))


# ---------- signals ----------
def add_signal(slug: str, name: str, signal: str, weight: float, event_date: str,
               detail: dict | None = None, url: str | None = None, ref: str | None = None) -> None:
    with conn() as c:
        c.execute("INSERT OR IGNORE INTO signals(run_date,event_date,slug,name,signal,weight,detail,url,ref) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (date.today().isoformat(), event_date, slug, name, signal, weight,
                   json.dumps(detail or {}, default=str), url, ref or event_date))


def prune_numbers(keep_days: int = 30) -> None:
    """Only the latest numbers scan matters for scoring; keep a month for history."""
    cutoff = date.fromordinal(date.today().toordinal() - keep_days).isoformat()
    with conn() as c:
        c.execute("DELETE FROM signals WHERE signal LIKE 'num:%' AND run_date < ?", (cutoff,))


def signals_since(since_iso: str) -> list[dict]:
    with conn() as c:
        out = []
        for r in c.execute("SELECT * FROM signals WHERE event_date>=? OR run_date>=?", (since_iso, since_iso)):
            d = dict(r)
            d["detail"] = json.loads(d["detail"] or "{}")
            out.append(d)
        return out


def save_scores(run_date: str, scores: dict[str, float]) -> None:
    with conn() as c:
        c.executemany("INSERT OR REPLACE INTO scores VALUES (?,?,?)", [(run_date, s, v) for s, v in scores.items()])


def score_on_or_before(slug: str, iso: str) -> float | None:
    with conn() as c:
        r = c.execute("SELECT score FROM scores WHERE slug=? AND run_date<=? ORDER BY run_date DESC LIMIT 1",
                      (slug, iso)).fetchone()
        return r["score"] if r else None


# ---------- document cache ----------
def doc_get(key: str, max_age_days: int = 30) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM docs WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    if (datetime.now() - datetime.fromisoformat(r["created"])).days > max_age_days:
        return None
    return json.loads(r["data"])


def doc_put(key: str, data: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?)",
                  (key, datetime.now().isoformat(timespec="seconds"), json.dumps(data, default=str)))


# ---------- quarterly results ----------
def result_get(slug: str, filed: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM results WHERE slug=? AND filed=?", (slug, filed)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"] or "{}")
    return d


def result_put(r: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?,?,?,?,?)", (
            r["slug"], r["filed"], r.get("name"), r.get("quarter"), r.get("url"), r.get("summary"),
            r.get("status", "listed"), int(r.get("tries") or 0), json.dumps(r.get("data") or {}, default=str),
            datetime.now().isoformat(timespec="seconds")))


def results_since(since_iso: str, status: str | None = None) -> list[dict]:
    q = "SELECT * FROM results WHERE filed>=?" + (" AND status=?" if status else "") + " ORDER BY filed DESC"
    with conn() as c:
        rows = [dict(r) for r in c.execute(q, (since_iso, status) if status else (since_iso,))]
    for d in rows:
        d["data"] = json.loads(d["data"] or "{}")
    return rows


def latest_result(slug: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM results WHERE slug=? ORDER BY filed DESC LIMIT 1", (slug,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"] or "{}")
    return d


# ---------- the filing observer (importance scores + what was already reported) ----------
def save_score(newsid: str, importance, why: str | None, change) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO filing_scores VALUES (?,?,?,?,?)",
                  (newsid, None if importance is None else int(importance), (why or "")[:300],
                   None if change is None else int(bool(change)), datetime.now().isoformat(timespec="seconds")))


def filings_scored_since(since_iso: str) -> list[dict]:
    """Filings on or after a date, with the reader's importance score (None when not scored)."""
    with conn() as c:
        rows = c.execute("SELECT f.*, s.importance, s.why, s.change FROM filings f "
                         "LEFT JOIN filing_scores s ON s.newsid = f.newsid WHERE f.dt >= ? ORDER BY f.dt DESC",
                         (since_iso,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        out.append(d)
    return out


def observer_sent(newsids: list[str]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with conn() as c:
        c.executemany("INSERT OR REPLACE INTO observer_sent VALUES (?,?)", [(n, now) for n in newsids])


def observer_was_sent(newsid: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM observer_sent WHERE newsid=?", (newsid,)).fetchone() is not None
