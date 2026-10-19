"""ValuePickr forum (Discourse) via its public JSON — no login needed."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)
BASE = "https://forum.valuepickr.com"
HDR = {"User-Agent": "Mozilla/5.0 (research bot; personal use)", "Accept": "application/json"}


def _get(path: str, **params):
    time.sleep(1.5)
    r = requests.get(BASE + path, params=params, headers=HDR, timeout=30)
    r.raise_for_status()
    return r.json()


def find_thread(company_name: str, symbol: str) -> dict | None:
    name = re.sub(r"\b(ltd|limited|india|industries)\b\.?", "", company_name or "", flags=re.I).strip()
    for q in (name, symbol):
        if not q:
            continue
        try:
            res = _get("/search.json", q=q)
        except Exception as e:
            log.warning("valuepickr search failed: %s", e)
            return None
        topics = res.get("topics") or []
        if topics:
            # prefer the thread with most posts whose title mentions the name
            key = (name.split() or [symbol])[0].lower()
            topics.sort(key=lambda t: (key in t.get("title", "").lower(), t.get("posts_count", 0)), reverse=True)
            return topics[0]
    return None


def fetch(company_name: str, symbol: str, max_posts: int = 60, lookback_days: int = 90) -> dict:
    t = find_thread(company_name, symbol)
    if not t:
        return {"found": False}
    tid, slug = t["id"], t.get("slug", "")
    meta = _get(f"/t/{tid}.json")
    stream = meta.get("post_stream", {}).get("stream", [])[-max_posts:]
    posts = []
    for i in range(0, len(stream), 20):
        chunk = stream[i:i + 20]
        data = _get(f"/t/{tid}/posts.json", **{"post_ids[]": chunk})
        posts += data.get("post_stream", {}).get("posts", [])
    now = datetime.now(timezone.utc)
    recent = prior = 0
    out_posts = []
    for p in posts:
        created = datetime.fromisoformat(p["created_at"].replace("Z", "+00:00"))
        age = (now - created).days
        recent += age <= lookback_days
        prior += lookback_days < age <= 2 * lookback_days
        text = BeautifulSoup(p.get("cooked", ""), "lxml").get_text(" ", strip=True)
        out_posts.append({
            "date": created.date().isoformat(), "author": p.get("username"),
            "likes": p.get("like_count", 0) if "like_count" in p else sum(a.get("count", 0) for a in p.get("actions_summary", []) if a.get("id") == 2),
            "url": f"{BASE}/t/{slug}/{tid}/{p.get('post_number')}",
            "text": text[:1800],
        })
    return {
        "found": True, "title": t.get("title"), "url": f"{BASE}/t/{slug}/{tid}",
        "total_posts": meta.get("posts_count"),
        "attention": {"posts_last_period": recent, "posts_prior_period": prior,
                      "period_days": lookback_days,
                      "note": "counted within the last ~%d posts fetched" % max_posts},
        "posts": out_posts,
    }
