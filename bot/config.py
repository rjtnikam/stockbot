"""Loads .env + config.yaml and exposes project paths."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ["STOCKBOT_DATA"]) if os.environ.get("STOCKBOT_DATA") else ROOT / "data"  # tests use a temp dir
REPORTS = DATA / "reports"
RAW = DATA / "raw"
SESSION_FILE = ROOT / "session.json"
PHILOSOPHY_FILE = ROOT / "philosophy.md"
BASE_RATES_FILE = DATA / "base_rates.json"

for p in (DATA, REPORTS, RAW):
    p.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    # normalise empty lists (YAML gives None when every item is commented out)
    cfg.setdefault("screener", {})
    cfg["screener"]["screens"] = cfg["screener"].get("screens") or []
    cfg.setdefault("marketsmith", {})
    cfg["marketsmith"]["lists"] = cfg["marketsmith"].get("lists") or []
    cfg.setdefault("hunt", {"enabled": False})
    cfg["hunt"]["numbers_queries"] = cfg["hunt"].get("numbers_queries") or []
    cfg["hunt"].setdefault("filings", {})
    return cfg


def env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name, default)
    return v if v else default


def read_watchlist(cfg: dict) -> list[str]:
    path = ROOT / cfg.get("watchlist_file", "watchlist.txt")
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().upper()
        if line:
            out.append(line)
    return out


def yahoo_ticker(symbol: str) -> str:
    """NSE symbol -> SYMBOL.NS ; numeric BSE code -> CODE.BO"""
    return f"{symbol}.BO" if symbol.isdigit() else f"{symbol}.NS"
