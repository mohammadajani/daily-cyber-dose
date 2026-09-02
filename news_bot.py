#!/usr/bin/env python3
"""
Hermes Lite — RSS -> LLM summary -> Telegram
Runs once per invocation. Trigger it with cron / systemd timer / Termux job scheduler.

Works unmodified on: PC, Linux, Termux (Android), Raspberry Pi Zero W / 2W.

CLI flags:
  --dry-run          Evaluate everything (triage, summarize) and print what
                      would be sent, without touching Telegram or persisting
                      any state (seen links / sent history). Safe to run
                      repeatedly while testing prompt or provider changes.
  --weekly-digest     Post a summary of the last 7 days' sent articles to
                      Telegram and exit, skipping the normal RSS run. Meant
                      to be triggered by a separate weekly cron/systemd timer.

State (seen links + sent-article history) lives in a local SQLite database
(hermes.db by default), auto-migrated from an old seen.json on first run.
"""

import argparse
import calendar
import concurrent.futures
import html
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:
    _HAVE_FCNTL = False  # e.g. Windows — locking becomes a no-op

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
FEEDS_FILE = BASE_DIR / "feeds.txt"
SEEN_FILE = BASE_DIR / "seen.json"          # legacy — read once for migration
DB_FILE = BASE_DIR / os.getenv("DB_FILE", "hermes.db")
LOCK_FILE = BASE_DIR / ".hermes.lock"
RUN_LOG_FILE = BASE_DIR / os.getenv("RUN_LOG_FILE", "run_events.jsonl")

MAX_NEW_ARTICLES_PER_RUN = int(os.getenv("MAX_NEW_ARTICLES_PER_RUN", "15"))

# How many days of "seen" history to keep in the database before pruning.
# SQLite doesn't have the same unbounded-memory problem the old in-memory
# seen.json set did, so this is about tidiness/disk, not correctness —
# generous by default.
SEEN_RETENTION_DAYS = int(os.getenv("SEEN_RETENTION_DAYS", "90"))

# Articles older than this are skipped entirely — never triaged, never
# summarized — and marked seen so they're not re-scanned every run. This is
# what keeps a feed that returns its whole historical archive (some CERT/
# advisory feeds do this) from flooding a run with years-old items.
MAX_ARTICLE_AGE_HOURS = int(os.getenv("MAX_ARTICLE_AGE_HOURS", "72"))

# How many articles go into ONE LLM call. Batching means clearing a big
# backlog costs far fewer requests than one-call-per-article — e.g. 15
# articles at batch size 15 is 1 request instead of 15.
EVAL_BATCH_SIZE = int(os.getenv("EVAL_BATCH_SIZE", "15"))

# Articles are scored 1-10 for criticality by the LLM. Only scores at or
# above this threshold get sent to Telegram. Articles below it are still
# marked "seen" so they aren't re-evaluated (and re-billed) on every run.
CRITICALITY_THRESHOLD = int(os.getenv("CRITICALITY_THRESHOLD", "5"))

# Optional per-category override, e.g. "Newsletters:7,CERT advisory:4" — a
# curated-roundup category and a raw CERT feed don't mean the same thing by
# "critical", so they can carry different bars. Anything not listed here
# falls back to CRITICALITY_THRESHOLD above.
def _parse_category_thresholds(raw: str) -> dict:
    thresholds = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        name, _, value = part.rpartition(":")
        try:
            thresholds[name.strip()] = int(value.strip())
        except ValueError:
            print(f"[!] Ignoring malformed CATEGORY_THRESHOLDS entry: '{part}'")
    return thresholds

CATEGORY_THRESHOLDS = _parse_category_thresholds(os.getenv("CATEGORY_THRESHOLDS", ""))


def threshold_for_category(category: str) -> int:
    return CATEGORY_THRESHOLDS.get(category, CRITICALITY_THRESHOLD)


# How many feeds to fetch in parallel, and how long to wait per feed. Feed
# fetching is I/O-bound (waiting on remote servers), so a thread pool here
# is a straightforward win — a couple of slow/dead feeds no longer stall
# the other 40+.
FEED_FETCH_WORKERS = int(os.getenv("FEED_FETCH_WORKERS", "10"))
FEED_FETCH_TIMEOUT = int(os.getenv("FEED_FETCH_TIMEOUT", "15"))

# If an article's RSS snippet is shorter than this many characters, fetch
# the full article page and summarize from that instead — some feeds give
# almost nothing to work with. 0 disables this (default: off, since it adds
# an extra HTTP request per thin article). Only applied to articles that
# already cleared triage, not the whole batch.
FULL_ARTICLE_MIN_SNIPPET_CHARS = int(os.getenv("FULL_ARTICLE_MIN_SNIPPET_CHARS", "0"))
FULL_ARTICLE_MAX_CHARS = int(os.getenv("FULL_ARTICLE_MAX_CHARS", "4000"))

