"""Clean messages: the stock card, the discussion view and the memo file.

Rule: every number in a card comes from the data (Screener / MarketSmith), never from an AI.
The AI writes only the words (story, risk, what to watch).
"""
from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

from . import fmt
from .council import ICON, NAME, STAGE, Discussion
from .telegram import esc

VERDICT = {"PURSUE": ("🟢", "PURSUE"), "WATCH": ("🟡", "WATCH"), "REJECT": ("🔴", "REJECT"),
           "NO DECISION": ("⚪", "NO DECISION"), "TEST": ("🧪", "TEST RUN — no AI used")}
STAGE_PLAIN = {
    "Hidden": "Early: the change has started, but few people see it.",
    "Inflection": "Turning point: the numbers have started to change.",
    "Recognition": "The market is starting to notice the story.",
    "Consensus": "Everyone knows the story; the price already shows it.",
    "Decay": "Late: the story is fading.",
}


def cmd(name: str, arg: str | None) -> str:
    """A one-tap Telegram command: '/talk_ACGL' (falls back to '/talk M&amp;M' for symbols Telegram cannot link)."""
    arg = str(arg or "")
    if arg and re.fullmatch(r"[A-Za-z0-9]+", arg) and len(name) + len(arg) < 31:
        return f"/{name}_{arg}"
    return f"/{name} {esc(arg)}".rstrip()


def short_key(d: Discussion) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", d.id)


def _clean(s) -> str:
    s = re.sub(r"\*\*|__|`", "", str(s or ""))
    return re.sub(r"\s+\n", "\n", s).strip()


def _first_sentence(s: str, n: int = 110) -> str:
    s = _clean(s)
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    out = m.group(1) if m else s
    return out if len(out) <= n else out[: n - 1].rsplit(" ", 1)[0] + "…"


# ------------------------------------------------------------------ numbers (from data only)
def _r(label: str, value: str, extra: str = "") -> str:
    return f"{label:<9}{value:>10}  {extra}".rstrip()


def numbers_lines(n: dict) -> list[str]:
    q, a = n.get("quarterly") or {}, n.get("annual") or {}
    sh, tr = n.get("shareholding") or {}, n.get("top_ratios") or {}
    rows: list[str] = []
    periods = q.get("periods") or []
    sv, pv, opm = q.get("sales_values") or [], q.get("pat_values") or [], q.get("opm_pct") or []
    sy, py = q.get("sales_yoy") or [], q.get("pat_yoy") or []
    if periods and sv:
        rows.append(f"Latest quarter ({periods[-1]})")
        rows.append(_r("Sales", fmt.cr(sv[-1]), (fmt.pct(sy[-1]) + " YoY") if sy and sy[-1] is not None else ""))
        if pv:
            rows.append(_r("Profit", fmt.cr(pv[-1]), (fmt.pct(py[-1]) + " YoY") if py and py[-1] is not None else ""))
        if len(opm) >= 5 and fmt.num(opm[-1]) is not None:
            rows.append(_r("Margin", fmt.pctv(opm[-1]), f"{fmt.pctv(opm[-5])} yr ago"))
    if q.get("ttm_sales") is not None:
        rows.append("Last 12 months")
        rows.append(_r("Sales", fmt.cr(q.get("ttm_sales")), fmt.pct(fmt.growth(sv))))
        rows.append(_r("Profit", fmt.cr(q.get("ttm_pat")), fmt.pct(q.get("ttm_pat_growth"))))
    roce = [x for x in (a.get("roce_pct") or []) if fmt.num(x) is not None]
    p = sh.get("promoters")
    if roce or a.get("cfo_to_pat_5y") is not None or p:
        rows.append("Quality")
        if roce:
            rows.append(_r("ROCE", fmt.pctv(roce[-1]), f"Debt/Eq {fmt.plain_num(a.get('debt_to_equity'), 2)}"))
        if a.get("cfo_to_pat_5y") is not None:
            rows.append(_r("Cash/PAT", fmt.plain_num(a.get("cfo_to_pat_5y"), 2), "5-yr CFO ÷ profit"))
        if p:
            rows.append(_r("Promoter", fmt.pctv(p.get("latest")), f"{fmt.pts(p.get('change'))} (3y)"))
    rd, gap = n.get("reverse_dcf") or {}, n.get("expectations_gap") or {}
    if tr.get("Market Cap") is not None:
        rows.append("Price")
        rows.append(_r("Mcap", fmt.cr(tr.get("Market Cap")), f"P/E {fmt.plain_num(tr.get('Stock P/E'), 1)}"))
        imp = rd.get("implied_growth_10y")
        if isinstance(imp, (int, float)):
            did = gap.get("delivered_growth_used")
            rows.append(_r("Needs", fmt.pct(imp) + "/yr", "growth for 10 yrs"))
            if did is not None:
                rows.append(_r("Delivered", fmt.pct(did) + "/yr",
                               "past 5 yrs" if a.get("pat_cagr_5y") is not None else "past 3 yrs"))
    return rows


