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

**PC (systemd timer)** — sample units are in `systemd/` (edit the paths
inside first, then copy to `~/.config/systemd/user/`):
```bash
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-lite.timer          # every 30 min
systemctl --user enable --now hermes-lite-digest.timer   # weekly recap, Mondays 9am
```

**GitHub Actions** — no server needed at all; runs on GitHub's own infra.
See `.github/workflows/hermes-lite.yml` — currently set for 10:20 AM and
8:15 PM IST. Setup:

1. Push this repo to GitHub (private, unless you're fine with `hermes.db`'s
   contents — sent article titles/scores/links, nothing sensitive — being
   publicly visible in commit history).
2. Repo Settings → Secrets and variables → Actions → New repository secret.
   Add `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GROQ_API_KEY`,
   `GEMINI_API_KEY` (whichever providers you're actually using) — **not**
   your local `.env` file, which stays out of git entirely (already in
   `.gitignore`).
3. That's it — the workflow runs on the schedule, and also has
   `workflow_dispatch` enabled so you can trigger it manually from the
   Actions tab to test it.

Two things specific to GitHub Actions, not cron/systemd:

- **State persistence.** Every run gets a brand-new, disposable VM — so
  unlike a Pi or PC where `hermes.db` just sits on disk between runs, here
  it would reset to empty every single time (re-triaging, and probably
  re-sending, everything) unless something persists it. The workflow
  commits `hermes.db` back to the repo as its last step, which is why
  `.gitignore` deliberately does NOT exclude it in this setup (see the
  comment there) and why the workflow needs `contents: write` permission.
- **Inactive-repo auto-disable.** GitHub automatically disables scheduled
  workflows after 60 days with no commits to the repo at all. In practice
  the bot's own state-commit every run keeps the repo "active," so this
  shouldn't bite you — but if you ever see it silently stop running, check
  the Actions tab for a "workflow disabled" notice first.

## 4. Secrets — will your API keys leak?

Not if they're stored as **GitHub Actions secrets**, not written in the
workflow file or committed anywhere:

- Secrets are encrypted at rest and only decrypted into the job's
  environment at run time — they're never visible in the repo, the
  workflow YAML, or to anyone browsing the code.
- GitHub automatically scans job logs for the literal value of every
  secret referenced via `${{ secrets.X }}` in that job and replaces it
  with `***` wherever it appears in output — including inside error
  messages, not just where you'd expect it.
- As defense-in-depth (log masking is a safety net, not something to rely
  on alone), this codebase specifically avoids putting the Gemini key in
  a URL query string (`?key=...`) — a failed request there would embed
  the key in plain text inside the exception message, which is a real,
  previously-reported issue with Gemini's REST API. It's sent via the
  `x-goog-api-key` header instead, which Google documents as fully
  equivalent and never puts the key anywhere a log line could echo it —
  this matters doubly for a local cron/systemd setup, since GitHub's log
  masking obviously doesn't apply to a `news_bot.log` file on your own disk.

What WOULD leak your keys, regardless of any of the above:
- Committing a `.env` file to the repo (already gitignored — don't
  override that).
- Printing `os.environ` or the raw request payload/headers for debugging
  and pushing that log output somewhere public.
- Pasting your `.env` contents into an issue, PR, or chat when asking for
  help — screenshot/redact instead.

## 5. How dedup works

Seen/sent state now lives in `hermes.db` (SQLite), not `seen.json` — the
first run after this update auto-migrates your existing `seen.json` into
it and leaves the old file in place (harmless, just unused after that).

Two tables: `seen` (any link already evaluated, sent or not — so it's
never re-billed to an LLM call) and `sent` (title/category/score/link for
every article actually sent, which is what powers the weekly digest in
§9). Old entries get pruned automatically past `SEEN_RETENTION_DAYS`
(default 90) — SQLite doesn't have the same unbounded-memory problem the
old in-memory `set` approach did, so this is about tidiness, not
correctness.

**If you pulled this file before late July 2026 patches:** an earlier
version tracked `seen` as a plain Python `set` and trimmed it with
`list(seen)[-CAP:]`, which drops an effectively *random* selection of URLs
once over the cap (`set` iteration order has no relationship to insertion
order) — that could make an old sent article look "new" again and
resurface. Fixed as of the SQLite migration; not something to worry about
going forward.

## 6. Age filtering

Some CERT/advisory feeds return their entire historical archive on every
fetch, not just recent items — this is what caused the "7,893 new
articles" on an early run. Anything older than `MAX_ARTICLE_AGE_HOURS`
(default 72) is now skipped before it ever reaches triage, and marked
seen so it's never rechecked.

## 7. Category-aware criticality + recency tiebreak

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

## 8. Criticality filtering (two-stage, to save tokens)

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

## 9. Known MVP limitations (by design, for v1)

- Uses the feed's own summary/description field by default, not the full
  article body — set `FULL_ARTICLE_MIN_SNIPPET_CHARS` if you want to fetch
  the full page for articles with thin snippets (only applied to articles
  that already cleared triage, so it doesn't add a request per article).
- `MAX_NEW_ARTICLES_PER_RUN` caps how many get *evaluated* per run (not
  how many get sent — that depends on how many clear the score
  threshold), so a large backlog won't blow through your free-tier rate
  limit all at once.
- Feeds are still processed independently (concurrently now, see §9) —
  a feed with a big backlog doesn't dominate a run at the expense of
  others, but there's still no round-robin fairness *within* the
  per-run article cap.
- ESP32 family is intentionally out of scope for this version.

## 10. Multi-provider LLM fallback

`LLM_PROVIDERS` in `.env` is a priority list (e.g. `groq,gemini`), tried
in order until one answers. Groq, Cerebras, OpenRouter, Together, and a
local Ollama/LM Studio server all share one code path (same OpenAI-style
API); Gemini has its own. Once a provider answers successfully in a run,
it's tried first on later calls too, so a known-dead provider isn't
re-attempted every batch. `openai/gpt-oss-*` models (Groq/OpenRouter's
current defaults) are reasoning models — they think before answering, so
`reasoning_effort=low` and generous `max_tokens` budgets are baked in to
avoid the "thought for the whole budget, wrote nothing" empty-response
failure mode.

## 11. Cross-feed duplicate detection

The same story often gets covered by 2-4 of your 44 feeds within the
same window (a CERT advisory plus a few news outlets). Before triage,
headlines are compared pairwise (word-overlap heuristic, not semantic)
and near-duplicates are collapsed to the most recent one — so you don't
spend a triage slot, and then a Telegram message, on the same CVE three
times.

## 12. `--dry-run` and `--weekly-digest`

```bash
python news_bot.py --dry-run        # evaluate + print what would be sent;
                                     # touches neither Telegram nor the DB
python news_bot.py --weekly-digest  # post a 7-day summary to Telegram and exit
```

Point a separate weekly cron/systemd timer at `--weekly-digest` if you
want a standing "what did I actually get sent this week" recap.

## 13. Overlapping runs

A file lock (`.hermes.lock`) means a slow run still in progress when the
next cron/systemd fire happens won't step on the same SQLite database —
the second invocation just exits quietly instead of erroring.

## 14. Self-monitoring

If every configured LLM provider fails for an entire run, or the script
hits an unhandled error, it best-effort pings your Telegram
(`TELEGRAM_CHAT_ID`) with a warning — so an outage shows up immediately
instead of silently not-running for a week.

## 15. Critical-item routing

Set `TELEGRAM_CRITICAL_CHAT_ID` to route the highest scorers (≥
`CRITICAL_SPLIT_SCORE`, default 8) to a separate chat/topic, so a real
0-day doesn't get buried in routine CVE traffic. Leave it unset and
everything goes to `TELEGRAM_CHAT_ID` as before.