# Optional second Telegram destination for the most critical items (e.g. a
# pinned/separate topic or chat), so a real 0-day doesn't get buried in a
# scroll of routine CVEs. Leave TELEGRAM_CRITICAL_CHAT_ID unset to disable —
# everything just goes to TELEGRAM_CHAT_ID as before.
TELEGRAM_CRITICAL_CHAT_ID = os.getenv("TELEGRAM_CRITICAL_CHAT_ID", "")
CRITICAL_SPLIT_SCORE = int(os.getenv("CRITICAL_SPLIT_SCORE", "8"))

# Providers are tried IN ORDER, left to right, until one succeeds. Set via
# LLM_PROVIDERS in .env, comma separated, e.g. "groq,cerebras,gemini".
# (LLM_PROVIDER, singular, still works as a one-provider fallback for old
# .env files.) This is what makes a provider retiring a model a non-event:
# reorder or extend this list and nothing else in the file has to change.
LLM_PROVIDERS = [
    p.strip().lower()
    for p in os.getenv("LLM_PROVIDERS", os.getenv("LLM_PROVIDER", "groq")).split(",")
    if p.strip()
]

# Groq, Cerebras, OpenRouter, Together, and a local Ollama/LM Studio server
# all speak the same OpenAI-style chat-completions request/response shape —
# only base_url / api_key / model differ — so one function (see
# _chat_openai_compatible below) drives all of them off this table. Add a
# new one by adding a row here; no new function needed.
#
# Groq deprecated llama-3.3-70b-versatile in June 2026 — openai/gpt-oss-120b
# is their recommended replacement (faster, similar quality, still free-tier).
OPENAI_COMPATIBLE_PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1/chat/completions",
        "api_key": os.getenv("GROQ_API_KEY", ""),
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        # NOT the literal value sent — see _reasoning_effort_for_model()
        # below. This is just "how hard should reasoning models think",
        # translated per-model into whatever vocabulary that specific
        # model family actually accepts on Groq.
        "reasoning_intent": os.getenv("GROQ_REASONING_EFFORT", "low"),
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1/chat/completions",
        "api_key": os.getenv("CEREBRAS_API_KEY", ""),
        "model": os.getenv("CEREBRAS_MODEL", "llama-3.3-70b"),
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free"),
        "reasoning_intent": os.getenv("OPENROUTER_REASONING_EFFORT", "low"),
    },
    "together": {
        "base_url": "https://api.together.xyz/v1/chat/completions",
        "api_key": os.getenv("TOGETHER_API_KEY", ""),
        "model": os.getenv("TOGETHER_MODEL", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"),
    },
    # Point this at a local Ollama (http://localhost:11434/v1/chat/completions)
    # or LM Studio server if you want a fully offline fallback that never gets
    # deprecated out from under you. No API key needed for most local servers.
    "local": {
        "base_url": os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1/chat/completions"),
        "api_key": os.getenv("LOCAL_LLM_API_KEY", "not-needed"),
        "model": os.getenv("LOCAL_LLM_MODEL", "llama3.1"),
    },
}

# Gemini uses a different request/response shape, so it keeps its own
# function (_chat_gemini below) rather than joining the table above.
# Google shut down gemini-2.0-flash on June 1 2026 — gemini-3.1-flash-lite
# is the current recommended free-tier target (generous RPM, GA as of 2026).
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

REQUEST_TIMEOUT = 20  # seconds, for every outbound HTTP call


# ---------------------------------------------------------------------------
# Structured event log (JSON lines) — separate from the human-readable
# print() statements scattered through this file, which stay as-is. This is
# for the events worth grepping/parsing later (run summaries, provider
# selection, failures) without scraping stdout.
# ---------------------------------------------------------------------------

def log_event(event: str, **fields) -> None:
    try:
        with RUN_LOG_FILE.open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), "event": event, **fields}) + "\n")
    except OSError:
        pass  # structured logging is a nice-to-have, never fatal


# ---------------------------------------------------------------------------
# Run lock — prevents two overlapping runs (e.g. a slow run still going when
# the next cron/systemd fire happens) from writing to the database at the
# same time.
# ---------------------------------------------------------------------------

_lock_fh = None


def acquire_run_lock() -> bool:
    """Returns False if another run already holds the lock — the caller
    should exit quietly (not an error, just "already running")."""
    global _lock_fh
    if not _HAVE_FCNTL:
        return True  # best-effort only on platforms without fcntl
    _lock_fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


