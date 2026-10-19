"""Exchange filings: every BSE announcement since the last run, tagged.

Pipeline: fetch -> drop junk -> keep your market-cap band (+ watchlist) ->
regex tags -> optional Gemini second opinion -> first-ever-concall check ->
store filings + signals.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from . import db
from .tags import KILL_TAGS, TAG_INFO, info

log = logging.getLogger(__name__)

API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
WARM = "https://www.bseindia.com/corporates/ann.html"

# ---------------------------------------------------------------- junk
JUNK = re.compile(
    r"trading window|loss of (share )?certificates?|duplicate (share )?certificates?|issue of duplicate|"
    r"reg(ulation)?\.? ?74 ?\(5\)|newspaper (publication|advertisement|ad)|book closure|record date|"
    r"scrutini[sz]er|voting results|proceedings of (the )?(\d+\w* )?(annual|extra.?ordinary)|"
    r"compliance certificate|structured digital database|\bsdd\b|investor complaints|"
    r"certificate under reg(ulation)?\.? ?(7|40)|transfer of (physical )?shares|transmission of shares|"
    r"change in (registered office|registrar)|shareholding pattern|corporate governance report|"
    r"esop|esos|employee stock option|intimation of (the )?(\d+\w* )?annual general meeting|"
    r"notice of (the )?(\d+\w* )?(agm|annual general meeting|postal ballot)|annual report|"
    r"reconciliation of share capital|disclosure of related party|related party transactions|"
    r"reaffirm|demat|kyc|unclaimed (dividend|shares)|iepf|cut.off date|e.?voting|"
    r"copy of (the )?(newspaper|advertisement)|closure of (the )?trading", re.I)

NEG_DEFAULT = re.compile(r"\bnil\b|no default|not (been )?default|without any default|no delay|"
                         r"timely (payment|paid)|paid on (time|due date)|has been paid|have been paid", re.I)
REGULATOR_ORDER = re.compile(r"\b(gst|tax|income.?tax|court|tribunal|nclt|nclat|sebi|commissioner|penalty|"
                             r"demand|adjudicat\w*|assessment|appellate|show cause|authority|department)\b", re.I)
RESIGN = re.compile(r"resign|cessation|ceased|steps? down|stepping down", re.I)

P = {
    "results": r"\b(financial results?|quarterly results?|(un)?audited (standalone |consolidated )?(financial )?results)\b",
    "order_win": r"\b(bag(s|ged|ging)?|secur(e|es|ed|ing)|receiv(e|es|ed|ing)|won|wins?|award(s|ed)?)\b[^|]{0,80}?"
                 r"\b(orders?|contracts?|work orders?|purchase orders?|loa|letters? of (award|intent)|mandates?)\b"
                 r"|\border(s)? (inflow|win|worth|valued|aggregating|received)\b"
                 r"|award.of.order|receipt.of.order|letter of award|\bloa\b",
    "capex_announced": r"capacity expansion|expansion of (the )?(\w+ )?capacit|expand(ing|s)? (its |the )?(\w+ )?capacit|"
                       r"new (manufacturing )?(plant|unit|facility|factory|line)\b|greenfield|brownfield|"
                       r"capital expenditure|\bcapex\b|set(ting)?[- ]?up (of )?(a )?(new )?(manufacturing )?"
                       r"(plant|unit|facility|factory)|debottleneck",
    "commissioning": r"commencement of (commercial )?(production|operations?)|commenced (commercial )?(production|operations?)|"
                     r"commercial production|commission(ed|ing) of|successfully commissioned|trial (run|production)|"
                     r"start(ed)? of production|(plant|unit|facility) (is |has become |became )?operational|"
                     r"commercial operations? date|\bcod\b",
    "new_product": r"launch(es|ed|ing)? (of )?(a |its |the )?(new )?(product|brand|range|variant|model)|new product|"
                   r"product launch|(us ?fda|eu ?gmp|who.?gmp|dcgi|cdsco|anda|mhra|tga|pmda|bis) (approval|nod|clearance|certification)|"
                   r"approval (from|by) (the )?(us ?fda|dcgi|cdsco|mhra)|final approval .{0,30}(anda|fda)|"
                   r"patent (granted|grant)|commerciali[sz](ation|ed|es) of",
    "new_geography": r"export (orders?|contracts?)|international (orders?|contracts?|markets?)|overseas (orders?|contracts?|markets?|subsidiary)|"
                     r"foray into|enter(s|ed|ing)? (into )?(the )?[a-z ]{0,25}\b(markets?|region)\b|new (geograph\w*|markets?|countr\w*)|"
                     r"subsidiary (in|at) (usa|u\.s\.|the us|uk|u\.k\.|uae|dubai|singapore|europe|germany|netherlands|africa|"
                     r"kenya|nigeria|australia|canada|japan|vietnam|indonesia|saudi|qatar|oman|mexico|brazil|bangladesh|"
                     r"sri lanka|nepal|thailand|malaysia|poland|spain|italy|france)",
    "concall": r"earnings?\s+(conference\s+)?call|conference\s+call|con[- ]?call|investors?\s+call|analysts?\s+call|"
               r"post.?results?\s+call|earnings\s+call",
    "investor_meet": r"(analysts?|institutional investors?|investors?)\s+meet|investor (day|conference)",
    "warrants_preferential": r"\bwarrants?\b|preferential (issue|allotment|basis|offer)",
    "change_in_control": r"open offer|change in (the )?control|substantial acquisition of shares|"
                         r"regulation ?(3|4)\b.{0,30}(sast|takeover)|reg\.? ?3 ?\(1\)|share purchase agreement|takeover",
    "buyback": r"buy[- ]?back",
    "voluntary_update": r"business update|operational update|monthly (sales|business|update|production)|"
                        r"operational (highlights|performance)|key (operating|business) (highlights|metrics)|"
                        r"production (update|numbers)",
    "acquisition": r"acqui(re|res|red|ring|sition) (of )?(a |an |the )?(\d+(\.\d+)?\s?%|stake|equity|business|company|majority|controlling)",
    "fund_raise": r"\bqip\b|qualified institutions? placement|rights issue|fund rais(e|ing)|raising of funds",
    "pledge_released": r"(release|revocation|withdrawal) of (pledge|encumbrance)|pledge (released|revoked)",
    "pledge_created": r"(creation|invocation) of (pledge|encumbrance)|pledge (created|invoked)|encumbrance (created|creat)",
    "rating_upgrade": r"upgrad\w*[^|]{0,60}rating|rating[^|]{0,60}upgrad\w*|outlook (revised |changed )?(to|as) positive",
    "rating_downgrade": r"downgrad\w*[^|]{0,60}rating|rating[^|]{0,60}downgrad\w*|watch with negative|outlook (revised |changed )?(to|as) negative",
    "regulatory_action": r"show cause notice|sebi (order|interim order|adjudicat\w*)|adjudication order|search (and|&) seizure|"
                         r"search operation|income.?tax (search|survey)|survey (was )?(conducted|under section)|"
                         r"enforcement directorate|\bed\b (search|raid)|dggi|suspension of trading|"
                         r"order (passed )?by (sebi|cci|sfio)",
    "tax_or_penalty_order": r"(gst|income.?tax|tax|customs|excise)[^|]{0,40}(demand|order|penalty|notice)|penalty (imposed|levied|of)",
    "fraud_disclosure": r"\bfraud\b",
    "forensic_audit": r"forensic (audit|auditor)|siphon\w*|misappropriat\w*|\bsfio\b",
    "insolvency": r"insolvency|\bcirp\b|\bnclt\b[^|]{0,60}(admit\w*|petition|application)|\bibc\b|liquidation",
}
PATTERNS = {k: re.compile(v, re.I) for k, v in P.items()}
CONCALL_RE = PATTERNS["concall"]

LEADER = r"(chief executive|\bceo\b|managing director|\bmd\b|chief financial|\bcfo\b|whole.?time director|" \
         r"executive director|chief operating|\bcoo\b|president)"
LEADER_RE = re.compile(LEADER, re.I)


def is_junk(text: str) -> bool:
    return bool(JUNK.search(text))


def regex_tags(text: str) -> set[str]:
    t = text or ""
    tags = {k for k, rx in PATTERNS.items() if rx.search(t)}

    # business orders vs orders FROM a tax office / court / regulator
    if "order_win" in tags and REGULATOR_ORDER.search(t):
        tags.discard("order_win")
    if "new_geography" in tags and re.search(r"market.?mak", t, re.I):
        tags.discard("new_geography")
    if "pledge_released" in tags:
        tags.discard("pledge_created")
    if "rating_upgrade" in tags and "rating_downgrade" in tags:
        tags -= {"rating_upgrade", "rating_downgrade"}
    if "tax_or_penalty_order" in tags and "regulatory_action" in tags:
        tags.discard("tax_or_penalty_order")
    if "forensic_audit" in tags:
        tags.discard("fraud_disclosure")

    # management changes
    if RESIGN.search(t):
        if re.search(r"auditor", t, re.I) and (re.search(r"statutory auditor", t, re.I) or
                                               not re.search(r"(secretarial|internal|cost) auditor", t, re.I)):
            tags.add("auditor_exit")
        if re.search(r"independent director", t, re.I):
            tags.add("independent_director_exit")
        elif LEADER_RE.search(t):
            tags.add("kmp_exit")
        elif re.search(r"company secretary|compliance officer", t, re.I):
            tags.add("cs_exit")
    if re.search(r"appoint", t, re.I) and LEADER_RE.search(t):
        tags.add("new_leadership")

    # defaults: companies file routine 'nil default' statements
    if re.search(r"\b(default|delay) in (the )?(payment|repayment|servicing)|non[- ]?payment of (interest|principal)|"
                 r"\bdefaulted\b|default (on|in) (interest|principal|debt|loan|ncd)", t, re.I) and not NEG_DEFAULT.search(t):
        tags.add("default")
    if "insolvency" in tags and re.search(r"(filed|files|filing|initiated|initiates)[^|]{0,60}against", t, re.I) \
            and not re.search(r"against the company", t, re.I):
        tags.discard("insolvency")  # the company is the creditor, filing against a customer
    return tags


# ---------------------------------------------------------------- amounts
AMT = re.compile(r"(?P<cur>rs\.?|inr|₹|usd|us\$|\$|eur|€)\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
                 r"(?P<unit>crores?|crs?\b\.?|lakhs?|lacs?|millions?|mn\b|mio\b|billions?|bn\b|thousand|k\b)?", re.I)
AMT_NOCUR = re.compile(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>crores?|crs?\b|lakhs?|lacs?)", re.I)


def parse_amount_cr(text: str, usd_inr: float = 88.0, first: bool = False) -> float | None:
    """Largest (or, with first=True, the first-mentioned) money amount in the text, in ₹ crore."""
    vals = []

    def conv(num: str, unit: str | None, cur: str | None):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            return None
        u = (unit or "").lower().rstrip(".")
        c = (cur or "rs").lower()
        fx = usd_inr if c in ("usd", "us$", "$") else usd_inr * 1.08 if c in ("eur", "€") else 1.0
        if u.startswith("cr"):
            return v if fx == 1.0 else None  # 'USD x crore' is too ambiguous to trust
        if u.startswith(("lakh", "lac")):
            return v / 100 if fx == 1.0 else None
        if u.startswith(("million", "mn", "mio")):
            return v * fx / 10
        if u.startswith(("billion", "bn")):
            return v * fx * 100
        if u.startswith(("thousand", "k")):
            return v * fx / 1e4
        return v * fx / 1e7  # plain rupees / dollars

    for m in AMT.finditer(text or ""):
        x = conv(m.group("num"), m.group("unit"), m.group("cur"))
        if x:
            vals.append((m.start(), x))
    for m in AMT_NOCUR.finditer(text or ""):
        x = conv(m.group("num"), m.group("unit"), None)
        if x:
            vals.append((m.start(), x))
    vals = [(p, v) for p, v in vals if 0.01 <= v <= 500_000]
    if not vals:
        return None
    return round(min(vals)[1] if first else max(v for _, v in vals), 2)


# ---------------------------------------------------------------- fetching
def fetch_range(http, start: date, end: date, scrip: str = "", max_pages: int = 150) -> list[dict]:
    out, page = [], 1
    while page <= max_pages:
        data = http.get(API, params={"pageno": page, "strCat": "-1", "strPrevDate": start.strftime("%Y%m%d"),
                                     "strScrip": scrip, "strSearch": "P", "strToDate": end.strftime("%Y%m%d"),
                                     "strType": "C", "subcategory": "-1"}, warm=WARM)
        rows = (data or {}).get("Table") or []
        out += rows
        total = int((rows[0].get("TotalPageCnt") if rows else 0) or 0)
        if not rows or page >= total:
            break
        page += 1
    return out


def _text(r: dict) -> str:
    return " | ".join(str(r.get(k) or "") for k in ("SUBCATNAME", "CATEGORYNAME", "HEADLINE", "NEWSSUB", "MORE"))


def _strip_name(text: str, name: str) -> str:
    """Remove the company's own name, so 'Global Exports Ltd' does not look like an export order.

    Whole words only: 'Audit Co Ltd' must not turn 'Auditor resigned' into 'or resigned'.
    A one-word core name ('Capex' in 'Capex Co Ltd') goes only together with Co / Ltd / Limited.
    """
    if not name:
        return text
    core = re.sub(r"\b(ltd|limited|pvt|private|india|industries|co|company|corporation|corp)\b\.?", "", name,
                  flags=re.I).strip(" .-&")
    alts = [re.escape(name.strip())]
    if len(core.split()) >= 2:
        alts.append(re.escape(core))
    if core:
        alts.append(re.escape(core) + r"\s+(?:co|company|ltd|limited|corp|corporation|industries|india)\b\.?")
    for a in alts:
        text = re.sub(r"(?<!\w)" + a + r"(?!\w)", " ", text, flags=re.I)
    return text


def _url(r: dict) -> str:
    a = (r.get("ATTACHMENTNAME") or "").strip()
    return ATTACH + a if a else (r.get("NSURL") or "")



# ---------------------------------------------------------------- Screener announcements feed
SCREENER_FEEDS = {"important": "https://www.screener.in/announcements/",
                  "all": "https://www.screener.in/announcements/all/",
                  "results": "https://www.screener.in/announcements/results/"}


def _ist_today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Kolkata")).date()


def _header_date(text: str, today: date) -> date | None:
    """Screener groups items under 'Today', 'Yesterday' or 'Oct 09, 2026'."""
    t = " ".join((text or "").split())
    low = t.lower()
    if low.startswith("today"):
        return today
    if low.startswith("yesterday"):
        return today - timedelta(days=1)
    for f in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%b %d %Y"):
        try:
            return datetime.strptime(t, f).date()
        except ValueError:
            continue
    return None


def parse_screener_feed(html: str, today: date | None = None, carry: date | None = None):
    """Announcement items (BSE-shaped dicts), the 'Show more' query for the next page, and the last date seen.

    Only the newest items carry a <time>; older ones get their date from the group header above them
    ('Today' / 'Yesterday' / 'Oct 09, 2026'). Without this, most of yesterday's filings were lost.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    today = today or _ist_today()
    current = carry
    out = []
    for el in soup.select("div.announcement-item, div.sub"):
        if "announcement-item" not in (el.get("class") or []):
            if el.find_parent(class_="announcement-item") is None:  # a group header, not an item summary
                d = _header_date(el.get_text(" ", strip=True), today)
                current = d or current
            continue
        it = el
        comp = it.select_one("a[href^='/company/']")
        doc = next((a for a in it.find_all("a", href=True) if not a["href"].startswith("/company/")), None)
        if comp is None or doc is None:
            continue
        m = re.match(r"/company/([^/]+)/", comp["href"])
        t = doc.find("time")
        summary = doc.select_one("div.sub")
        head = doc.find(string=True, recursive=False)
        headline = " ".join((head or "").split()) or doc.get_text(" ", strip=True)[:300]
        url = doc["href"]
        when = (t.get("datetime") if t else "") or (f"{current.isoformat()}T00:00:00" if current else "")
        out.append({"NEWSID": "scr:" + re.sub(r"\W", "", url)[-80:], "SCRIP_CD": m.group(1).upper(),
                    "SLONGNAME": comp.get_text(" ", strip=True), "HEADLINE": headline,
                    "MORE": summary.get_text(" ", strip=True) if summary else "",
                    "SUBCATNAME": "", "NSURL": url, "ATTACHMENTNAME": "", "NEWS_DT": when})
    nxt = None
    btn = soup.select_one("button[onclick*='ajaxLoad']")
    if btn:
        q = re.search(r"ajaxLoad\(event,\s*'([^']+)'", btn["onclick"])
        nxt = q.group(1).replace("&amp;", "&") if q else None
    return out, nxt, current


