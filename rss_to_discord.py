#!/usr/bin/env python3
"""
RSS/Atom -> Discord webhook poster with:
- Reddit-friendly fetch (old.reddit.com + headers)
- Embeds w/ truncation (Discord limits)
- SQLite dedupe
- Enclosure media download (optional)
- Robust scheduler settings (no overlapping runs)
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
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

# ---------- Config ----------
load_dotenv()
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
if not WEBHOOK_URL:
    raise SystemExit("Please set DISCORD_WEBHOOK_URL in a .env file")

# Add/adjust feeds here
RSS_FEEDS = [
    "https://www.reddit.com/r/netsec/.rss",
    "https://www.reddit.com/r/bugbounty/.rss",
    "https://www.exploit-db.com/rss.xml",
    "https://krebsonsecurity.com/feed/",
    "https://thehackernews.com/feeds/posts/default",
]

# GitHub repo release feeds (ATOM)
GITHUB_RELEASE_ATOMS = [
    "https://github.com/rapid7/metasploit-framework/releases.atom",
    # "https://github.com/torvalds/linux/releases.atom",
]

POLL_INTERVAL_MINUTES = 5
DB_FILE = "rss_state.db"
MEDIA_DIR = "rss_media"
os.makedirs(MEDIA_DIR, exist_ok=True)

# Reddit-friendly headers (helps avoid HTML/anti-bot page)
REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) DiscordFeedBot/1.0 (+https://example.com)",
    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
}

# ---------- DB (dedupe) ----------
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
    # Reddit RSS behaves better on old.reddit.com
    if "reddit.com" in url:
        url = url.replace("www.reddit.com", "old.reddit.com")
        if not url.endswith(".rss"):
            url = url.rstrip("/") + "/.rss"
    return url

def fetch_feed_bytes(url: str, timeout=20) -> bytes | None:
    try:
        headers = REDDIT_HEADERS if "reddit.com" in url else {"Accept": "application/rss+xml, application/xml;q=0.9,*/*;q=0.8"}
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 200:
            print(f"[WARN] feed HTTP {r.status_code}: {url}")
            return None
        return r.content
    except Exception as e:
        print(f"[WARN] feed request failed: {url} ({e})")
        return None

def download_media(url: str, item_id: str, idx: int = 0) -> str | None:
    try:
        resp = requests.get(url, timeout=20, stream=True)
        if resp.status_code == 200:
            path = urlparse(url).path
            ext = os.path.splitext(path)[1] or ".bin"
            fn = os.path.join(MEDIA_DIR, f"{re.sub(r'[^a-zA-Z0-9_-]', '', item_id)}_{idx}{ext}")
            with open(fn, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return fn
        else:
            print(f"[WARN] media HTTP {resp.status_code}: {url}")
    except Exception as e:
        print("[WARN] media download error:", e)
    return None

def safe_entry_id(e, fallback_source: str) -> str | None:
    entry_id = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None)
    if not entry_id:
        # fallback if feed omits IDs
        title = getattr(e, "title", "no-title")
        published = getattr(e, "published", "") or getattr(e, "updated", "")
        entry_id = f"{fallback_source}|{title}|{published}"
    return entry_id

# ---------- Discord ----------
def post_to_discord(title: str, content: str, url: str, files: list[str] | None = None):
    # Embed-safe sizing
    title_s   = truncate(strip_html(title or "No title"), 256)
    desc_full = strip_html(content or "")
    desc_s    = truncate(desc_full, 4000)  # Discord embed description limit is 4096

    payload = {
        "embeds": [{
            "title": title_s,
            "url": url or "",
            "description": desc_s,
        }]
    }

    try:
        if files:
            # multipart/form-data: payload_json must be JSON string
            files_payload = []
            for i, filepath in enumerate(files):
                try:
                    files_payload.append((f"files[{i}]", open(filepath, "rb")))
                except Exception as e:
                    print("[WARN] could not open file:", filepath, e)
            r = requests.post(WEBHOOK_URL, data={"payload_json": json.dumps(payload)}, files=files_payload, timeout=25)
        else:
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
    # Post oldest first for nice chronology
    for e in reversed(entries):
        entry_id = safe_entry_id(e, feed_url)
        if not entry_id or seen(entry_id):
            continue

        title = e.get("title", "No title")
        summary = getattr(e, "summary", "") or getattr(e, "description", "")
        link = getattr(e, "link", url)

        files = []
        # RSS enclosures (images, etc.)
        if hasattr(e, "enclosures"):
            for idx, enc in enumerate(e.enclosures):
                href = enc.get("href")
                if href:
                    fp = download_media(href, entry_id, idx)
                    if fp:
                        files.append(fp)

        post_to_discord(title, summary, link, files=files if files else None)
        mark_seen(entry_id, feed_url, title)
        time.sleep(0.3)  # gentle throttle

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
    # Initial run
    poll_all()

    scheduler = BackgroundScheduler(
        job_defaults={
            "coalesce": True,           # skip backlog; run once if missed
            "max_instances": 1,         # no overlapping jobs
            "misfire_grace_time": 120,  # tolerate short delays
        }
    )
    scheduler.add_job(poll_all, "interval", minutes=POLL_INTERVAL_MINUTES)
    scheduler.start()
    print(f"Started scheduler. Ctrl-C to quit. Interval={POLL_INTERVAL_MINUTES} min")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Exiting...")
        scheduler.shutdown()
