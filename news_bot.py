#!/usr/bin/env python3
"""
Hermes Lite — RSS -> LLM summary -> Telegram
Runs once per invocation. Trigger it with cron / systemd timer / Termux job scheduler.

Works unmodified on: PC, Linux, Termux (Android), Raspberry Pi Zero W / 2W.
"""

import calendar
import json
import os
import re
import sys
import time
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
FEEDS_FILE = BASE_DIR / "feeds.txt"
SEEN_FILE = BASE_DIR / "seen.json"

MAX_NEW_ARTICLES_PER_RUN = int(os.getenv("MAX_NEW_ARTICLES_PER_RUN", "15"))

# How many URLs to remember before trimming the oldest. Bumped up from the
# original 2000 default — a feed list this size (44 feeds, several of them
# high-volume CVE streams like MSRC) blows past 2000 unique URLs quickly,
# which was triggering the trimming bug fixed below far sooner than intended.
SEEN_HISTORY_CAP = int(os.getenv("SEEN_HISTORY_CAP", "8000"))

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

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()  # "groq" or "gemini"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

REQUEST_TIMEOUT = 20  # seconds, for every outbound HTTP call


# ---------------------------------------------------------------------------
# Seen-articles store (simple JSON file — swap for SQLite later if needed)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Seen-articles store (swap for SQLite later if needed)
#
# IMPORTANT: this used to be a plain Python `set`, trimmed with
# `list(seen)[-CAP:]`. That looks like "keep the most recent CAP entries" but
# isn't — set iteration order in Python has NO relationship to insertion
# order, so that trim was dropping an effectively random selection of URLs
# once the set exceeded SEEN_HISTORY_CAP. A dropped URL that was still
# within MAX_ARTICLE_AGE_HOURS would then look "new" again on a later run —
# which is exactly what re-sent an already-sent article. This class tracks
# real insertion order (a list) alongside a set for O(1) membership checks,
# so trimming now genuinely drops the oldest entries first.
# ---------------------------------------------------------------------------

class SeenStore:
    def __init__(self, initial_order: list):
        self._order = list(initial_order)   # oldest-first; source of truth
        self._set = set(self._order)         # fast membership checks only

    def __contains__(self, link: str) -> bool:
        return link in self._set

    def add(self, link: str) -> None:
        if link not in self._set:
            self._set.add(link)
            self._order.append(link)  # newest entries always land at the end

    def trimmed_for_save(self, cap: int) -> list:
        return self._order[-cap:] if len(self._order) > cap else self._order


def load_seen() -> SeenStore:
    if not SEEN_FILE.exists():
        return SeenStore([])
    try:
        return SeenStore(json.loads(SEEN_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return SeenStore([])


def save_seen(seen: SeenStore) -> None:
    SEEN_FILE.write_text(json.dumps(seen.trimmed_for_save(SEEN_HISTORY_CAP)))


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


def fetch_new_entries(feed_urls: list, seen: SeenStore) -> list:
    """feed_urls is a list of (url, category) tuples.
    Returns a flat list of dicts: {title, link, raw_summary, published, category}.
    Entries older than MAX_ARTICLE_AGE_HOURS are marked seen and skipped here,
    before they ever reach triage."""
    new_entries = []
    skipped_stale = 0

    for url, category in feed_urls:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"[!] Failed to parse feed {url}: {e}")
            continue

        for entry in parsed.entries:
            link = entry.get("link", "")
            if not link or link in seen:
                continue

            age_hours = _entry_age_hours(entry)
            if age_hours is not None and age_hours > MAX_ARTICLE_AGE_HOURS:
                seen.add(link)  # never re-check a stale item again
                skipped_stale += 1
                continue

            new_entries.append({
                "title": entry.get("title", "Untitled"),
                "link": link,
                # feed summary/description is usually short — good enough
                # context for an LLM summary without fetching the full page
                "raw_summary": entry.get("summary", "") or entry.get("description", ""),
                "published": entry.get("published", ""),
                "published_epoch": calendar.timegm(entry["published_parsed"]) if entry.get("published_parsed") else 0,
                "category": category,
            })

    if skipped_stale:
        print(f"[i] Skipped {skipped_stale} item(s) older than {MAX_ARTICLE_AGE_HOURS}h (marked seen, won't recheck).")

    return new_entries


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


def triage_batch_with_groq(articles: list) -> list:
    max_tokens = min(1024, 6 * len(articles) + 100)
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": build_triage_prompt(articles)}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    return _parse_int_array(raw, len(articles))


def triage_batch_with_gemini(articles: list) -> list:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        json={"contents": [{"parts": [{"text": build_triage_prompt(articles)}]}]},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_int_array(raw, len(articles))


def triage_batch(articles: list) -> list:
    """Returns a list of ints (scores), same order as `articles`. Headlines only."""
    if LLM_PROVIDER == "gemini":
        return triage_batch_with_gemini(articles)
    return triage_batch_with_groq(articles)


# ---------------------------------------------------------------------------
# Stage 2: SUMMARIZE — only called for articles that cleared the threshold.
# Full title + content sent, one summary string back per article.
# ---------------------------------------------------------------------------

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


def summarize_batch_with_groq(articles: list) -> list:
    max_tokens = min(4096, 150 * len(articles) + 200)
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": build_summarize_prompt(articles)}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"]
    return _parse_str_array(raw, len(articles))