def fetch_screener(br, start: date, feed: str = "important", max_pages: int = 60) -> list[dict]:
    from bot import access
    base = SCREENER_FEEDS.get(feed, SCREENER_FEEDS["important"])
    rows, url, carry, pages = [], base, None, 0
    for _ in range(max_pages):
        items, nxt, carry = parse_screener_feed(br.goto(url), carry=carry)
        pages += 1
        if "/login" in (br.page.url or ""):
            access.record("screener", f"{feed} feed", None, False, "LOGGED OUT", base)
            raise RuntimeError("Screener session expired (log in again)")
        rows += [r for r in items if (r["NEWS_DT"] or "9999")[:10] >= start.isoformat()]
        dated = [r["NEWS_DT"][:10] for r in items if r["NEWS_DT"]]
        if not items or not nxt or (dated and min(dated) < start.isoformat()):
            break
        url = base + nxt
    access.record("screener", f"{feed} feed", None, True,
                  f"{len(rows)} filings since {start:%d %b} ({pages} page{'s' if pages != 1 else ''})", base)
    return rows

# ---------------------------------------------------------------- first-ever concall
def is_first_concall(http, code: str, event_day: date) -> bool | None:
    before = event_day - timedelta(days=30)
    if db.has_concall_before(code, before.isoformat()):
        return False
    cached = db.concall_cache_get(code)
    if cached is not None:
        if cached["has_prior"]:
            return False
        if (date.today() - date.fromisoformat(cached["checked"])).days < 30:
            return True
    try:
        rows = fetch_range(http, event_day - timedelta(days=3 * 365), before, scrip=code, max_pages=15)
    except Exception as e:
        log.warning("concall history for %s failed: %s", code, e)
        return None
    prior = any(CONCALL_RE.search(_text(r)) for r in rows)
    db.concall_cache_set(code, prior)
    return not prior