# ---------------------------------------------------------------------------
# Persistence — SQLite-backed store for seen links and sent-article history.
# Replaces the old flat seen.json (same job: don't re-process a link we've
# already handled) and adds a `sent` table, which is what makes a weekly
# digest possible without re-parsing logs. Auto-migrates from seen.json the
# first time it runs against a fresh database.
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (link TEXT PRIMARY KEY, seen_epoch INTEGER NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sent ("
            "link TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT, "
            "score INTEGER, sent_epoch INTEGER NOT NULL)"
        )
        self._conn.commit()
        self._migrate_from_json()

    def _migrate_from_json(self) -> None:
        if not SEEN_FILE.exists():
            return
        if self._conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0] > 0:
            return  # already has data — don't re-import over it
        try:
            links = json.loads(SEEN_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return
        now = int(time.time())
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen (link, seen_epoch) VALUES (?, ?)",
            [(link, now) for link in links],
        )
        self._conn.commit()
        print(f"[i] Migrated {len(links)} link(s) from seen.json into {DB_FILE.name}.")

    def __contains__(self, link: str) -> bool:
        return self._conn.execute("SELECT 1 FROM seen WHERE link = ?", (link,)).fetchone() is not None

    def add(self, link: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen (link, seen_epoch) VALUES (?, ?)", (link, int(time.time()))
        )

    def record_sent(self, link: str, title: str, category: str, score: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sent (link, title, category, score, sent_epoch) VALUES (?, ?, ?, ?, ?)",
            (link, title, category, score, int(time.time())),
        )

    def prune_old(self, days: int) -> int:
        cutoff = int(time.time()) - days * 86400
        return self._conn.execute("DELETE FROM seen WHERE seen_epoch < ?", (cutoff,)).rowcount

    def weekly_sent(self, days: int = 7) -> list:
        cutoff = int(time.time()) - days * 86400
        return self._conn.execute(
            "SELECT title, category, score, link FROM sent WHERE sent_epoch >= ? "
            "ORDER BY score DESC, sent_epoch DESC",
            (cutoff,),
        ).fetchall()

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

def load_feed_urls() -> list:
    """Returns a list of (url, category) tuples. Category comes from the
    most recent '# --- Section Name ---' header above each URL in feeds.txt,
    so feeds.txt doubles as both the feed list AND the category config —
    no second file to keep in sync."""
    if not FEEDS_FILE.exists():
        print(f"[!] {FEEDS_FILE} not found. Create it with one RSS URL per line.")
        sys.exit(1)

    lines = FEEDS_FILE.read_text().splitlines()
    feeds = []
    current_category = "general"

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# ---"):
            # e.g. "# --- Threat intelligence (as you asked for specifically) ---"
            label = line.strip("# -").strip()
            label = label.split("(")[0].strip()  # drop parenthetical asides
            if label:
                current_category = label
            continue
        if line.startswith("#"):
            continue  # a plain comment, e.g. the excluded-feeds notes at the bottom
        feeds.append((line, current_category))

    return feeds


def _entry_age_hours(entry) -> float:
    """Returns age in hours, or None if the feed doesn't provide a date."""
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    published_epoch = calendar.timegm(struct)
    return (time.time() - published_epoch) / 3600.0


def _strip_html(text: str) -> str:
    """Some feeds put full HTML (tags, embedded images, sometimes an
    entire newsletter body) in <description> instead of a plain-text
    snippet. Stored unstripped, that HTML both wastes tokens on markup the
    LLM doesn't need and can make a single article's content balloon to
    tens of KB — see MAX_SNIPPET_CHARS_FOR_SUMMARY below for why that
    matters."""
    if not text:
        return text
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _fetch_one_feed(url_category: tuple) -> list:
    """Worker for the thread pool below. Returns raw entry dicts — NOT yet
    filtered by seen/age/dedup, since that all happens serially against the
    (single-threaded) SQLite store back in the main thread."""
    url, category = url_category
    try:
        resp = requests.get(
            url, timeout=FEED_FETCH_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HermesLite/1.0)"},
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        print(f"[!] Failed to fetch/parse feed {url}: {e}")
        return []

    entries = []
    for entry in parsed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        entries.append({
            "title": entry.get("title", "Untitled"),
            "link": link,
            # feed summary/description is usually short — good enough
            # context for an LLM summary without fetching the full page
            # (unless FULL_ARTICLE_MIN_SNIPPET_CHARS says otherwise later).
            # Stripped of HTML here; hard-capped in build_summarize_prompt.
            "raw_summary": _strip_html(entry.get("summary", "") or entry.get("description", "")),
            "published": entry.get("published", ""),
            "published_epoch": calendar.timegm(entry["published_parsed"]) if entry.get("published_parsed") else 0,
            "age_hours": _entry_age_hours(entry),
            "category": category,
        })
    return entries


def fetch_all_feeds(feed_urls: list) -> list:
    """Fetches every feed concurrently — feed fetching is I/O-bound, so a
    thread pool cuts wall-clock time a lot once you're past a handful of
    feeds, and one slow/dead feed no longer stalls all the others."""
    all_entries = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
        for entries in pool.map(_fetch_one_feed, feed_urls):
            all_entries.extend(entries)
    return all_entries


# ---------------------------------------------------------------------------
# Cross-feed near-duplicate detection — the same CVE or breach often gets
# covered by several feeds (a CERT advisory + 2-3 news outlets) within the
# same MAX_ARTICLE_AGE_HOURS window. This keeps triage from spending a slot
# (and Telegram from spending a message) on the same story 3 times.
# ---------------------------------------------------------------------------