def numbers_block(n: dict) -> str:
    rows = numbers_lines(n)
    return "<pre>" + esc("\n".join(rows)) + "</pre>" if rows else "<i>No numbers available.</i>"


# ------------------------------------------------------------------ the stock card
def _verdict_key(card: dict, dry: bool) -> str:
    if dry:
        return "TEST"
    v = str(card.get("verdict") or "").upper().strip()
    return v if v in ("PURSUE", "WATCH", "REJECT") else "NO DECISION"


def discussion_lines(d: Discussion | None, card: dict) -> list[str]:
    if d is None:
        return []
    votes, absent, out = d.votes(), d.absent(), []
    for m in ("grok", "gemini"):
        if m in votes:
            v = votes[m]
            out.append(f"{ICON[m]} {NAME[m]}: <b>{esc(v['vote'] or '')}</b> — {esc(v['reason'])}")
        elif m in absent:
            out.append(f"{ICON[m]} {NAME[m]}: <i>absent ({esc(absent[m])})</i>")
        elif any(t.speaker == m and t.ok for t in d.turns):
            out.append(f"{ICON[m]} {NAME[m]}: spoke, gave no vote")
    v = _verdict_key(card, False)
    reason = _clean(card.get("decision_reason") or "")
    out.append(f"{ICON['claude']} Claude decided: <b>{esc(v)}</b>" + (f" — {esc(reason)}" if reason else ""))
    if (card.get("discussion") or {}).get("overruled_majority"):
        out.append("<i>Claude went against the majority (reason above).</i>")
    if card and card.get("discussion_checked") is False and v != "NO DECISION":
        out.append("<i>⚠️ Claude did not say which points from the discussion it used. Read /talk.</i>")
    return out


def sources_line(sources: dict | None) -> str:
    """'🔐 Data read: Screener ✅ 06:04 today (…) · MarketSmith ❌ no data …' — proof of access per stock."""
    from . import access
    if not sources:
        return ""
    bits = []
    for site, name in (("screener", "Screener"), ("marketsmith", "MarketSmith")):
        s = sources.get(site)
        if not s:
            continue
        mark = "✅" if s.get("ok") else "❌"
        at = f" {access.when(s.get('at'))}" if s.get("ok") else ""
        bits.append(f"{name} {mark}{esc(at)} ({esc(s.get('what') or '')})")
    return "🔐 <b>Data read</b>: " + " · ".join(bits) if bits else ""


