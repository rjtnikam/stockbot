# Stockbot — autonomous hunter + thesis engine (v2)

Every weekday at 6:00 IST your Lightsail server wakes up, opens an invisible Chrome
that is already logged in as you, **hunts the whole market for candidates on its own**,
then writes a full thesis on the best few.

## Part 1 — The hunt (finds the stocks)

| Step | What happens | Cost |
|---|---|---|
| **Numbers net** | Your Screener queries run across all ~5,000 listed companies: revenue jump (YoY+QoQ), EPS acceleration, margin expansion (YoY+QoQ), profit inflection, turnaround, CWIP/gross-block expansion (capex ahead of demand), promoter buying. Edit them in `config.yaml`. | free |
| **Filings net** | Every BSE announcement since the last run (~1,000+/day). Junk dropped, your market-cap band kept, then tagged: order win (₹ value parsed), capex announced, **commissioning**, new product, new geography, **first-ever concall** (checked against 3 years of history), warrants/preferential, change in control, new leadership, buyback, voluntary updates. Red flags: auditor/CFO/ID exits, pledges, defaults, downgrades, regulator action, forensic audits. Gemini gives a second opinion on tags. | ~free |
| **Scoring with memory** | Signals accumulate for weeks and fade (half-life 45 days), so a story that is *building* rises. Combos earn bonuses: **capex → revenue conversion**, capex digestion, earnings inflection, coming-out party (first concall + numbers), skin in the game, material order (≥25% of revenue), new growth vector, turnaround. Kill-level red flags remove a company. Many tiny order announcements = promotion flag. | free |
| **Deep check (top 20)** | Company page: assets+CWIP growth vs revenue growth over 2 years, asset turnover, how capex was funded (cash flow / debt / dilution), quarterly acceleration, concall count. | free |
| **Documents (top 8)** | Gemini reads the actual PDFs: warrant allottees (promoters vs operators, price vs market), order size/customer/duration, capex amount/capacity/funding, and the **last 3 concall transcripts**: guidance policy (specific / directional / refuses), **say-do record**, candor vs evasions, **expensive signals**. Cached: never paid twice. | small |

You get a **Hunt Board** on Telegram: top 15 with score, trend (▲/▼/NEW), stage hint and the
reasons; red-flag alerts for your watchlist; and a list of any broken sources.

> **Already installed v1?** On the server: `cp config.yaml philosophy.md watchlist.txt ~/backup/`,
> then `unzip -o stockbot.zip` (your `.env`, `session.json` and `data/` are untouched), merge your own
> edits back into the new `config.yaml` / `philosophy.md`, and run step 7 below. No new packages needed.

## Part 2 — The thesis (top 4–5 per day)

Watchlist names with new filings go first (cockroach check), then hunt finalists, then watchlist
refreshes, then MarketSmith / your saved screens. For each stock:

| Who | Does what |
|---|---|
| **Python** | All numbers: Screener financials → your codex metrics (ROTNA, RR×ROIIC, Buffett interest cover, CFO/PAT, debtor days), reverse DCF, technicals (Weinstein stage, RS, base, trend template), price-vs-results timeline + chart |
| **Grok** | X (Twitter) threads, narratives, pump warnings — via xAI's built-in X search (no X login or X API needed) |
| **Gemini** | Google/news research with live search + digest of the ValuePickr thread |
| **Rules engine** | Story stage (Hidden → Inflection → Recognition → Consensus → Decay), price-vs-story gap, convergence odds (once base rates exist) |
| **Claude** | The thesis: why the hunter flagged it (each signal judged HOLDS UP / NOISE), story, numbers, management (say-do, candor, expensive signals), timeline, gap & convergence, bear case, reverse DCF, codex checklist, what to verify |

You get on Telegram: a headline, the memo, the timeline chart, and the full memo as a file.
Numbers never come from an LLM. Every external claim must carry a URL or it's dropped.

---

## v6 — clean cards, the council, quarterly results

**Update a running server:** copy the new zip, then `unzip -o ~/stockbot_v6.zip -d ~/ && bash ~/stockbot/deploy/update.sh`.

