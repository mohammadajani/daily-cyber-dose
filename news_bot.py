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

MAX_NEW_ARTICLES_PER_RUN = int(os.getenv("MAX_NEW_ARTICLES_PER_RUN", "5"))
SEEN_HISTORY_CAP = 2000  # how many URLs to remember before trimming the oldest

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
# LLM summarization
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = (
    "Summarize this news article in 2-3 concise sentences for a busy reader. "
    "Be factual and neutral, no fluff, no restating the headline verbatim.\n\n"
    "Title: {title}\n\nContent: {content}"
)


def summarize_with_groq(title: str, content: str) -> str:
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "user", "content": SUMMARY_PROMPT.format(title=title, content=content)}
            ],
            "temperature": 0.3,
            "max_tokens": 150,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def summarize_with_gemini(title: str, content: str) -> str:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    resp = requests.post(
        url,
        json={
            "contents": [{
                "parts": [{"text": SUMMARY_PROMPT.format(title=title, content=content)}]
            }]
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def summarize(title: str, content: str) -> str:
    if not content:
        content = "(no snippet available, summarize based on title only)"
    if LLM_PROVIDER == "gemini":
        return summarize_with_gemini(title, content)
    return summarize_with_groq(title, content)


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

def send_telegram(title: str, summary: str, link: str) -> bool:
    text = f"*{escape_markdown(title)}*\n\n{escape_markdown(summary)}\n\n{link}"
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

    processed = 0
    for entry in new_entries:
        if processed >= MAX_NEW_ARTICLES_PER_RUN:
            print(f"[i] Reached MAX_NEW_ARTICLES_PER_RUN ({MAX_NEW_ARTICLES_PER_RUN}), stopping for this run.")
            break

        title, link = entry["title"], entry["link"]
        try:
            summary = summarize(title, entry["raw_summary"])
        except requests.HTTPError as e:
            print(f"[!] LLM summarize failed for '{title}': {e}")
            continue
        except Exception as e:
            print(f"[!] Unexpected error summarizing '{title}': {e}")
            continue

        if send_telegram(title, summary, link):
            print(f"[+] Sent: {title}")
            seen.add(link)
            processed += 1
        else:
            print(f"[!] Skipped marking as seen (send failed): {title}")

        time.sleep(2)  # stay well under free-tier rate limits

    save_seen(seen)
    print(f"[i] Done. Sent {processed} article(s) this run.")


if __name__ == "__main__":
    main()