def stock_card(name: str, sym: str, numbers: dict, card: dict, d: Discussion | None = None,
               dry: bool = False, stage: str | None = None, sources: dict | None = None) -> str:
    card = card or {}
    key = _verdict_key(card, dry)
    icon, label = VERDICT[key]
    conv = card.get("conviction")
    head = f"{icon} <b>{esc(label)}</b>" + (f" · conviction {esc(conv)}/5" if conv and key in ("PURSUE", "WATCH", "REJECT") else "")
    tr = (numbers or {}).get("top_ratios") or {}
    what = _clean(card.get("what_it_does")) or _first_sentence((numbers or {}).get("about") or "")
    sub = " · ".join(x for x in (esc(what), f"₹{tr['Market Cap']:,.0f} Cr" if fmt.num(tr.get("Market Cap")) else "",
                                 f"P/E {fmt.plain_num(tr.get('Stock P/E'), 0)}" if fmt.num(tr.get("Stock P/E")) else "") if x)
    parts = [f"{head}\n<b>{esc(name)}</b> ({esc(sym)})" + (f"\n{sub}" if sub else "")]
    if dry:
        parts.append("<i>Test run: the AI bots did not run. Only the numbers are real.</i>")
    elif key == "NO DECISION":
        parts.append("<i>" + esc(_clean(card.get("decision_reason")) or "No decision was taken.") + "</i>")
    if card.get("story"):
        parts.append("📖 <b>The story</b>\n" + esc(_clean(card["story"])))
    parts.append("🔢 <b>The numbers</b>\n" + numbers_block(numbers or {}))
    where = _clean(card.get("where_it_stands")) or STAGE_PLAIN.get(card.get("stage") or stage or "", "")
    if where:
        parts.append("⏳ <b>Where the story is</b>\n" + esc(where))
    dl = discussion_lines(d, card) if not dry else []
    if dl:
        parts.append("💬 <b>The discussion</b>\n" + "\n".join(dl))
    wn = [w for w in (card.get("watch_next") or []) if str(w).strip()][:3]
    if wn:
        parts.append("👀 <b>Watch next</b>\n" + "\n".join("• " + esc(_clean(w)) for w in wn))
    if card.get("main_risk"):
        parts.append("⚠️ <b>Main risk</b>\n" + esc(_clean(card["main_risk"])))
    sl = sources_line(sources)
    if sl:
        parts.append(sl)
    parts.append("📎 Full memo and the bots' discussion: the file below" + (f" · {cmd('talk', sym)}" if d else ""))
    return "\n\n".join(parts)


# ------------------------------------------------------------------ inputs for the council
def stock_facts(dossier: dict) -> str:
    n = dossier.get("numbers") or {}
    sym = dossier.get("symbol")
    codes = (dossier.get("codes") or {})
    ids = " / ".join(x for x in (f"NSE: {codes['nse']}" if codes.get("nse") else "",
                                 f"BSE: {codes['bse']}" if codes.get("bse") else "") if x)
    q = n.get("quarterly") or {}
    per, sv, sy = (q.get("periods") or [None])[-1], q.get("sales_values") or [], q.get("sales_yoy") or []
    pv, py = q.get("pat_values") or [], q.get("pat_yoy") or []
    latest = (f"Latest quarter ({per}): sales {fmt.cr(sv[-1])} ({fmt.pct(sy[-1])} YoY), net profit {fmt.cr(pv[-1])} "
              f"({fmt.pct(py[-1])} YoY)." if per and sv and pv and sy and py else "")
    tr = n.get("top_ratios") or {}
    return (f"COMPANY: {n.get('name') or sym} ({sym}{', ' + ids if ids else ''}), an Indian listed company.\n"
            f"What it does: {(n.get('about') or '')[:350]}\n"
            f"Market cap {fmt.cr(tr.get('Market Cap'))}, P/E {fmt.plain_num(tr.get('Stock P/E'), 1)}. {latest}\n")


def _top(items, k: int, keys=("point", "claim", "summary", "headline", "insight", "sign")) -> list[str]:
    out = []
    for it in (items or [])[:k]:
        if isinstance(it, dict):
            txt = next((it[x] for x in keys if it.get(x)), "")
            url = it.get("url")
            out.append(f"- {str(txt)[:220]}" + (f" ({url})" if url else ""))
        elif it:
            out.append(f"- {str(it)[:220]}")
    return out