# ---------------------------------------------------------------- LLM second opinion
LLM_PROMPT = """You classify Indian stock-exchange (BSE) corporate announcements for an equity investor.
For each line "i | company | category | headline", return the tags that truly apply, from this list only:
{vocab}
Rules: an "order" from a tax office, court, tribunal or regulator is NOT order_win (use tax_or_penalty_order or
regulatory_action). Routine 'nil default' or 'no default' statements are NOT default. A company filing insolvency
against its customer is NOT insolvency. Reaffirmed ratings get no tag. If nothing applies, return an empty list.
If an order/contract value is stated, give it in INR crore as order_cr (USD at {usd_inr}).
If a capex / capacity-expansion amount is stated, give it in INR crore as capex_cr.
"change" = true only if the filing shows a real change in the business story (material order, capacity going live,
new product / approval, new geography, new CEO/MD from outside, promoter putting in money, first-ever concall).
Routine compliance, newspaper ads, trading-window notices and small repeat orders are change = false.
"note" = max 15 words, the fact that matters, with numbers.
"importance" = 0-10 for an investor in Indian small and mid caps:
  9-10 could change the company's value or is a serious red flag (auditor exit, default, fraud, pledge invoked,
       order or capex that is large for the company, plant commissioned, change in control, regulator raid);
  6-8  a real business change worth reading (order, new product approval, new market, new CEO, warrants to promoters);
  3-5  minor news (small repeat order, routine expansion talk, investor meet);
  0-2  routine (compliance, notices, ads, intimations).
"why" = max 15 words: why it matters to the investor (empty if importance < 3).
Return JSON: {{"items": [{{"i": 0, "tags": [], "order_cr": null, "capex_cr": null, "change": false, "note": "",
"importance": 0, "why": ""}}]}}

LINES:
{lines}"""