def _significant_words(title: str) -> set:
    normalized = re.sub(r"[^a-z0-9 ]", " ", title.lower())
    return {w for w in normalized.split() if len(w) >= 4}


def dedupe_near_duplicate_titles(entries: list, threshold: float = 0.5) -> tuple:
    """Returns (kept, duplicates). Processes newest-first so the most
    recent article covering a story is the one that's kept. Similarity is
    the overlap coefficient (shared words / smaller title's word count) on
    significant (4+ char) words — not Jaccard, because two outlets covering
    the same story rarely use the same NUMBER of words (one adds "hackers",
    "linked", etc.), which tanks a Jaccard score even when the core content
    fully overlaps. A heuristic, not a semantic match, but cheap and catches
    the common case."""
    kept, kept_word_sets, duplicates = [], [], []
    for entry in sorted(entries, key=lambda e: e["published_epoch"], reverse=True):
        words = _significant_words(entry["title"])
        is_dup = False
        for kw in kept_word_sets:
            if words and kw and len(words & kw) / min(len(words), len(kw)) >= threshold:
                is_dup = True
                break
        if is_dup:
            duplicates.append(entry)
        else:
            kept.append(entry)
            kept_word_sets.append(words)
    return kept, duplicates


# ---------------------------------------------------------------------------
# Full-article fetch — only used for entries that already cleared triage
# and have a thin RSS snippet, so it doesn't add an HTTP request per
# article regardless of whether it ends up getting summarized at all.
# ---------------------------------------------------------------------------

def fetch_full_article_text(url: str, max_chars: int) -> str:
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HermesLite/1.0)"},
        )
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        print(f"[!] Couldn't fetch full article text for {url}: {e}")
        return ""


# ---------------------------------------------------------------------------
# LLM provider layer — one call site (`call_llm`) that tries every provider
# in LLM_PROVIDERS in order and falls through to the next on any failure
# (missing key, deprecated/decommissioned model, rate limit, outage, ...).
# Triage and summarize just build a prompt and call this; they don't know or
# care which provider actually answered.
#
# Once a provider answers successfully, it's remembered as "sticky" and
# tried first on the next call this run — so if Groq is having a bad day,
# batch 2 doesn't waste a request re-discovering that before falling to
# Gemini again; it goes straight there.
# ---------------------------------------------------------------------------

class ProviderUnavailable(Exception):
    """Raised for a provider that isn't configured at all (no API key) —
    skipped silently rather than logged as a failure."""


_sticky_provider = None


def _reasoning_effort_for_model(model: str, intent: str):
    """Reasoning-capable models on Groq/OpenRouter don't share one
    reasoning_effort vocabulary — send the wrong word for the model family
    and the WHOLE request 400s (it's not silently ignored):
      - openai/gpt-oss-* accepts: low, medium, high  (default: medium)
      - qwen3 / qwen3.6 accepts:  none, default       (default: default)
      - everything else (llama-3.x, kimi, mixtral, ...) doesn't do hidden
        reasoning at all and 400s on ANY reasoning_effort value, including
        "none" — the only safe move is to omit the parameter entirely.
    This is exactly what broke when GROQ_MODEL was switched from
    gpt-oss-120b to qwen/qwen3.6-27b: the old code always sent "low",
    which is valid for gpt-oss but invalid for Qwen. Detecting the family
    from the model name (rather than hardcoding one family's vocabulary
    per provider) is what makes this survive a future model swap without
    a code change — though a brand-new model family Groq adds later, using
    a vocabulary this function has never seen, will just fall through to
    "omit the parameter" (safe — no 400, you just lose the effort tuning
    for that specific model until this list is updated)."""
    model_l = model.lower()
    if "gpt-oss" in model_l:
        return intent if intent in ("low", "medium", "high") else "low"
    if "qwen3" in model_l.replace("qwen/qwen", "qwen").replace(".", ""):
        return "none" if intent in ("low", "none") else "default"
    return None  # unrecognized family — omit rather than guess and 400


