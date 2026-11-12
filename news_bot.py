#!/usr/bin/env python3
"""AI / 金融新闻 → Telegram 推送机器人

设计要点（都是上一版踩过的坑）：
1. 去重状态必须持久化，否则 GitHub Actions 每次都是全新容器，会把所有新闻重发一遍
2. Telegram 的 parse_mode=HTML 必须对文本做转义，标题里的 & < > 会让接口直接 400
3. RSS 的 summary 是 HTML，要剥成纯文本再用，且不能截断在标签中间
4. 发送失败要打印接口返回的 description，不能 except: pass 静默吞掉
5. 关键词要用词边界匹配，"ai" 用子串会命中 said / chairman / rain，等于不过滤
6. 遇到 429 要读 retry_after 等待重试，不能直接放弃
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

# ------------------------------------------------------------------ 配置

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()

STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen.json"))
MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "2"))        # 单次运行最多推几条
PER_FEED = int(os.getenv("PER_FEED", "4"))              # 每个源每轮最多看几条
MIN_SCORE = float(os.getenv("MIN_SCORE", "1"))          # 关键词命中数下限
MAX_AGE_HOURS = float(os.getenv("MAX_AGE_HOURS", "30")) # 超过这个时间的旧闻不推
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "10"))           # 去重记录保留天数

# 有些站会拦默认 UA（VentureBeat 直接 429），伪装成浏览器
feedparser.USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

FEEDS = [
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Google News · AI", "https://news.google.com/rss/search?q=AI+OR+artificial+intelligence+OR+LLM+OR+OpenAI+OR+Anthropic&hl=en-US&gl=US&ceid=US:en"),
    ("Google News · Finance", "https://news.google.com/rss/search?q=stock+market+OR+finance+OR+earnings+OR+Fed+OR+NVIDIA&hl=en-US&gl=US&ceid=US:en"),
]

# 用"整词"匹配，避免 ai 命中 said / chairman / rain 这类假阳性
KEYWORDS = [
    "ai", "llm", "llms", "gpt", "chatgpt", "openai", "anthropic", "claude",
    "gemini", "deepseek", "nvidia", "gpu", "model", "agent", "inference",
    "stock", "stocks", "earnings", "fed", "finance", "financial", "market",
    "nasdaq", "s&p", "dow", "ipo", "chip", "chips", "semiconductor",
]

WORD_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(k) for k in sorted(KEYWORDS, key=len, reverse=True)) + r")(?![a-z0-9])",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\u00a0]+")
BLANK_RE = re.compile(r"\n{3,}")


# ------------------------------------------------------------------ 工具

def strip_html(raw: str, limit: int = 200) -> str:
    """RSS summary 是 HTML，剥成纯文本再用于消息。"""
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
        # 尽量在句末或空格处收尾，避免出现半截单词
        for sep in (". ", "! ", "? ", "; ", " "):
            idx = cut.rfind(sep)
            if idx > limit * 0.6:
                cut = cut[: idx + len(sep)]
                break
        s = cut.rstrip() + "…"
    return s


def norm_title(title: str) -> str:
    """标题归一化：用于跨源去重（同一个事件多家转载只推一次）。"""
    t = html.unescape(title or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\b(the|a|an|of|to|in|on|for|and|with|as|at|by|is|are)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def uid_for(title: str, link: str) -> str:
    """主键用归一化标题（Google News 链接带随机参数，天天变，不能用链接做键）。"""
    key = norm_title(title) or link
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def score(title: str, summary: str) -> int:
    text = f"{title} {summary}"
    return len({m.group(1).lower() for m in WORD_RE.finditer(text)})


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
            if isinstance(data, list):  # 兼容旧版只有 list 的格式
                return {"seen": {k: now_utc().isoformat() for k in data}, "version": 2}
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] 状态文件损坏，按空处理：{e}")
    return {"seen": {}, "version": 2}


def save_state(state: dict) -> None:
    cutoff = (now_utc() - timedelta(days=KEEP_DAYS)).isoformat()
    seen = {k: v for k, v in state.get("seen", {}).items() if str(v) >= cutoff}
    state["seen"] = seen
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), "utf-8"
    )
    print(f"[state] 已保存 {len(seen)} 条去重记录 → {STATE_FILE}")


# ------------------------------------------------------------------ 抓取

def fetch_feed(name: str, url: str):
    try:
        feed = feedparser.parse(url)
        entries = list(feed.entries)[:PER_FEED]
        status = getattr(feed, "status", None)
        if not entries:
            return name, [], f"0 条（http={status}）"
        note = ""
        if status and status >= 400:
            note = f"http={status}，但解析到 {len(entries)} 条"
        return name, entries, note or f"{len(entries)} 条"
    except Exception as e:  # noqa: BLE001
        return name, [], f"失败 {type(e).__name__}: {e}"


def fetch_all() -> list[tuple[str, list]]:
    with ThreadPoolExecutor(max_workers=len(FEEDS)) as ex:
        futures = [ex.submit(fetch_feed, n, u) for n, u in FEEDS]
        results = []
        for (name, _), f in zip(FEEDS, futures):
            n, entries, note = f.result()
            print(f"[feed] {n:22} {note}")
            results.append((n, entries))
        return results


# ------------------------------------------------------------------ 发送

def send_message(text: str, chat_id: str = "", retries: int = 3) -> tuple[bool, str]:
    chat_id = chat_id or CHAT_ID
    if not BOT_TOKEN:
        return False, "缺少 BOT_TOKEN 环境变量"
    if not chat_id:
        return False, "缺少 CHAT_ID 环境变量"
    if len(text) > 4000:
        text = text[:3990] + "…"

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    last = "unknown"
    for attempt in range(retries):
        try:
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code == 200 and r.json().get("ok"):
                return True, "ok"
            if r.status_code == 429:  # 限流：按接口给的 retry_after 等待
                wait = int(r.json().get("parameters", {}).get("retry_after", 3))
                last = f"429 限流，等待 {wait}s"
                time.sleep(min(wait, 30))
                continue
            try:
                detail = r.json().get("description", "")
            except ValueError:
                detail = r.text[:200]
            last = f"HTTP {r.status_code}: {detail}"
            if r.status_code == 400:  # 请求本身有问题（如实体解析失败），重试没意义
                break
        except requests.RequestException as e:
            last = f"{type(e).__name__}: {e}"
        time.sleep(1.5 * (attempt + 1))
    return False, last


def fmt_message(source: str, title: str, summary: str, link: str) -> str:
    # 正文用 quote=False：Telegram 的 HTML 只认 &amp; &lt; &gt; &quot; 四个实体，
    # html.escape 默认会把 ' 转成 &#x27;，Telegram 会原样显示出来，很难看
    parts = [f"<b>[{html.escape(source, quote=False)}]</b>",
             html.escape(title, quote=False)]
    if summary:
        parts.append(html.escape(summary, quote=False))
    if link:
        parts.append(f'<a href="{html.escape(link, quote=True)}">阅读原文</a>')
    return "\n\n".join(parts)


def get_chat_id() -> int:
    if not BOT_TOKEN:
        print("缺少 BOT_TOKEN，无法查询。")
        return 1
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=20)
    data = r.json()
    if not data.get("ok"):
        print(f"接口报错：{data}")
        return 1
    found = {}
    for u in data.get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        c = m.get("chat") or {}
        if c.get("id"):
            found[c["id"]] = (c.get("type"), c.get("title") or c.get("first_name") or c.get("username"))
    if not found:
        print("还没有收到任何消息。请先在 Telegram 里搜索你的 bot，点开并发送 /start，然后重跑本命令。")
        return 1
    print("找到以下会话，把 id 填到 GitHub Secrets 的 CHAT_ID：")
    for cid, (ctype, name) in found.items():
        print(f"  chat_id = {cid}   ({ctype}, {name})")
    return 0


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
            # Google News 的 summary 就是"标题 + 来源"，跟标题重复，去掉
            if summary:
                nt = norm_title(title)
                if nt and norm_title(summary).startswith(nt[:40]):
                    summary = ""
            published = parse_time(e)
            if published and (now - published) > timedelta(hours=MAX_AGE_HOURS):
                continue
            sc = score(title, summary)
            if sc < MIN_SCORE:
                continue
            out.append({
                "uid": uid, "source": name, "title": title, "link": link,
                "summary": summary, "published": published, "score": sc,
            })
    # 新→旧；同一事件（标题归一化相同）只留一条
    out.sort(key=lambda x: (x["published"] or datetime.min.replace(tzinfo=timezone.utc)),
             reverse=True)
    uniq, seen_norm = [], set()
    for item in out:
        key = norm_title(item["title"])
        if key in seen_norm:
            continue
        seen_norm.add(key)
        uniq.append(item)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="AI/金融新闻 → Telegram 推送")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发送的内容，不真发")
    ap.add_argument("--test", action="store_true", help="发一条自检消息，验证 token/chat_id")
    ap.add_argument("--baseline", action="store_true", help="把当前所有新闻标记为已读，不发消息")
    ap.add_argument("--get-chat-id", action="store_true", help="打印能与 bot 对话的 chat_id")
    ap.add_argument("--reset", action="store_true", help="清空去重状态")
    args = ap.parse_args()

    if args.get_chat_id:
        return get_chat_id()

    if args.reset:
        save_state({"seen": {}, "version": 2})
        print("去重状态已清空。")

    if args.test:
        ok, detail = send_message(
            "✅ <b>机器人自检成功</b>\n\n"
            f"时间：{now_utc().astimezone().strftime('%Y-%m-%d %H:%M:%S')}\n"
            "收到这条消息说明 BOT_TOKEN 和 CHAT_ID 都配对了，可以开始推新闻。"
        )
        print(("已发送" if ok else "发送失败") + f"：{detail}")
        return 0 if ok else 1

    state = load_state()
    candidates = build_candidates(state)
    first_run = not state.get("seen")

    print(f"[main] 候选 {len(candidates)} 条，首轮 = {first_run}")

    if args.baseline or first_run:
        # 首轮只建基线，不推历史，避免一次性刷 40 条把手机打爆
        for item in candidates:
            state["seen"][item["uid"]] = now_utc().isoformat()
        save_state(state)
        print(f"[baseline] 已把 {len(candidates)} 条标记为已读，未推送。"
              f"用 --test 可验证通道，下一轮开始正常推送。")
        return 0

    batch = candidates[:MAX_PER_RUN]
    if len(candidates) > MAX_PER_RUN:
        print(f"[main] 超过单轮上限 {MAX_PER_RUN}，只推最新的 {MAX_PER_RUN} 条")

    sent = failed = 0
    for item in batch:
        text = fmt_message(item["source"], item["title"], item["summary"], item["link"])
        if args.dry_run:
            print("\n" + "-" * 60)
            print(text)
            sent += 1
            continue
        ok, detail = send_message(text)
        if ok:
            sent += 1
            print(f"[sent] {item['source']}: {item['title'][:60]}")
        else:
            failed += 1
            print(f"[fail] {item['source']}: {item['title'][:60]} → {detail}", file=sys.stderr)
        # 成功的和失败的都记账，避免下一轮对同一条反复重试造成刷屏
        state["seen"][item["uid"]] = now_utc().isoformat()
        time.sleep(0.8)  # 温和节流，避免触发 Telegram 限流

    save_state(state)
    print(f"[done] 推送 {sent} 条，失败 {failed} 条，候选 {len(candidates)} 条")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