def stock_brief(dossier: dict) -> str:
    n, t = dossier.get("numbers") or {}, dossier.get("technicals") or {}
    st, h, r = dossier.get("story_stage") or {}, dossier.get("hunt") or {}, dossier.get("research") or {}
    a = n.get("annual") or {}
    lines = [stock_facts(dossier)]
    if h:
        lines.append("WHY IT CAME UP: " + "; ".join(c.get("label", "") for c in (h.get("why_flagged") or [])[:6]))
        gate = ((h.get("deep_check") or {}).get("gate") or {})
        if gate:
            lines.append(f"QUALITY GATES: {gate.get('status')} ({gate.get('passed')}/{gate.get('total')} passed)")
    else:
        lines.append(f"WHY IT CAME UP: {dossier.get('candidate_source')}")
    lines.append("NUMBERS:\n" + "\n".join(numbers_lines(n)))
    lines.append(f"Interest cover {a.get('interest_coverage')}; RR x ROIIC {a.get('implied_growth_rr_x_roiic')}; "
                 f"FCF 5y {a.get('fcf_5y_sum_approx')}; debtor days {(a.get('debtor_days') or [None])[-1]}")
    ms = t.get("marketsmith") or {}
    if ms:
        lines.append(f"MARKETSMITH: RS {ms.get('rs_rating')}, EPS {ms.get('eps_rating')}, Master {ms.get('master_score')}, "
                     f"A/D {ms.get('acc_dis_rating')}, group rank {ms.get('industry_group_rank')} "
                     f"({ms.get('industry_group')}), market {ms.get('market_direction')}, base {ms.get('latest_base')}")
    else:
        lines.append(f"TECHNICALS (derived): Weinstein {(t.get('weinstein') or {}).get('stage')}, "
                     f"trend template {t.get('trend_template_passed')}/8")
    lines.append(f"STORY STAGE (rules): {st.get('stage')} ({st.get('stage_confidence')}); price vs story: "
                 f"{st.get('price_vs_story')}")
    x, w, vp = r.get("x") or {}, r.get("web") or {}, r.get("valuepickr") or {}
    if x and not x.get("error"):
        lines.append(f"X (earlier Grok scan): mention {x.get('mention_level')}, trend {x.get('mention_trend')}, "
                     f"who: {x.get('who_is_talking')}")
        lines += _top(x.get("bull_narratives"), 3) + _top(x.get("bear_narratives"), 3) + _top(x.get("pump_warning_signs"), 2)
    if w and not w.get("error"):
        lines.append(f"WEB (earlier Gemini scan): {str(w.get('story_now') or '')[:500]}")
        lines += _top(w.get("recent_news"), 4) + _top(w.get("red_flags"), 3) + _top(w.get("capacity_and_guidance"), 3)
    if vp and vp.get("found") is not False and not vp.get("error"):
        lines += ["VALUEPICKR:"] + _top(vp.get("bull_points"), 3) + _top(vp.get("bear_points"), 3)
    return "\n".join(x for x in lines if x)[:14000]


# ------------------------------------------------------------------ the discussion (Telegram /talk)
def discussion_message(d: Discussion) -> str:
    title = esc(d.name or d.sym or "Team question")
    blocks = [f"💬 <b>Discussion — {title}</b> · {esc(d.created[:16].replace('T', ' '))}"]
    if d.kind == "team":
        blocks.append("<i>Question: " + esc(d.topic[:600]) + "</i>")
    for t in d.turns:
        blocks.append(turn_message(t))
    if d.status not in ("decided",):
        blocks.append(f"<i>Status: {esc(d.status)}</i>")
    return "\n\n".join(blocks)


def discussion_folded(d: Discussion, limit: int = 1500) -> str:
    """The morning view: every turn in a folded quote (tap to open). One or two messages per stock."""
    title = esc(d.name or d.sym or "Team question")
    votes = d.votes()
    vote_line = " · ".join(f"{ICON[m]} {NAME[m]} {esc(v['vote'] or '')}" for m, v in votes.items())
    dec = d.decision.get("verdict") or ""
    head = f"💬 <b>The bots' discussion — {title}</b>\n" + (vote_line + (f" · {ICON['claude']} Claude {esc(dec)}" if dec else "")
                                                         if vote_line or dec else "<i>no votes</i>")
    blocks = [head + "\n<i>Tap a quote to open it.</i>"]
    for t in d.turns:
        who = f"{ICON[t.speaker]} <b>{NAME[t.speaker]}</b> · {STAGE.get(t.stage, t.stage)}"
        if not t.ok:
            blocks.append(f"{who}\n<i>could not answer: {esc(t.error)}</i>")
            continue
        text = re.sub(r"\n{2,}", "\n", _clean(t.text))  # one quote = one message block
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0] + " …"
        blocks.append(f"{who}\n<blockquote expandable>{esc(text)}</blockquote>")
    return "\n\n".join(blocks)


def turn_message(t) -> str:
    head = f"{ICON[t.speaker]} <b>{NAME[t.speaker]}</b> · {STAGE.get(t.stage, t.stage)}"
    body = esc(_clean(t.text)) if t.ok else f"<i>could not answer: {esc(t.error)}</i>"
    return f"{head}\n{body}"


