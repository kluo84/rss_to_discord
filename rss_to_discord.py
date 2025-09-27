#!/usr/bin/env python3
"""
RSS/Atom → Discord webhook
- Runs once per execution (GitHub Actions friendly)
- Dedupes with SQLite (stored in repo workspace)
- Handles Reddit RSS quirks
- Posts as Discord embeds (safe limits)
"""

import os
import re
import json
import html
import time
import sqlite3
import requests
import feedparser
from urllib.parse import urlparse

# ---------- Config ----------
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise SystemExit("Please set DISCORD_WEBHOOK_URL env var")

RSS_FEEDS = [
    "https://www.reddit.com/r/netsec/.rss",
    "https://www.reddit.com/r/bugbounty/.rss",
    "https://www.exploit-db.com/rss.xml",
    "https://krebsonsecurity.com/feed/",
    "https://thehackernews.com/feeds/posts/default",
]

GITHUB_RELEASE_ATOMS = [
    "https://github.com/rapid7/metasploit-framework/releases.atom",
]

DB_FILE = "rss_state.db"
MEDIA_DIR = "rss_media"
os.makedirs(MEDIA_DIR, exist_ok=True)

REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DiscordFeedBot/1.0",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

# ---------- DB ----------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS seen (
    id TEXT PRIMARY KEY,
    source TEXT,
    title TEXT,
    time INTEGER
)
""")
conn.commit()

def seen(entry_id: str) -> bool:
    cur.execute("SELECT 1 FROM seen WHERE id=?", (entry_id,))
    return cur.fetchone() is not None

def mark_seen(entry_id: str, source: str, title: str):
    cur.execute("INSERT OR IGNORE INTO seen (id, source, title, time) VALUES (?, ?, ?, ?)",
                (entry_id, source, title, int(time.time())))
    conn.commit()

# ---------- Helpers ----------
def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()

def truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

def normalize_feed_url(url: str) -> str:
    if "reddit.com" in url:
        url = url.replace("www.reddit.com", "old.reddit.com")
        if not url.endswith(".rss"):
            url = url.rstrip("/") + "/.rss"
    return url

def fetch_feed_bytes(url: str, timeout=20) -> bytes | None:
    try:
        headers = REDDIT_HEADERS if "reddit.com" in url else {"Accept": "application/rss+xml"}
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            print(f"[WARN] feed HTTP {r.status_code}: {url}")
            return None
        return r.content
    except Exception as e:
        print(f"[WARN] feed request failed: {url} ({e})")
        return None

def safe_entry_id(e, fallback_source: str) -> str | None:
    entry_id = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None)
    if not entry_id:
        title = getattr(e, "title", "no-title")
        published = getattr(e, "published", "") or getattr(e, "updated", "")
        entry_id = f"{fallback_source}|{title}|{published}"
    return entry_id

# ---------- Discord ----------
def post_to_discord(title: str, content: str, url: str):
    title_s   = truncate(strip_html(title or "No title"), 256)
    desc_full = strip_html(content or "")
    desc_s    = truncate(desc_full, 4000)

    payload = {
        "embeds": [{
            "title": title_s,
            "url": url or "",
            "description": desc_s,
        }]
    }

    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=20)
        if r.status_code == 429:
            retry = int(r.headers.get("Retry-After", "1"))
            time.sleep(retry + 1)
            requests.post(WEBHOOK_URL, json=payload, timeout=20)
        elif r.status_code >= 400:
            print("Discord post failed:", r.status_code, r.text)
    except Exception as e:
        print("Discord post error:", e)

# ---------- Feed processing ----------
def process_feed_url(feed_url: str, limit: int = 5):
    url = normalize_feed_url(feed_url)
    raw = fetch_feed_bytes(url)
    if not raw:
        return

    feed = feedparser.parse(raw)
    if feed.bozo:
        print(f"[WARN] feed parse problem: {feed_url} ({getattr(feed, 'bozo_exception', 'parse error')})")
        return

    entries = feed.entries[:limit]
    for e in reversed(entries):
        entry_id = safe_entry_id(e, feed_url)
        if not entry_id or seen(entry_id):
            continue

        title = e.get("title", "No title")
        summary = getattr(e, "summary", "") or getattr(e, "description", "")
        link = getattr(e, "link", url)

        post_to_discord(title, summary, link)
        mark_seen(entry_id, feed_url, title)
        time.sleep(0.3)

def poll_all():
    print("Polling feeds...")
    for rss in RSS_FEEDS:
        try:
            process_feed_url(rss, limit=5)
        except Exception as ex:
            print("Error processing RSS", rss, ex)

    for atom in GITHUB_RELEASE_ATOMS:
        try:
            process_feed_url(atom, limit=5)
        except Exception as ex:
            print("Error processing GitHub atom", atom, ex)

# ---------- Main ----------
if __name__ == "__main__":
    poll_all()
