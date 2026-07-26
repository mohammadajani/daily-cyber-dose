#!/usr/bin/env python3
"""
Hermes Lite — RSS -> LLM summary -> Telegram
Runs once per invocation. Trigger it with cron / systemd timer / Termux job scheduler.

Works unmodified on: PC, Linux, Termux (Android), Raspberry Pi Zero W / 2W.
"""

import json
import os
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
SEEN_HISTORY_CAP = 2000  # how many URLs to remember before trimming the oldest

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

def load_seen() -> set:
    if not SEEN_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen(seen: set) -> None:
    trimmed = list(seen)[-SEEN_HISTORY_CAP:]
    SEEN_FILE.write_text(json.dumps(trimmed))


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

def load_feed_urls() -> list:
    if not FEEDS_FILE.exists():
        print(f"[!] {FEEDS_FILE} not found. Create it with one RSS URL per line.")
        sys.exit(1)
    lines = FEEDS_FILE.read_text().splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def fetch_new_entries(feed_urls: list, seen: set) -> list:
    """Return a flat list of dicts: {title, link, summary_source, published}."""
    new_entries = []
    for url in feed_urls:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"[!] Failed to parse feed {url}: {e}")
            continue

        for entry in parsed.entries:
            link = entry.get("link", "")
            if not link or link in seen:
                continue
            new_entries.append({
                "title": entry.get("title", "Untitled"),
                "link": link,
                # feed summary/description is usually short — good enough
                # context for an LLM summary without fetching the full page
                "raw_summary": entry.get("summary", "") or entry.get("description", ""),
                "published": entry.get("published", ""),
            })
    return new_entries


# ---------------------------------------------------------------------------
# Stage 1: TRIAGE — headlines only, scores only. Cheap: no article content
# sent, no summaries generated for articles that end up filtered out.
# ---------------------------------------------------------------------------

TRIAGE_PROMPT_HEADER = (
    "You are triaging cybersecurity news headlines for a security researcher, "
    "by how critical/actionable each one is. Below are {n} headlines, "
    "numbered. Respond with ONLY a JSON array of {n} integers (1-10), no "
    "objects, no markdown fences, no extra text — just the raw array, e.g. "
    "[7, 2, 9], in the SAME ORDER as the headlines.\n"
    "10 = actively-exploited vulnerability or major breach affecting "
    "widely-used systems, requiring immediate attention.\n"
    "5 = notable but not urgent.\n"
    "1 = minor/opinion/non-actionable news.\n\n"
)


def build_triage_prompt(articles: list) -> str:
    parts = [TRIAGE_PROMPT_HEADER.format(n=len(articles))]
    for i, art in enumerate(articles, 1):
        parts.append(f"{i}. {art['title']}")
    return "\n".join(parts)


def _parse_int_array(raw_text: str, expected_count: int) -> list:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
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
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
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
    print(f"[i] Found {len(new_entries)} new article(s) across {len(feed_urls)} feed(s).")

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

    # sort so the most critical articles get sent to Telegram first
    to_summarize.sort(key=lambda pair: pair[1], reverse=True)

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