def _chat_openai_compatible(provider: str, prompt: str, max_tokens: int, temperature: float) -> str:
    cfg = OPENAI_COMPATIBLE_PROVIDERS[provider]
    if not cfg["api_key"]:
        raise ProviderUnavailable(f"no API key configured for '{provider}'")
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    effort = _reasoning_effort_for_model(cfg["model"], cfg.get("reasoning_intent", "low"))
    if effort:
        payload["reasoning_effort"] = effort
    resp = requests.post(
        cfg["base_url"],
        headers={"Authorization": f"Bearer {cfg['api_key']}"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]
    content = message.get("content") or ""
    if not content.strip():
        # Reasoning models (gpt-oss, etc.) can spend the entire max_tokens
        # budget on hidden chain-of-thought and return empty content even
        # on a 200 OK. Log it plainly (ProviderUnavailable is normally a
        # silent "not configured" skip, but this case is worth seeing) and
        # let the caller fall through to the next provider.
        print(f"[!] '{provider}' ({cfg['model']}) returned empty content — likely spent all "
              f"{max_tokens} max_tokens on reasoning before writing an answer.")
        raise ProviderUnavailable("empty content from reasoning overrun")
    return content


def _chat_gemini(prompt: str, max_tokens: int, temperature: float) -> str:
    if not GEMINI_API_KEY:
        raise ProviderUnavailable("no API key configured for 'gemini'")
    # The key goes in the x-goog-api-key HEADER, not a ?key= query param.
    # Both are accepted by Google's API, but a key in the URL ends up
    # embedded in requests.HTTPError's string representation on any
    # failure (e.g. "400 Client Error: ... for url: ...?key=AIza...") —
    # which then gets printed straight to our own logs. GitHub Actions
    # masks known secrets in its own log UI, but this file also writes to
    # a local news_bot.log on a cron/systemd box, where nothing masks it.
    # The header form is documented as fully equivalent and simply never
    # puts the key somewhere it can leak into log text.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    resp = requests.post(
        url,
        headers={"x-goog-api-key": GEMINI_API_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


def configured_providers() -> list:
    """LLM_PROVIDERS entries that actually have credentials set — used by
    validate_config() to fail fast at startup instead of mid-run."""
    live = []
    for p in LLM_PROVIDERS:
        if p == "gemini" and GEMINI_API_KEY:
            live.append(p)
        elif p in OPENAI_COMPATIBLE_PROVIDERS and OPENAI_COMPATIBLE_PROVIDERS[p]["api_key"]:
            live.append(p)
    return live


def call_llm(prompt: str, max_tokens: int, temperature: float) -> tuple:
    """Tries each provider, sticky-provider first, until one returns
    successfully. Returns (raw_response_text, provider_name_that_answered).

    If every provider fails, raises the most INFORMATIVE error rather than
    simply the last one tried: a real HTTP failure (bad request, decommissioned
    model, outage) is far more useful for debugging than "provider X has no
    API key configured", which just means X wasn't set up — not that
    anything actually went wrong with it. Without this, a genuine failure on
    provider 1 gets silently overwritten in the logs by an unconfigured
    provider 2 or 3 later in the list."""
    global _sticky_provider
    order = LLM_PROVIDERS
    if _sticky_provider and _sticky_provider in LLM_PROVIDERS:
        order = [_sticky_provider] + [p for p in LLM_PROVIDERS if p != _sticky_provider]

    last_err = None
    first_real_err = None  # first HTTP/connection failure — NOT a config skip
    for provider in order:
        try:
            if provider == "gemini":
                raw = _chat_gemini(prompt, max_tokens, temperature)
            elif provider in OPENAI_COMPATIBLE_PROVIDERS:
                raw = _chat_openai_compatible(provider, prompt, max_tokens, temperature)
            else:
                print(f"[!] '{provider}' in LLM_PROVIDERS isn't a known provider — skipping.")
                continue
            _sticky_provider = provider
            return raw, provider
        except ProviderUnavailable as e:
            if provider == _sticky_provider:
                _sticky_provider = None
            last_err = e
            continue  # not configured / empty content — try the next one
        except requests.HTTPError as e:
            print(f"[!] Provider '{provider}' failed ({e}) — trying next provider...")
            if provider == _sticky_provider:
                _sticky_provider = None
            last_err = e
            if first_real_err is None:
                first_real_err = e
            continue
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"[!] Provider '{provider}' unreachable ({e}) — trying next provider...")
            if provider == _sticky_provider:
                _sticky_provider = None
            last_err = e
            if first_real_err is None:
                first_real_err = e
            continue
    raise first_real_err or last_err or RuntimeError(
        "No LLM provider configured. Set at least one API key in .env for a "
        f"provider listed in LLM_PROVIDERS ({', '.join(LLM_PROVIDERS)})."
    )


# ---------------------------------------------------------------------------
# Stage 1: TRIAGE — headlines only, scores only. Cheap: no article content
# sent, no summaries generated for articles that end up filtered out.
# ---------------------------------------------------------------------------

TRIAGE_PROMPT_HEADER = (
    "You are triaging cybersecurity items for a security researcher, by how "
    "critical/actionable each one is. Below are {n} items, numbered, each "
    "tagged with its source category in brackets. Respond with ONLY a JSON "
    "array of {n} integers (1-10), no objects, no markdown fences, no extra "
    "text — just the raw array, e.g. [7, 2, 9], in the SAME ORDER as the "
    "items.\n\n"
    "Score with the category in mind, since 'critical' means different "
    "things for different sources:\n"
    "- [CERT advisory] / [Vendor advisories]: score by the severity of the "
    "vulnerability disclosed (actively exploited or widely-deployed = high).\n"
    "- [Threat intelligence] / [Vulnerability research] / [News & research]: "
    "score by real-world impact and how actionable it is right now.\n"
    "- [Newsletters]: these are curated roundups, not urgent by nature — "
    "score by how significant the notable items *within* the roundup are, "
    "not the newsletter format itself.\n\n"
    "General scale: 10 = actively-exploited vulnerability or major breach "
    "affecting widely-used systems, requiring immediate attention. "
    "5 = notable but not urgent. 1 = minor/opinion/non-actionable.\n\n"
)


def build_triage_prompt(articles: list) -> str:
    parts = [TRIAGE_PROMPT_HEADER.format(n=len(articles))]
    for i, art in enumerate(articles, 1):
        parts.append(f"{i}. [{art['category']}] {art['title']}")
    return "\n".join(parts)


def _sanitize_json_escapes(text: str) -> str:
    """LLMs frequently emit raw backslashes in generated text (Windows paths,
    regex fragments, etc.) without escaping them for JSON — \\W, \\S, \\d are
    not valid JSON escapes and make json.loads reject the whole response.
    This doubles any backslash that isn't already part of a valid escape
    sequence, so 'C:\\Windows' becomes 'C:\\\\Windows' and parses cleanly."""
    return re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', text)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _load_json_array(text: str):
    """Try a normal parse first; only pay the sanitization cost if that fails."""
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_sanitize_json_escapes(text))