def _parse_llm_items(text: str) -> dict[int, dict]:
    from bot.llm_util import parse_json
    out: dict[int, dict] = {}
    for x in (parse_json(text or "").get("items") or []):
        if isinstance(x, dict) and "i" in x:
            try:
                try:
                    imp = max(0, min(10, int(round(float(x.get("importance"))))))
                except (TypeError, ValueError):
                    imp = None
                out[int(x["i"])] = {"tags": {t for t in (x.get("tags") or []) if t in TAG_INFO},
                                    "order_cr": x.get("order_cr"), "capex_cr": x.get("capex_cr"),
                                    "change": bool(x.get("change")), "note": str(x.get("note") or "")[:200],
                                    "importance": imp, "why": str(x.get("why") or "")[:200]}
            except (TypeError, ValueError):
                pass
    return out


def _lines(chunk: list[dict], start: int) -> str:
    return "\n".join(f"{start + j} | {it['name'][:60]} | {(it.get('subcat') or '')[:60]} | "
                     f"{(it['headline'] + ' -- ' + it.get('more', ''))[:600]}" for j, it in enumerate(chunk))


def llm_tags_claude(items: list[dict], usd_inr: float, batch: int = 60) -> dict[int, dict]:
    """Claude Haiku reads every filing headline + summary (cheap, fast, same bill as the thesis).

    Goes through bot.ai.claude: the Anthropic SDK 1.x has no 'temperature' argument any more, and the
    old direct call failed on every batch with the new SDK.
    """
    from bot import ai
    from bot.config import env
    model = env("CLAUDE_FAST_MODEL", "claude-haiku-4-5-20251001")
    vocab = ", ".join(sorted(TAG_INFO))
    out: dict[int, dict] = {}
    for start in range(0, len(items), batch):
        chunk = items[start:start + batch]
        try:
            text = ai.claude("You classify stock-exchange filings. Reply with one JSON object only.",
                             LLM_PROMPT.format(vocab=vocab, usd_inr=usd_inr, lines=_lines(chunk, start)),
                             model=model, max_tokens=8000)
            out.update(_parse_llm_items(text))
        except Exception as e:
            log.warning("Claude filing reader failed for a batch: %s", e)
    return out