**The council (bots talk before any decision).** For every stock: Claude (chair) opens with a draft view and
questions → Grok answers from X → Gemini answers from the web and replies to Grok → Claude reacts → Grok and
Gemini give a final vote → Claude decides. The code blocks a decision without the discussion
(`council.require_discussion`). A failed member is shown as "absent". Transcripts: `data/discussions/`,
Telegram `/talk`, and the memo file. Ask your own question with `/team <question>` and watch every turn live.

**Clean Telegram card per stock:** verdict, the story in plain words, a numbers table (always from Screener /
MarketSmith data, never from an AI), where the story stands, the votes, what to watch, the main risk. The full
memo + discussion comes as one HTML file that opens on the phone.

**Quarterly results:** each run reads Screener's Results feed, opens company pages within a budget (your
watchlist + studied stocks first, then strong-looking results), and sends one results message. A strong result
from a company that passes the quality checks becomes a signal for the scan. `/results` and `/results SYMBOL`.

**Evening filing observer (Mon–Sat 21:30 IST):** reads all of the day's exchange filings and sends the important
ones. Who decides: rules drop junk → Claude Haiku (the reader) scores every filing 0–10 with a reason → Claude
Sonnet (the editor) picks max 10 with your philosophy (red flags first, then business changes, then your stocks) →
Grok checks X for the top 3 → free quality checks on Screener. It only ranks news; investment decisions stay with
the council. Telegram: `/filings` (last report), `/observe` (make it now). Settings: `observer:` in config.yaml.

**Proof of access:** every page the hidden browser opens on Screener or MarketSmith, and what it read from it,
goes into an access log (`access_log` table in `data/stockbot.db`, kept 90 days). You see it as: the "🔐 Data read"
line on every stock card, the login line on the morning scan, `/access` (today) and `/access SYMBOL` in Telegram,
and a "Data sources" section in the memo file.

**Filing dates fix:** Screener shows a time only on the newest filings; older ones sit under "Yesterday" or a
date header. v6 reads those headers, so the scan now sees all of yesterday's filings.

## Tonight's setup (about 60–90 minutes)

> Assumes your Lightsail instance runs **Ubuntu**. Commands marked **[laptop]** run on your
> laptop (PowerShell on Windows works); **[server]** run over SSH on Lightsail.

### 1. Get the keys (15 min)
- **Anthropic:** console.anthropic.com → API keys. Add credit. Set a monthly spend limit.
- **Gemini:** aistudio.google.com → Get API key. Enable billing if you exceed the free tier.
- **xAI (Grok):** console.x.ai → API keys. Add credit.
- **Telegram:** open @BotFather → `/newbot` → copy the token. Then send any message to your new
  bot, and message @userinfobot to get your numeric chat id.

Set a spend cap in each console on day one. Check actual cost per stock after the first real run.

### 2. Copy the project to the server
**[laptop]** (download your Lightsail SSH key `.pem` from Lightsail → Account → SSH keys):
```
scp -i LightsailDefaultKey.pem stockbot.zip ubuntu@<SERVER_IP>:~/
ssh -i LightsailDefaultKey.pem ubuntu@<SERVER_IP>
```
**[server]**
```
sudo apt-get install -y unzip && unzip stockbot.zip && cd stockbot
```

### 3. Install everything + the 6 am schedule
**[server]**
```
bash deploy/setup.sh
```
This installs Python packages, headless Chrome, locks down `.env`, and enables a systemd
timer for **Mon–Fri 06:00 IST**. It runs even when your laptop is off.

### 4. One-time login (the only manual step)
**[laptop]** (needs Python; on Windows install from python.org):
```
pip install playwright
python -m playwright install chromium
python login_local.py
```
A Chrome window opens with Screener and MarketSmith. Log in to both (tick "remember me"),
return to the terminal, press Enter. Then send the session to the server:
```
scp -i LightsailDefaultKey.pem session.json ubuntu@<SERVER_IP>:~/stockbot/
```
**[server]** `chmod 600 ~/stockbot/session.json`

`session.json` = your logged-in cookies. Treat it like a password. When a site logs you out
(roughly monthly), the bot messages you on Telegram; repeat this step.

### 5. Paste the keys
**[server]** `nano .env` → fill in the five values → Ctrl+O, Enter, Ctrl+X.