def _parse_int_array(raw_text: str, expected_count: int) -> list:
    data = _load_json_array(raw_text)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of integers from the LLM")
    scores = [int(x) for x in data]
    if len(scores) != expected_count:
        print(f"[!] Warning: triage expected {expected_count} scores, got {len(scores)} "
              f"— matching by position, extras/missing will be dropped or skipped")
    return scores


def triage_batch(articles: list) -> list:
    """Returns a list of ints (scores), same order as `articles`. Headlines only."""
    # The JSON array itself is tiny (a few tokens per article), but reasoning
    # models need real headroom beyond that for hidden chain-of-thought — a
    # budget sized only for the answer starves the reasoning and comes back
    # empty (see the reasoning_effort / empty-content handling above).
    max_tokens = min(2048, 60 * len(articles) + 500)
    raw, provider = call_llm(build_triage_prompt(articles), max_tokens, temperature=0.2)
    print(f"[i] Triage answered by: {provider}")
    log_event("triage", provider=provider, batch_size=len(articles))
    return _parse_int_array(raw, len(articles))


# ---------------------------------------------------------------------------
# Stage 2: SUMMARIZE — only called for articles that cleared the threshold.
# Full title + content sent, one summary string back per article.
# ---------------------------------------------------------------------------

# A hard ceiling on how much of one article's content goes into the
# summarize prompt, regardless of what a feed provides. This is what
# actually fixed the "413 Payload Too Large" failures: some feeds (mostly
# newsletter/roundup-style ones) put their entire body — sometimes tens of
# KB — into <description>, and with EVAL_BATCH_SIZE articles combined into
# one request, a single bloated feed entry was enough to blow past Groq's
# request-size limit. 2000 chars is generous for what's meant to be a
# snippet-level summary input; FULL_ARTICLE_MIN_SNIPPET_CHARS content
# (opted-in, see fetch_full_article_text) gets capped the same way.
MAX_SNIPPET_CHARS_FOR_SUMMARY = int(os.getenv("MAX_SNIPPET_CHARS_FOR_SUMMARY", "2000"))

SUMMARIZE_PROMPT_HEADER = (
    "Summarize each of these {n} cybersecurity news articles in 2-3 concise, "
    "factual, neutral sentences for a busy reader — no fluff, no restating "
    "the headline verbatim. Respond with ONLY a JSON array of {n} strings, "
    "no markdown fences, no extra text, in the SAME ORDER as the articles.\n\n"
)


def build_summarize_prompt(articles: list) -> str:
    parts = [SUMMARIZE_PROMPT_HEADER.format(n=len(articles))]
    for i, art in enumerate(articles, 1):
        content = art["raw_summary"] or "(no snippet available, summarize based on title only)"
        if len(content) > MAX_SNIPPET_CHARS_FOR_SUMMARY:
            content = content[:MAX_SNIPPET_CHARS_FOR_SUMMARY] + " [...truncated]"
        parts.append(f"{i}. Title: {art['title']}\n   Content: {content}\n")
    return "\n".join(parts)


def _parse_str_array(raw_text: str, expected_count: int) -> list:
    data = _load_json_array(raw_text)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of strings from the LLM")
    summaries = [str(x).strip() for x in data]
    if len(summaries) != expected_count:
        print(f"[!] Warning: summarize expected {expected_count} summaries, got {len(summaries)} "
              f"— matching by position, extras/missing will be dropped or skipped")
    return summaries


