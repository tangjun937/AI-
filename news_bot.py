#!/usr/bin/env python3
"""金融 + AI 新闻 → X（Buffer）自动发布机器人

数据源为英文 RSS，直接发 X，无需翻译。
只发金融 / AI 相关新闻，其他领域过滤掉。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

# ------------------------------------------------------------------ 配置

BUFFER_TOKEN = (os.getenv("BUFFER_TOKEN") or "").strip()
BUFFER_CHANNEL_ID = (os.getenv("BUFFER_CHANNEL_ID") or "6ae73010e29c69a17126cbf5").strip()
BUFFER_ENDPOINT = "https://api.buffer.com"

STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "2"))        # 单次最多发几条
PER_FEED = int(os.getenv("PER_FEED", "4"))              # 每个源每轮看几条
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "30")) # 旧闻过滤
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "10"))           # 去重记录保留天数
INCLUDE_LINK = os.getenv("INCLUDE_LINK", "0") == "1"    # 是否带文章链接（默认不带）
SPACING_MIN = int(os.getenv("SPACING_MIN", "10"))       # 第 2 条间隔下限（分钟）
SPACING_MAX = int(os.getenv("SPACING_MAX", "15"))       # 第 2 条间隔上限（分钟）

feedparser.USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---- 信息源（AI + 金融，英文）----
FEEDS = [
    # AI
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Google News · AI", "https://news.google.com/rss/search?q=AI+OR+artificial+intelligence+OR+LLM+OR+OpenAI+OR+Anthropic+OR+NVIDIA&hl=en-US&gl=US&ceid=US:en"),
    # 金融
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
    ("Google News · Finance", "https://news.google.com/rss/search?q=stock+market+OR+finance+OR+earnings+OR+Federal+Reserve+OR+NVIDIA+OR+inflation&hl=en-US&gl=US&ceid=US:en"),
]

# 整词匹配，只放行金融 + AI 相关新闻
KEYWORDS = [
    # AI
    "ai", "a.i.", "artificial intelligence", "machine learning", "llm", "llms", "gpt",
    "chatgpt", "openai", "anthropic", "claude", "gemini", "deepseek", "nvidia", "gpu",
    "neural", "model", "models", "agent", "agents", "inference", "chatbot", "copilot",
    "datacenter", "data center", "semiconductor", "chip", "chips",
    # 金融
    "stock", "stocks", "earnings", "fed", "federal reserve", "finance", "financial",
    "market", "markets", "nasdaq", "s&p", "dow", "ipo", "inflation", "interest rate",
    "rate hike", "sec", "etf", "crypto", "bitcoin", "wall street", "investor", "investors",
    "shares", "trading", "merger", "acquisition", "revenue", "quarterly", "bull", "bear",
    "economy", "gdp", "recession",
]

WORD_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in sorted(KEYWORDS, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0]+")
BLANK_RE = re.compile(r"\n{3,}")

M_CREATE = """
mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id text dueAt status } }
    ... on MutationError { message }
  }
}
"""


# ------------------------------------------------------------------ 工具

def strip_html(raw: str, limit: int = 220) -> str:
    if not raw:
        return ""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    s = WS_RE.sub(" ", s)
    s = BLANK_RE.sub("\n\n", s).strip()
    if len(s) > limit:
        cut = s[:limit]
        for sep in (". ", "! ", "? ", "; ", " "):
            idx = cut.rfind(sep)
            if idx > limit * 0.6:
                cut = cut[: idx + len(sep)]
                break
        s = cut.rstrip() + "…"
    return s


def norm_title(title: str) -> str:
    t = html.unescape(title or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\b(the|a|an|of|to|in|on|for|and|with|as|at|by|is|are)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def uid_for(title: str, link: str) -> str:
    key = norm_title(title) or link
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def score(title: str, summary: str) -> int:
    return len({m.group(1).lower() for m in WORD_RE.finditer(f"{title} {summary}")})


def parse_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        v = entry.get(key)
        if v:
            try:
                return datetime.fromtimestamp(time.mktime(v), tz=timezone.utc)
            except (OverflowError, ValueError):
                continue
    return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ 状态

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text("utf-8"))
            if isinstance(data, dict) and "seen" in data:
                return data
            if isinstance(data, list):
                return {"seen": {k: now_utc().isoformat() for k in data}, "version": 2}
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": {}, "version": 2}


def save_state(state: dict) -> None:
    cutoff = (now_utc() - timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state.get("seen", {}).items() if str(v) >= cutoff}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


# ------------------------------------------------------------------ 抓取

def fetch_feed(name: str, url: str):
    try:
        r = requests.get(url, headers={"User-Agent": feedparser.USER_AGENT}, timeout=20)
        if r.status_code >= 400:
            return name, [], f"http={r.status_code}"
        feed = feedparser.parse(r.content)
        entries = list(feed.entries)[:PER_FEED]
        if not entries:
            return name, [], "0 条"
        return name, entries, f"{len(entries)} 条"
    except Exception as e:  # noqa: BLE001
        return name, [], f"失败 {type(e).__name__}: {e}"


def fetch_all() -> list[tuple[str, list]]:
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as ex:
        futures = [ex.submit(fetch_feed, n, u) for n, u in FEEDS]
        results = []
        for (name, _), f in zip(FEEDS, futures):
            n, entries, note = f.result()
            print(f"[feed] {n:18} {note}")
            results.append((n, entries))
        return results


# ------------------------------------------------------------------ 发 X

def buffer_post(text: str, mode: str = "shareNow", due_at: str | None = None) -> tuple[bool, str]:
    if not BUFFER_TOKEN:
        return False, "缺少 BUFFER_TOKEN"
    if len(text) > 4000:
        text = text[:3990] + "…"
    inp = {
        "text": text,
        "channelId": BUFFER_CHANNEL_ID,
        "schedulingType": "automatic",
        "mode": mode,
        "needsApproval": False,
        "assets": [],
    }
    if due_at:
        inp["dueAt"] = due_at
    for attempt in range(3):
        try:
            r = requests.post(
                BUFFER_ENDPOINT,
                data=json.dumps({"query": M_CREATE, "variables": {"input": inp}}).encode(),
                headers={"Authorization": f"Bearer {BUFFER_TOKEN}", "Content-Type": "application/json"},
                timeout=30,
            )
            data = r.json()
            if data.get("errors"):
                return False, "; ".join(str(e.get("message", e)) for e in data["errors"])
            res = (data.get("data") or {}).get("createPost") or {}
            if "message" in res and "post" not in res:
                return False, f"Buffer 拒绝：{res['message']}"
            post = res.get("post") or {}
            return bool(post.get("id")), str(post.get("id", ""))
        except requests.RequestException as e:
            if attempt == 2:
                return False, f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (attempt + 1))
    return False, "unknown"


def fmt_post(source: str, title: str, summary: str, link: str) -> str:
    # X 是纯文本，不加任何 HTML 标签
    parts = [title.strip()]
    if summary:
        parts.append(summary.strip())
    if INCLUDE_LINK and link:
        parts.append(link.strip())
    return "\n\n".join(parts)


# ------------------------------------------------------------------ 主流程

def build_candidates(state: dict) -> list[dict]:
    seen = state.get("seen", {})
    out: list[dict] = []
    now = now_utc()
    for name, entries in fetch_all():
        for e in entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title:
                continue
            uid = uid_for(title, link)
            if uid in seen:
                continue
            summary = strip_html(e.get("summary") or e.get("description") or "")
            if summary:
                nt = norm_title(title)
                if nt and norm_title(summary).startswith(nt[:40]):
                    summary = ""
            published = parse_time(e)
            if published and (now - published) > timedelta(hours=MAX_AGE_HOURS):
                continue
            sc = score(title, summary)
            if sc < 1:
                continue
            out.append({
                "uid": uid, "source": name, "title": title, "link": link,
                "summary": summary, "published": published, "score": sc,
            })
    out.sort(key=lambda x: (x["published"] or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
    uniq, seen_norm = [], set()
    for item in out:
        key = norm_title(item["title"])
        if key in seen_norm:
            continue
        seen_norm.add(key)
        uniq.append(item)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="金融+AI 新闻 → X 自动发布")
    ap.add_argument("--dry-run", action="store_true", help="只打印不真发")
    ap.add_argument("--baseline", action="store_true", help="把当前新闻标记为已读，不发")
    ap.add_argument("--reset", action="store_true", help="清空去重状态")
    args = ap.parse_args()

    if args.reset:
        save_state({"seen": {}, "version": 2})
        print("去重状态已清空")

    state = load_state()
    candidates = build_candidates(state)
    first_run = not state.get("seen")
    print(f"[main] 候选 {len(candidates)} 条，首轮 = {first_run}")

    if args.baseline or first_run:
        for item in candidates:
            state["seen"][item["uid"]] = now_utc().isoformat()
        save_state(state)
        print(f"[baseline] 已把 {len(candidates)} 条标记为已读，未发布。下一轮开始正常发。")
        return 0

    batch = candidates[:MAX_PER_RUN]
    sent = failed = 0
    for idx, item in enumerate(batch):
        text = fmt_post(item["source"], item["title"], item["summary"], item["link"])
        if args.dry_run:
            print("\n" + "-" * 60 + "\n" + text)
            sent += 1
            continue
        mode, due_at = "shareNow", None
        if idx > 0:
            delay = random.randint(SPACING_MIN, SPACING_MAX)
            due = now_utc() + timedelta(minutes=delay)
            due_at = due.strftime("%Y-%m-%dT%H:%M:%SZ")
            mode = "customScheduled"
            print(f"[schedule] 第 {idx + 1} 条排到 {delay} 分钟后")
        ok, pid = buffer_post(text, mode=mode, due_at=due_at)
        state["seen"][item["uid"]] = now_utc().isoformat()
        if ok:
            sent += 1
            print(f"[sent] {item['source']}: {item['title'][:60]}")
        else:
            failed += 1
            print(f"[fail] {item['source']}: {item['title'][:60]} → {pid}", file=sys.stderr)
        time.sleep(0.8)

    save_state(state)
    print(f"[done] 发 {sent} 条，失败 {failed} 条，候选 {len(candidates)} 条")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
