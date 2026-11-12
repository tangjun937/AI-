import feedparser
import requests
import hashlib
import json
import os
from datetime import datetime

# 从环境变量读取（更安全）
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SEEN_FILE = "seen.json"

FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Google News AI", "https://news.google.com/rss/search?q=AI+OR+artificial+intelligence+OR+LLM+OR+OpenAI&hl=en-US&gl=US&ceid=US:en"),
    ("Google News Finance", "https://news.google.com/rss/search?q=stock+market+OR+finance+OR+earnings+OR+Fed+OR+NVIDIA&hl=en-US&gl=US&ceid=US:en"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
]

KEYWORDS = [
    "ai", "llm", "gpt", "openai", "nvidia", "anthropic", "claude",
    "stock", "earnings", "fed", "finance", "market", "nasdaq", "s&p"
]

def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)

def is_relevant(title, summary):
    text = (title + " " + summary).lower()
    return any(k in text for k in KEYWORDS)

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(url, data=data, timeout=15)
        return r.status_code == 200
    except:
        return False

def main():
    seen = load_seen()
    new_count = 0

    for name, url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = entry.get("title", "")
                link = entry.get("link", "")
                summary = entry.get("summary", "")[:180]
                uid = hashlib.md5(link.encode()).hexdigest()

                if uid not in seen and is_relevant(title, summary):
                    seen.add(uid)
                    msg = (
                        f"<b>[{name}]</b>\n"
                        f"{title}\n\n"
                        f"{summary}...\n"
                        f"<a href='{link}'>阅读原文</a>"
                    )
                    if send_telegram(msg):
                        new_count += 1
                        print(f"已推送: {title[:60]}")
        except Exception as e:
            print(f"抓取 {name} 失败: {e}")

    save_seen(seen)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 本次推送 {new_count} 条")

if __name__ == "__main__":
    main()