def summarize_batch(articles: list) -> list:
    """Returns a list of summary strings, same order as `articles`."""
    max_tokens = min(8192, 150 * len(articles) + 1200)
    raw, provider = call_llm(build_summarize_prompt(articles), max_tokens, temperature=0.3)
    print(f"[i] Summarize answered by: {provider}")
    log_event("summarize", provider=provider, batch_size=len(articles))
    return _parse_str_array(raw, len(articles))


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def _post_telegram_message(text: str, chat_id: str, parse_mode: str = "HTML") -> bool:
    """Low-level send with two recovery paths baked in:
    - 429 flood control: honor Telegram's own retry_after and try again once.
    - 400 'can't parse entities': a stray character in LLM-generated text
      broke HTML entity parsing — Telegram rejects the WHOLE message for
      this, not just the bad character. Retry once as plain text rather
      than losing the message entirely."""
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    resp = None
    for _ in range(3):  # normal attempt + up to two recovery attempts
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            return True

        if resp.status_code == 429:
            retry_after = resp.json().get("parameters", {}).get("retry_after", 3)
            print(f"[!] Telegram flood control — waiting {retry_after}s before retry.")
            time.sleep(retry_after + 1)
            continue

        if resp.status_code == 400 and "parse entities" in resp.text and payload.get("parse_mode"):
            print("[!] Telegram rejected formatted text (entity parse error) — retrying as plain text.")
            payload.pop("parse_mode", None)
            continue

        break  # some other error — no point retrying the same payload

    print(f"[!] Telegram send failed ({resp.status_code if resp is not None else '?'}): "
          f"{resp.text if resp is not None else 'no response'}")
    return False


def send_telegram(title: str, summary: str, link: str, score: int = None) -> bool:
    score_line = f"\U0001F4CA Criticality: {score}/10\n" if score is not None else ""
    text = f"<b>{html.escape(title)}</b>\n{score_line}\n{html.escape(summary)}\n\n{link}"
    chat_id = TELEGRAM_CHAT_ID
    if score is not None and score >= CRITICAL_SPLIT_SCORE and TELEGRAM_CRITICAL_CHAT_ID:
        chat_id = TELEGRAM_CRITICAL_CHAT_ID
    return _post_telegram_message(text, chat_id)