def summarize_batch_with_gemini(articles: list) -> list:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        json={"contents": [{"parts": [{"text": build_summarize_prompt(articles)}]}]},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    return _parse_str_array(raw, len(articles))


def summarize_batch(articles: list) -> list:
    """Returns a list of summary strings, same order as `articles`."""
    if LLM_PROVIDER == "gemini":
        return summarize_batch_with_gemini(articles)
    return summarize_batch_with_groq(articles)


def chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def send_telegram(title: str, summary: str, link: str, score: int = None) -> bool:
    score_line = f"\U0001F4CA Criticality: {score}/10\n" if score is not None else ""
    text = f"*{escape_markdown(title)}*\n{score_line}\n{escape_markdown(summary)}\n\n{link}"
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": False,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        print(f"[!] Telegram send failed ({resp.status_code}): {resp.text}")
        return False
    return True


def escape_markdown(text: str) -> str:
    for ch in ("_", "*", "[", "]"):
        text = text.replace(ch, f"\\{ch}")
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate_config() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
        missing.append("GROQ_API_KEY")
    if LLM_PROVIDER == "gemini" and not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if missing:
        print(f"[!] Missing required .env values: {', '.join(missing)}")
        sys.exit(1)


def main():
    validate_config()
    feed_urls = load_feed_urls()
    seen = load_seen()

    new_entries = fetch_new_entries(feed_urls, seen)
    print(f"[i] Found {len(new_entries)} new article(s) across {len(feed_urls)} feed(s) "
          f"(within the last {MAX_ARTICLE_AGE_HOURS}h).")

    if not new_entries:
        return

    capped_entries = new_entries[:MAX_NEW_ARTICLES_PER_RUN]
    if len(new_entries) > MAX_NEW_ARTICLES_PER_RUN:
        print(f"[i] Capping this run to {MAX_NEW_ARTICLES_PER_RUN} of {len(new_entries)} "
              f"found (rest will be picked up on future runs).")

    evaluated = 0
    sent = 0
    filtered_out = 0
    to_summarize = []  # list of (entry, score) that cleared the threshold

    # --- Stage 1: triage on headlines only ---
    for batch in chunked(capped_entries, EVAL_BATCH_SIZE):
        try:
            scores = triage_batch(batch)
        except requests.HTTPError as e:
            print(f"[!] Triage failed ({len(batch)} headlines): {e}")
            continue  # don't mark seen — retry this whole batch next run
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[!] Couldn't parse triage response: {e}")
            continue
        except Exception as e:
            print(f"[!] Unexpected error during triage: {e}")
            continue

        for entry, score in zip(batch, scores):
            evaluated += 1
            if score < CRITICALITY_THRESHOLD:
                print(f"[-] Filtered out (score {score}/10): {entry['title']}")
                seen.add(entry["link"])  # triaged once, never re-spend quota on it
                filtered_out += 1
            else:
                to_summarize.append((entry, score))

    # sort so the most critical articles get sent first; among equal scores,
    # the more recent article wins (a 1-hour-old 8/10 beats a 48-hour-old 8/10)
    to_summarize.sort(key=lambda pair: (pair[1], pair[0]["published_epoch"]), reverse=True)

    # --- Stage 2: summarize only the articles that cleared the bar ---
    for batch in chunked(to_summarize, EVAL_BATCH_SIZE):
        entries = [pair[0] for pair in batch]
        scores_by_entry = {pair[0]["link"]: pair[1] for pair in batch}
        try:
            summaries = summarize_batch(entries)
        except requests.HTTPError as e:
            print(f"[!] Summarize failed ({len(entries)} articles): {e}")
            continue  # don't mark seen — retry next run
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"[!] Couldn't parse summarize response: {e}")
            continue
        except Exception as e:
            print(f"[!] Unexpected error during summarize: {e}")
            continue

        for entry, summary in zip(entries, summaries):
            title, link = entry["title"], entry["link"]
            score = scores_by_entry[link]
            if send_telegram(title, summary, link, score=score):
                print(f"[+] Sent (score {score}/10): {title}")
                seen.add(link)
                sent += 1
            else:
                print(f"[!] Telegram send failed, will retry next run: {title}")
            time.sleep(1)  # light pacing between Telegram sends only

    save_seen(seen)
    print(f"[i] Done. Triaged {evaluated}, sent {sent}, filtered out {filtered_out}.")


if __name__ == "__main__":
    main()