### 6. Test, cheapest first
**[server]** First the health check: it verifies Python, packages, every key (free test calls),
Telegram (sends you a hello), the invisible Chrome, the Screener login and the 6 am timer,
and tells you what to fix first:
```
.venv/bin/python doctor.py
```
Then:
```
# a) numbers + technicals only, zero LLM cost (watchlist has Fredun's BSE code 539730)
.venv/bin/python run_daily.py --symbols 539730 --dry-run

# b) the real thing on one stock you know cold
.venv/bin/python run_daily.py --symbols 539730
```
Compare (b) against your own Fredun forensic work. If the bot misses what you found
(receivables reclassification, Ind AS 116 effects, cash-flow restatement), tighten
`philosophy.md` and the thesis instructions in `bot/thesis.py` **before** trusting it on unknowns.

### 7. Calibrate the hunter (5 min, do this before the first 6 am run)
**[server]**
```
.venv/bin/python run_daily.py --calibrate-hunt
```
It checks every hunting source live and prints (and sends to Telegram) a ✅/❌ list:
the BSE security master, the BSE filings feed, and **each Screener query** with how many
companies it returns. I couldn't test these against the live sites, so this step matters:

- A ❌ on a Screener query almost always means a ratio name differs from Screener's spelling.
  Open screener.in → *Create new screen*, type the ratio name to see the exact spelling, fix it
  in `config.yaml` → `hunt.numbers_queries`, rerun. (The failing page is saved in `data/raw/screen_<name>.html`.)
- A query returning 100+ companies is too loose (a "+" is shown): tighten its thresholds.
- A ❌ on BSE usually means the exchange is blocking the server; the bot already retries through
  the browser. If it still fails, tell Claude Code on the server what the error says.

Then a free dry run of the whole hunt (no AI cost), and look at the board on Telegram:
```
.venv/bin/python run_daily.py --hunt-only --dry-run
```

### 7b. Optional extra sources
Edit `config.yaml`:
- `screener.screens`: paste your saved-screen URLs (your own query stacks), in addition to the hunt.
- `marketsmith.lists`: paste the URLs of your Blue Dot / idea-list pages.

Then verify MarketSmith extraction once:
```
.venv/bin/python run_daily.py --calibrate-marketsmith
```
It prints the symbols it found. If it finds zero, it saved a screenshot plus raw data in
`data/raw/ms_list0.*`. Open Claude Code on the server and ask it to fix extraction using those files.

### 8. Done: it now runs itself
```
systemctl list-timers stockbot.timer      # next scheduled run
sudo systemctl start stockbot             # run right now, in the background
journalctl -u stockbot -f                 # watch the logs live
```
Reports pile up in `data/reports/`; every analysis is logged in `data/stockbot.db` for the feedback loop.

---

## This weekend: convergence odds (base rates)

Until this exists, the memo says "base rates not built" and gives no probabilities. That's deliberate.

1. Put a universe in `data/universe.txt`, one symbol per line. The Nifty 500 + Smallcap 250
   constituent CSVs from niftyindices.com are a good start; more stocks = better base rates.
2. **[server]** `.venv/bin/python -m event_study.convergence --build` → reads Screener pages slowly
   (polite delays; ~6s per stock, so 750 stocks ≈ 75 min). Run it inside `tmux` so it survives disconnects.
3. **[server]** `.venv/bin/python -m event_study.convergence --analyse` → writes `data/base_rates.json`.

From then on, every memo with a "story ahead of price" setup says things like:
"similar setups: N cases; P(price caught up within 180 days) = x; P(story broke first) = y".

Honest limits, also printed in the output: Screener pages show ~13 quarters, so v1 base rates
cover roughly one market regime, and delisted/collapsed companies are missing (survivorship bias
makes convergence look likelier than it is). Rerun quarterly; extend history over time.

The same universe file also upgrades the RS score to a true percentile rank (`rs_percentile`).

---

## Files