def discussion_list(ds: list[Discussion]) -> str:
    if not ds:
        return "No discussions yet."
    lines = ["💬 <b>Recent discussions</b>"]
    for d in ds:
        dec = d.decision.get("verdict") or d.decision.get("headline") or d.status
        topic = d.name or d.sym or d.topic[:50]
        lines.append(f"• {esc(d.created[:10])} · <b>{esc(topic)}</b> — {esc(str(dec)[:80])}\n"
                     f"  {cmd('talk', short_key(d))}")
    return "\n".join(lines)


# ------------------------------------------------------------------ memo file (opens on the phone)
def _linkify(escaped: str) -> str:
    """Bare URLs in already-escaped text -> clickable links (skips URLs that are already inside a link)."""
    return re.sub(r'(?<!href=")(?<!">)(https?://[^\s<>()]+[^\s<>().,;:!?])',
                  lambda m: f'<a href="{m.group(1)}">{m.group(1)}</a>', escaped)


def _inline(s: str) -> str:
    s = html.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",  # text is already escaped: keep the URL as it is
               lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>', s)
    s = _linkify(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    return s


def md_to_html(md: str) -> str:
    """Small markdown converter: headings, bold, italics, links, lists, tables, paragraphs."""
    out, para, lst, table = [], [], None, []

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal lst
        if lst:
            tag, items = lst
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(i)}</li>" for i in items) + f"</{tag}>")
            lst = None

    def flush_table():
        if table:
            rows = [r for r in table if not re.fullmatch(r"[\s|:\-]+", r.strip())]  # drop |---|---| lines
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            if cells:
                h = "".join(f"<th>{_inline(c)}</th>" for c in cells[0])
                b = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
                out.append(f'<div class="t"><table><thead><tr>{h}</tr></thead><tbody>{b}</tbody></table></div>')
            table.clear()

    for raw in (md or "").splitlines():
        line = raw.rstrip()
        if line.strip().startswith("|"):
            flush_para(); flush_list()
            table.append(line)
            continue
        flush_table()
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            flush_para(); flush_list()
            lvl = min(len(m.group(1)) + 1, 4)
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            continue
        m = re.match(r"^\s*([-*•])\s+(.*)", line) or re.match(r"^\s*(\d+)[.)]\s+(.*)", line)
        if m:
            flush_para()
            tag = "ol" if m.group(1).isdigit() else "ul"
            if not lst or lst[0] != tag:
                flush_list()
                lst = (tag, [])
            lst[1].append(m.group(2))
            continue
        if not line.strip():
            flush_para(); flush_list()
            continue
        if re.fullmatch(r"\s*(-{3,}|\*{3,})\s*", line):
            flush_para(); flush_list()
            out.append("<hr>")
            continue
        flush_list()
        para.append(line.strip())
    flush_para(); flush_list(); flush_table()
    return "\n".join(out)


CSS = """:root{--bg:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e5e5ea;--card:#f5f5f7;--a:#0a66c2}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#f2f2f7;--mut:#a1a1a6;--line:#333;--card:#1c1c1e;--a:#64a8ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:18px 16px 60px}
h1{font-size:22px;margin:4px 0}h2{font-size:19px;margin:28px 0 8px;border-top:1px solid var(--line);padding-top:18px}
h3,h4{font-size:16px;margin:18px 0 6px}a{color:var(--a)}.mut{color:var(--mut);font-size:14px}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;font-weight:600;font-size:14px;background:var(--card)}
pre{background:var(--card);padding:12px;border-radius:10px;overflow-x:auto;font-size:13.5px;line-height:1.45}
.t{overflow-x:auto}table{border-collapse:collapse;font-size:14px;width:100%}th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}
.turn{background:var(--card);border-radius:12px;padding:10px 14px;margin:10px 0}.who{font-weight:600;margin-bottom:4px}
.turn p{margin:6px 0;white-space:pre-wrap}"""


