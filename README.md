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

**PC (systemd timer)** — create `daily-news.service` + `daily-news.timer`
in `~/.config/systemd/user/` if you'd rather not use cron; ask me if you
want those two unit files written out.

## 4. How dedup works

`seen.json` stores article URLs already sent. It's capped at 2000 entries
(oldest trimmed first) so it never grows unbounded. Delete it to reset.

## 5. Known MVP limitations (by design, for v1)

- Uses the feed's own summary/description field, not the full article body
  — good enough for a decent summary, avoids scraping fragile page HTML.
- No retry/backoff on LLM or Telegram failures — a failed article is just
  skipped and logged; it'll be retried next run since it's not marked seen.
- `MAX_NEW_ARTICLES_PER_RUN` caps how many get sent per run, so a large
  batch of new articles won't blow through your free-tier rate limit or
  flood your chat all at once.
- ESP32 family is intentionally out of scope for this version.