def send_alert(message: str) -> None:
    """Best-effort self-monitoring ping for when the run can't proceed at
    all (no provider reachable, uncaught exception). Never raises — a
    broken alert path shouldn't mask the original problem."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        _post_telegram_message(f"\u26A0\uFE0F <b>Hermes Lite</b>\n{html.escape(message)}", TELEGRAM_CHAT_ID)
    except Exception:
        pass


def send_weekly_digest(store: Store) -> None:
    rows = store.weekly_sent(7)
    if not rows:
        _post_telegram_message(
            "\U0001F4C8 <b>Weekly Hermes Lite digest</b>\nNo articles cleared the criticality bar this week.",
            TELEGRAM_CHAT_ID,
        )
        print("[i] Weekly digest sent (nothing to report).")
        return
    lines = [
        "\U0001F4C8 <b>Weekly Hermes Lite digest</b>",
        f"{len(rows)} article(s) sent this week.\n",
        "<b>Top stories:</b>",
    ]
    for title, category, score, link in rows[:3]:
        lines.append(f"\u2022 {html.escape(title)} ({score}/10)\n{link}")
    _post_telegram_message("\n".join(lines), TELEGRAM_CHAT_ID)
    print(f"[i] Weekly digest sent — {len(rows)} article(s), top score {rows[0][2]}.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Hermes Lite — RSS -> LLM -> Telegram")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Evaluate and print what would be sent, without touching Telegram or the database.",
    )
    parser.add_argument(
        "--weekly-digest", action="store_true",
        help="Post a digest of the last 7 days' sent articles to Telegram, then exit.",
    )
    return parser.parse_args()


def validate_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print(f"[!] Missing required .env values: {', '.join(missing)}")
        sys.exit(1)

    live = configured_providers()
    if not live:
        print(
            f"[!] LLM_PROVIDERS is set to '{', '.join(LLM_PROVIDERS)}' but none of them "
            "have an API key configured in .env. Set at least one (e.g. GROQ_API_KEY)."
        )
        sys.exit(1)
    print(f"[i] LLM providers available this run, in priority order: {', '.join(live)}")


def main():
    args = parse_args()

    if not acquire_run_lock():
        print("[i] Another run is already in progress (lock held) — exiting.")
        return

    validate_config()
    store = Store(DB_FILE)
    pruned = store.prune_old(SEEN_RETENTION_DAYS)
    if pruned:
        print(f"[i] Pruned {pruned} seen-link record(s) older than {SEEN_RETENTION_DAYS} days.")

    if args.weekly_digest:
        send_weekly_digest(store)
        store.commit()
        store.close()
        return

    feed_urls = load_feed_urls()
    raw_entries = fetch_all_feeds(feed_urls)
    candidates = [e for e in raw_entries if e["link"] not in store]

    deduped, duplicates = dedupe_near_duplicate_titles(candidates)
    if duplicates:
        print(f"[i] Skipped {len(duplicates)} near-duplicate item(s) covering the same story across feeds.")
        for dup in duplicates:
            store.add(dup["link"])

    new_entries = []
    skipped_stale = 0
    for entry in deduped:
        age_hours = entry["age_hours"]
        if age_hours is not None and age_hours > MAX_ARTICLE_AGE_HOURS:
            store.add(entry["link"])
            skipped_stale += 1
            continue
        new_entries.append(entry)
    if skipped_stale:
        print(f"[i] Skipped {skipped_stale} item(s) older than {MAX_ARTICLE_AGE_HOURS}h (marked seen, won't recheck).")

    print(f"[i] Found {len(new_entries)} new article(s) across {len(feed_urls)} feed(s) "
          f"(within the last {MAX_ARTICLE_AGE_HOURS}h).")

    if not new_entries:
        if args.dry_run:
            store.rollback()
        else:
            store.commit()
        store.close()
        return

    capped_entries = new_entries[:MAX_NEW_ARTICLES_PER_RUN]
    if len(new_entries) > MAX_NEW_ARTICLES_PER_RUN:
        print(f"[i] Capping this run to {MAX_NEW_ARTICLES_PER_RUN} of {len(new_entries)} "
              f"found (rest will be picked up on future runs).")

    evaluated = 0
    sent = 0
    filtered_out = 0
    triage_batches_failed = 0
    to_summarize = []  # list of (entry, score) that cleared the threshold

    # --- Stage 1: triage on headlines only ---
    for batch in chunked(capped_entries, EVAL_BATCH_SIZE):
        try:
            scores = triage_batch(batch)
        except requests.HTTPError as e:
            print(f"[!] Triage failed ({len(batch)} headlines): {e}")
            triage_batches_failed += 1
            continue  # don't mark seen — retry this whole batch next run
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[!] Couldn't parse triage response: {e}")
            triage_batches_failed += 1
            continue
        except (ProviderUnavailable, RuntimeError) as e:
            print(f"[!] All LLM providers failed for this batch: {e}")
            triage_batches_failed += 1
            continue
        except Exception as e:
            print(f"[!] Unexpected error during triage: {e}")
            triage_batches_failed += 1
            continue

        for entry, score in zip(batch, scores):
            evaluated += 1
            threshold = threshold_for_category(entry["category"])
            if score < threshold:
                print(f"[-] Filtered out (score {score}/10, threshold {threshold}): {entry['title']}")
                store.add(entry["link"])  # triaged once, never re-spend quota on it
                filtered_out += 1
            else:
                to_summarize.append((entry, score))

    if triage_batches_failed and evaluated == 0:
        send_alert(
            f"Every triage batch failed this run ({triage_batches_failed} batch(es)) — "
            "check provider status and logs."
        )

    # sort so the most critical articles get sent first; among equal scores,
    # the more recent article wins (a 1-hour-old 8/10 beats a 48-hour-old 8/10)
    to_summarize.sort(key=lambda pair: (pair[1], pair[0]["published_epoch"]), reverse=True)

    # --- Stage 2: summarize only the articles that cleared the bar ---
    for batch in chunked(to_summarize, EVAL_BATCH_SIZE):
        entries = [pair[0] for pair in batch]
        scores_by_entry = {pair[0]["link"]: pair[1] for pair in batch}

        if FULL_ARTICLE_MIN_SNIPPET_CHARS > 0:
            for entry in entries:
                if len(entry["raw_summary"]) < FULL_ARTICLE_MIN_SNIPPET_CHARS:
                    full_text = fetch_full_article_text(entry["link"], FULL_ARTICLE_MAX_CHARS)
                    if full_text:
                        entry["raw_summary"] = full_text

        try:
            summaries = summarize_batch(entries)
        except requests.HTTPError as e:
            print(f"[!] Summarize failed ({len(entries)} articles): {e}")
            continue  # don't mark seen — retry next run
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[!] Couldn't parse summarize response: {e}")
            continue
        except (ProviderUnavailable, RuntimeError) as e:
            print(f"[!] All LLM providers failed for this batch: {e}")
            continue
        except Exception as e:
            print(f"[!] Unexpected error during summarize: {e}")
            continue

        for entry, summary in zip(entries, summaries):
            title, link, category = entry["title"], entry["link"], entry["category"]
            score = scores_by_entry[link]

            if args.dry_run:
                print(f"[dry-run] Would send (score {score}/10): {title}")
                sent += 1
                continue

            if send_telegram(title, summary, link, score=score):
                print(f"[+] Sent (score {score}/10): {title}")
                store.add(link)
                store.record_sent(link, title, category, score)
                sent += 1
            else:
                print(f"[!] Telegram send failed, will retry next run: {title}")
            time.sleep(1)  # light pacing between Telegram sends only

    if args.dry_run:
        store.rollback()
    else:
        store.commit()
    store.close()

    print(f"[i] Done. Triaged {evaluated}, sent {sent}, filtered out {filtered_out}.")
    log_event("run_complete", evaluated=evaluated, sent=sent, filtered_out=filtered_out, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise  # validate_config()'s deliberate exits — not a crash
    except Exception as e:
        print(f"[!] Fatal error: {e}")
        send_alert(f"Fatal error, run aborted: {e}")
        sys.exit(1)