def memo_file(path: Path, name: str, sym: str, memo_md: str, numbers: dict, card: dict,
              d: Discussion | None, dry: bool = False, sources: dict | None = None) -> str:
    from . import access
    src = ""
    for site, label in (("screener", "Screener"), ("marketsmith", "MarketSmith")):
        s = (sources or {}).get(site)
        if s:
            link_ = f' · <a href="{html.escape(s["url"], quote=True)}">open the page</a>' if s.get("url") else ""
            src += (f"<li>{'✅' if s.get('ok') else '❌'} <b>{label}</b>"
                    f"{' · read ' + access.when(s.get('at')) if s.get('ok') else ''} · {html.escape(s.get('what') or '')}{link_}</li>")
    key = _verdict_key(card or {}, dry)
    icon, label = VERDICT[key]
    conv = (card or {}).get("conviction")
    turns = ""
    if d is not None:
        for t in d.turns:
            body = _linkify(html.escape(_clean(t.text))) if t.ok else f"<i>could not answer: {html.escape(t.error)}</i>"
            turns += (f'<div class="turn"><div class="who">{ICON[t.speaker]} {NAME[t.speaker]} · '
                      f'{STAGE.get(t.stage, t.stage)}</div><p>{body}</p></div>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(name)} ({html.escape(sym)})</title>
<style>{CSS}</style></head><body><main>
<div class="pill">{icon} {html.escape(label)}{f" · conviction {conv}/5" if conv and key in ("PURSUE", "WATCH", "REJECT") else ""}</div>
<h1>{html.escape(name)} ({html.escape(sym)})</h1>
<div class="mut">{date.today():%d %b %Y} · numbers from Screener, technicals from MarketSmith</div>
<h2>The numbers</h2><pre>{html.escape(chr(10).join(numbers_lines(numbers or {})))}</pre>
<h2>The memo</h2>{md_to_html(memo_md)}
<h2>The discussion</h2>{turns or '<p class="mut">No discussion (test run).</p>'}
<h2>Data sources</h2>{'<ul>' + src + '</ul>' if src else '<p class="mut">Not recorded.</p>'}
</main></body></html>"""
    Path(path).write_text(doc, encoding="utf-8")
    return str(path)


# ------------------------------------------------------------------ proof of access (/access)
def access_message(sym: str | None = None) -> str:
    from . import access
    from .telegram import link
    if sym:
        rows = access.for_target(sym, 12)
        if not rows:
            return f"🔐 No record that the bot read data for {esc(sym)} (the log keeps 90 days)."
        lines = [f"🔐 <b>Data access — {esc(sym.upper())}</b>"]
        for r in reversed(rows):
            t = access.when(r["ts"])
            lines.append(f"• {esc(t)} {'✅' if r['ok'] else '❌'} {access.SITE_NAME.get(r['site'], esc(r['site']))} "
                         f"{esc(r['kind'])}: {esc(r['detail'])}" + (f" · {link(r['url'], 'open')}" if r["url"] else ""))
        return "\n".join(lines)
    acc = access.summary()
    lines = ["🔐 <b>Data access — today</b>"]
    if not acc:
        return lines[0] + "\n❌ The bot has not opened Screener or MarketSmith today."
    for site in ("screener", "marketsmith"):
        name = access.SITE_NAME[site]
        a = acc.get(site)
        if not a:
            lines.append(f"\n<b>{name}</b>: ❌ not opened today")
            continue
        login = {True: "✅ logged in", False: "❌ LOGGED OUT", None: "login not checked"}[a["login"]]
        lines.append(f"\n<b>{name}</b> · {login} · {a['pages']} page{'s' if a['pages'] != 1 else ''} opened "
                     f"({a['ok']} ok{', ' + str(a['failed']) + ' failed' if a['failed'] else ''})"
                     f" · {esc(access.hhmm(a['first']))}–{esc(access.hhmm(a['last']))}")
        data = [r for r in a["data"] if r["kind"] != "login"]
        bad = [r for r in data if not r["ok"]]
        for r in data[-10:]:
            tgt = f"{r['target']} — " if r["target"] else ""
            lines.append(f"• {esc(access.hhmm(r['ts']))} {'✅' if r['ok'] else '❌'} {esc(r['kind'])}: {esc(tgt)}{esc(r['detail'])}")
        if len(data) > 10:
            lines.append(f"<i>… {len(data) - 10} earlier reads not shown</i>")
        if bad:
            lines.append(f"⚠️ {len(bad)} read{'s' if len(bad) != 1 else ''} failed today")
    lines.append("\n/access SYMBOL — when the bot last read one company")
    return "\n".join(lines)