| File | What it is |
|---|---|
| `philosophy.md` | **Your rules.** Every bot reads it. Fill in the "TO FILL IN" section (The Sheet's criteria). |
| `config.yaml` | **Hunt settings + numbers queries + setup bonuses**, sources, limits, valuation assumptions |
| `watchlist.txt` | Red-flag alerts always; re-analysed on any new filing or every 14 days |
| `hunt/filings.py` | BSE feed, junk filter, filing tagger, ₹ order-value parser, first-ever-concall check |
| `hunt/tags.py` | **Weights of every filing signal and red flag** — tune here |
| `hunt/numbers_scan.py` | Runs your Screener queries across the market |
| `hunt/confirm.py` | Deep check: capex vs revenue, funding, quarterly acceleration |
| `hunt/documents.py` | Gemini reads warrant/order/capex PDFs and concall transcripts (say-do, candor, expensive signals) |
| `hunt/scoring.py` | Signals → score, setups, red-flag kills (every point is explained on the board) |
| `hunt/runner.py` | Orchestrates the hunt, renders the Hunt Board |
| `run_daily.py` | The daily run |
| `doctor.py` | Health check: what works, what to fix next (never prints your keys) |
| `login_local.py` | One-time login on your laptop |
| `bot/fundamentals.py` | Codex metrics + reverse DCF (fading-growth model) |
| `bot/technicals.py` | Weinstein stage, RS, base detection, trend template |
| `bot/timeline.py` | Price reaction to past results, lag correlations, next result date, chart |
| `bot/story_stage.py` | Stage call with evidence, gap direction, base-rate lookup |
| `bot/research_grok.py` / `research_gemini.py` / `valuepickr.py` | The research bots |
| `bot/thesis.py` | Claude's instructions — edit to change the memo |
| `event_study/convergence.py` | Base-rate builder (competing-risks survival analysis) |

## Change models
Edit `.env` (`CLAUDE_MODEL`, `GEMINI_MODEL`, `GROK_MODEL`). No code changes needed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Telegram "Login expired" | Repeat step 4 |
| "Screener returned no financials" | Screener changed its page layout; run with `--symbols X --dry-run`, check `data/raw/screener_X.html`, ask Claude Code to update `bot/screener.py` |
| MarketSmith finds 0 stocks | Step 7 calibration |
| Grok / Gemini error | Check key + credit; model names change, update `.env` |
| Run never happened | `systemctl list-timers`, `journalctl -u stockbot` |
| Not sure what's wrong | `.venv/bin/python doctor.py` and fix the first FAIL |
| Board says "SOURCE PROBLEMS" | Rerun `--calibrate-hunt`; fix the named query or source (step 7) |
| Board is full of junk names | Raise `min_score_finalist`, tighten queries, or lower a noisy tag's weight in `hunt/tags.py` |
| Board is empty | Results season matters: numbers queries light up in Feb, May, Aug, Nov. Loosen thresholds otherwise |

## Daily cost and time (rough)
- Hunt: ~25–120 exchange API calls + ~30–60 Screener page loads, 10–25 minutes, polite delays throughout.
- AI: Gemini filing tags (a few cents), Gemini document reading for 8 finalists, Claude theses for 4–5 stocks.
  Check real spend in each console after the first week and adjust `documents_top` / `max_stocks_per_run`.

## Safety rules built in
- Max 5 theses/day, random 3–7 s pauses between Screener pages, 1–2.5 s between exchange API calls, one run per day: behaves like a person, not a crawler.
- If a site logs you out, the bot pings you and carries on with what works; page failures are listed in the run summary. Nothing in the code attempts to bypass CAPTCHAs or blocks.
- No order placement here. This produces research; your Groww paper-trading bot stays separate until you decide to connect them.
- `.env` and `session.json` are `chmod 600`. Keep the Lightsail firewall to SSH only.

## Next upgrades (once v1 earns trust)
1. Bear bot: Gemini attacks Claude's thesis, Claude rebuts, you see both.
2. Feedback loop: score past memos vs. what price did (`data/stockbot.db`), weekly report of which stages/setups worked.
3. Hunt backtest: replay a year of filings + results and measure which setups actually preceded re-ratings; re-weight `tags.py` from evidence, not intuition.
4. Evening "filings flash": a 7:30 pm run that only alerts on commissioning / first concall / material orders / red flags.
5. Wire PURSUE verdicts into the Groww paper-trade bot.
