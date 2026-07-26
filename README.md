# Hermes Lite — RSS → LLM summary → Telegram (MVP)

Fetches new articles from your RSS feed list, summarizes each with a free
LLM API (Groq or Gemini), and posts the summary + original link to a
Telegram chat via bot. Runs as a one-shot script, triggered by a scheduler.

Tested target: PC, Linux, Termux (Android), Raspberry Pi Zero W / 2W.
No platform-specific code — it's plain Python 3.

## 1. Install

```bash
git clone <your-repo-or-copy-this-folder>
cd hermes-lite
pip install -r requirements.txt      # on Termux: pkg install python && pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in:
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — create a bot with @BotFather,
  then message your bot once and hit
  `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your chat_id.
- `GROQ_API_KEY` (console.groq.com/keys) **or** `GEMINI_API_KEY`
  (aistudio.google.com/apikey) depending on `LLM_PROVIDER`.

Edit `feeds.txt` with the RSS feeds you actually want.

## 2. Run once manually (sanity check)

```bash
python news_bot.py
```

You should see console output listing new articles found, and messages
should land in your Telegram chat within a few seconds.

## 3. Schedule it

**Linux / RPi (cron):**
```bash
crontab -e
# run every 30 minutes:
*/30 * * * * cd /path/to/hermes-lite && /usr/bin/python3 news_bot.py >> run.log 2>&1
```

**Termux:**
```bash
pkg install cronie
crond
crontab -e
# same line as above, using Termux's python path
```

**PC (systemd timer)** — create `hermes-lite.service` + `hermes-lite.timer`
in `~/.config/systemd/user/` if you'd rather not use cron; ask me if you
want those two unit files written out.

## 4. How dedup works — and a bug that's now fixed

`seen.json` stores article URLs already sent or already evaluated. It's
capped at `SEEN_HISTORY_CAP` (default 8000) so it never grows unbounded —
oldest entries get trimmed off once you're over the cap.

**If you pulled this file before late July 2026 patches:** an earlier
version tracked `seen` as a plain Python `set` and trimmed it with
`list(seen)[-CAP:]`. That looks like "keep the most recent N" but isn't —
Python set iteration order has no relationship to insertion order, so
that trim was dropping an effectively *random* selection of URLs once
you exceeded the cap. If a dropped URL was still within
`MAX_ARTICLE_AGE_HOURS`, it would look "new" again on a later run — which
could re-trigger evaluation and, if it cleared the criticality threshold,
**re-send an article you'd already gotten.** This is now a proper
order-preserving store (`SeenStore`), so trimming always drops the
genuinely oldest entries and a sent article can't resurface this way.

## 5. Age filtering

Some CERT/advisory feeds return their entire historical archive on every
fetch, not just recent items — this is what caused the "7,893 new
articles" on an early run. Anything older than `MAX_ARTICLE_AGE_HOURS`
(default 72) is now skipped before it ever reaches triage, and marked
seen so it's never rechecked.

## 6. Category-aware criticality + recency tiebreak

Each feed is tagged with a category, taken straight from the section
headers in `feeds.txt` (News & research / Threat intelligence /
Government-CERT / Vulnerability research / Vendor advisories /
Newsletters) — no second config file to maintain. The triage prompt
sees this tag and scores accordingly: a CERT advisory is judged on
vulnerability severity, a newsletter roundup on how notable its curated
content is, and so on — "critical" means something different depending
on the source type.

Articles are sent to Telegram highest-score first. When two articles
tie on score, the more recent one wins — a 1-hour-old 8/10 goes out
before a 48-hour-old 8/10, but a higher score always beats a fresher
lower one.

## 7. Criticality filtering (two-stage, to save tokens)

Runs in two passes instead of one:

1. **Triage** — sends just the headlines (no article content) and asks for
   a criticality score 1-10 per headline. Cheap: tiny input, and the
   output is just a list of numbers, not summaries.
2. **Summarize** — only articles that clear `CRITICALITY_THRESHOLD`
   (default 5) get a second call, this time with full title + content, to
   generate the actual summary that gets sent.

Articles that don't clear the bar in stage 1 never get a summary
generated at all — no wasted output tokens on content you're going to
discard anyway. Articles that do clear it get sent to Telegram **most
critical first**, sorted by score before sending, so if you're watching
your phone in real time the highest-priority items land first.

Both stages are batched (`EVAL_BATCH_SIZE`, default 15) — one call
handles up to that many articles at once, same reasoning as before.

## 8. Known MVP limitations (by design, for v1)

- Uses the feed's own summary/description field, not the full article body
  — good enough for a decent summary, avoids scraping fragile page HTML.
- No retry/backoff on Telegram failures — a failed send is just logged
  and retried next run since it's not marked seen. LLM evaluation
  failures also aren't marked seen, for the same reason.
- `MAX_NEW_ARTICLES_PER_RUN` caps how many get *evaluated* per run (not
  how many get sent — that depends on how many clear the score
  threshold), so a large backlog won't blow through your free-tier rate
  limit all at once.
- Feeds are still processed in file order — if one feed has a big
  backlog, it can dominate a run before later feeds get reached (this
  hasn't been changed yet — say the word if you want round-robin instead).
- ESP32 family is intentionally out of scope for this version.