def llm_tags(items: list[dict], usd_inr: float, reader: str = "claude") -> dict[int, dict]:
    if reader == "claude":
        return llm_tags_claude(items, usd_inr)
    return llm_tags_gemini(items, usd_inr)


def llm_tags_gemini(items: list[dict], usd_inr: float) -> dict[int, dict]:
    from google import genai
    from google.genai import types
    from bot.config import env
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    vocab = ", ".join(sorted(TAG_INFO))
    out: dict[int, dict] = {}
    for start in range(0, len(items), 120):
        chunk = items[start:start + 120]
        lines = _lines(chunk, start)
        try:
            resp = client.models.generate_content(
                model=env("GEMINI_MODEL", "gemini-3.5-flash"),
                contents=LLM_PROMPT.format(vocab=vocab, usd_inr=usd_inr, lines=lines),
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.0))
            out.update(_parse_llm_items(resp.text or ""))
        except Exception as e:
            log.warning("LLM filing classification failed for a batch: %s", e)
    return out


# ---------------------------------------------------------------- ingest
def ingest(http, cfg: dict, watch: set[str], dry: bool = False, today: date | None = None) -> dict:
    today = today or date.today()
    h = cfg["hunt"]
    fcfg = h.get("filings", {})
    lo, hi = h.get("market_cap_cr", [100, 25000])
    usd_inr = float(fcfg.get("usd_inr", 88))
    overrides = h.get("tag_weights") or {}

    last = db.get_state("last_filing_date")
    start = today - timedelta(days=1)
    if last:
        start = max(date.fromisoformat(last), today - timedelta(days=int(fcfg.get("max_lookback_days", 4))))
    source = fcfg.get("source", "screener")
    if source == "screener":
        rows = fetch_screener(http.br, start, fcfg.get("screener_feed", "important"))
    else:
        rows = fetch_range(http, start, today)
    master = db.master_map()
    stats = {"fetched": len(rows), "junk": 0, "out_of_band": 0, "already_seen": 0, "relevant": 0, "tagged": {}}

    items = []
    for r in rows:
        nid = str(r.get("NEWSID") or "")
        if not nid or db.filing_exists(nid):
            stats["already_seen"] += 1
            continue
        code = str(r.get("SCRIP_CD") or "").strip()
        m = master.get(code) or (db.master_by_slug(code) if source == "screener" else None) or {}
        slug = m.get("slug") or code
        mcap = m.get("mcap")
        if mcap is not None and not (lo <= mcap <= hi) and slug not in watch and code not in watch:
            stats["out_of_band"] += 1
            continue
        name = (r.get("SLONGNAME") or m.get("name") or code).strip()
        text = _strip_name(_text(r), name)  # 'Global Exports Ltd' must not look like an export order
        if is_junk(text):
            stats["junk"] += 1
            continue
        dt = str(r.get("NEWS_DT") or r.get("DT_TM") or today.isoformat())[:10]
        tags = regex_tags(text)
        items.append({"newsid": nid, "code": code, "slug": slug, "mcap": mcap, "name": name,
                      "dt": dt, "subcat": r.get("SUBCATNAME") or r.get("CATEGORYNAME") or "",
                      "headline": (r.get("HEADLINE") or r.get("NEWSSUB") or "").strip(),
                      "more": (r.get("MORE") or "")[:2000], "url": _url(r), "text": text, "tags": tags,
                      "order_cr": parse_amount_cr(text, usd_inr, first=True) if "order_win" in tags else None})
    stats["relevant"] = len(items)

    if items and fcfg.get("llm_classify", True) and not dry:
        llm = llm_tags(items, usd_inr, fcfg.get("reader", "claude"))
        stats["reader"] = f"{fcfg.get('reader', 'claude')}: {len(llm)}/{len(items)} read"
        for i, it in enumerate(items):
            if i in llm:
                it["tags"] = llm[i]["tags"] | (it["tags"] & KILL_TAGS)  # keep hard red flags no matter what
                it["change"], it["note"] = llm[i]["change"], llm[i]["note"]
                it["importance"], it["why"] = llm[i].get("importance"), llm[i].get("why")
                try:
                    it["capex_cr"] = float(llm[i]["capex_cr"]) if llm[i].get("capex_cr") else None
                except (TypeError, ValueError):
                    it["capex_cr"] = None
                if it.get("order_cr") is None and llm[i].get("order_cr"):
                    try:
                        it["order_cr"] = float(llm[i]["order_cr"])
                    except (TypeError, ValueError):
                        pass

    stats["changes"] = sum(1 for it in items if it.get("change")) if any("change" in it for it in items) else None
    max_mcap_first = float(fcfg.get("first_concall_max_mcap_cr", 5000))
    for it in items:
        if "concall" in it["tags"] and (it["mcap"] is None or it["mcap"] <= max_mcap_first):
            first = is_first_concall(http, it["code"], date.fromisoformat(it["dt"])) if source == "bse" else None
            if first:
                it["tags"].add("first_concall")
        db.save_filing(it)
        if "importance" in it:
            db.save_score(it["newsid"], it.get("importance"), it.get("why"), it.get("change"))
        for tag in it["tags"]:
            ti = info(tag, overrides)
            stats["tagged"][tag] = stats["tagged"].get(tag, 0) + 1
            if ti["w"] == 0:
                continue
            ref = f"first_concall:{it['code']}" if tag == "first_concall" else it["newsid"]
            db.add_signal(it["slug"], it["name"], tag, ti["w"], it["dt"],
                          detail={"headline": it["headline"][:300], "order_cr": it.get("order_cr"),
                                  "capex_cr": it.get("capex_cr"), "change": it.get("change"),
                                  "note": it.get("note"), "importance": it.get("importance"),
                                  "newsid": it["newsid"], "code": it["code"]},
                          url=it["url"], ref=ref)
    db.set_state("last_filing_date", today.isoformat())
    return stats
