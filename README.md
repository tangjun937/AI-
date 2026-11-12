# AI- · AI / 金融新闻 → Telegram 推送机器人

订阅 6 个 AI 与金融资讯源，每 2 小时自动挑出相关新闻，推送到你的 Telegram。

Bot：[@SmartFinNewsBot](https://t.me/SmartFinNewsBot)（「金融AI资讯」）

---

## 一、上线三步

### 1. 拿到你的 CHAT_ID

在 Telegram 里搜索 `@SmartFinNewsBot`，点开，发一句 `/start`。然后在本地运行：

```bash
pip install -r requirements.txt
BOT_TOKEN=你的token python news_bot.py --get-chat-id
```

它会打印出 `chat_id = 一串数字`。

> 收不到消息就别往后走 —— 这是最常见卡点。必须**你先给 bot 发消息**，bot 才知道该往哪推。

### 2. 配置 GitHub Secrets

仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Name | Value |
|---|---|
| `BOT_TOKEN` | BotFather 给的 token |
| `CHAT_ID` | 上一步拿到的数字 |

### 3. 先自检，再开头

Actions 页 → 「AI 新闻推送」→ Run workflow → 模式选 **test**：

- 手机收到「✅ 机器人自检成功」→ 通道没问题
- 没收到 → 看日志里的 `HTTP 400` / `403` 报错，对照下面排查表

自检通过后，选 **run** 跑一次。**第一次运行只会建立去重基线，不会推送任何历史新闻**（避免一上来刷 40 条），从下一轮开始正常推。

---

## 二、你原来那版的问题（都已修复）

| # | 问题 | 后果 | 修复 |
|---|---|---|---|
| 1 | `seen.json` 没有持久化 | Actions 每次都是全新容器，每 2 小时把同样几十条新闻**重复推送**一遍 | 用 `actions/cache` 存取 `state/` |
| 2 | 标题没做 HTML 转义 | 标题含 `&` `<` `>` 时 Telegram 返回 400，被 `except: return False` 静默吞掉，**你根本不知道发了没发** | `html.escape()` + 打印接口 `description` |
| 3 | summary 是 HTML 原文 | Telegram 解析实体失败；`[:180]` 还会把标签截断成半截 | `strip_html()` 剥标签 + 句末收尾 |
| 4 | 关键词用 `k in text` 子串匹配 | `"ai"` 会命中 `s**ai**d` / `ch**ai**rman` / `r**ai**n`，等于**完全不过滤** | 词边界正则 `(?<![a-z0-9])ai(?![a-z0-9])` |
| 5 | 用 link 的 md5 做去重主键 | Google News 链接带随机参数天天变，同一条新闻天天当新的推 | 改用归一化标题做键 |
| 6 | 6 个源串行抓、异常只 print | 单个源挂了拖慢全部；VentureBeat 被 429 也无人知晓 | 线程池并发 + 每源健康状态打印 + 浏览器 UA |
| 7 | 没有首轮基线 | 第一次部署瞬间推 40+ 条 | 首轮只记基线不推送 |
| 8 | 没有 429 退避 | 触发限流就丢消息 | 读 `retry_after` 等待重试 |
| 9 | 没有旧闻过滤 | RSS 里的陈年旧闻也会推 | `MAX_AGE_HOURS` 默认 30 小时 |

---

## 三、本地调试

```bash
pip install -r requirements.txt

# 只看会发什么，不真发
BOT_TOKEN=xx CHAT_ID=yy python news_bot.py --dry-run

# 发一条自检消息
BOT_TOKEN=xx CHAT_ID=yy python news_bot.py --test

# 正常跑一轮
BOT_TOKEN=xx CHAT_ID=yy python news_bot.py

# 清空去重记录（想让旧新闻重推时用）
python news_bot.py --reset
```

## 四、可调参数

都在 `news_bot.py` 顶部或 workflow 的 `env` 里：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_PER_RUN` | 10 | 单次运行最多推几条 |
| `PER_FEED` | 4 | 每个源每轮最多看几条 |
| `MAX_AGE_HOURS` | 30 | 超过这个时间的旧闻不推 |
| `MIN_SCORE` | 1 | 至少命中几个关键词才算相关 |
| `KEEP_DAYS` | 10 | 去重记录保留天数 |

改信息源就改 `FEEDS` 列表，加一行 `("名字", "RSS地址")` 即可。改兴趣方向就改 `KEYWORDS`。

## 五、排查表

| 现象 | 原因 | 处理 |
|---|---|---|
| `HTTP 400: chat not found` | CHAT_ID 错了，或你从没给 bot 发过 `/start` | 重新跑 `--get-chat-id` |
| `HTTP 400: can't parse entities` | 转义有遗漏 | 检查是不是绕过了 `fmt_message()` |
| `HTTP 401: Unauthorized` | token 错或已被 revoke | 去 BotFather 重新拿 |
| `HTTP 403: bot was blocked by the user` | 你把这个 bot 屏蔽了 | 解除屏蔽 |
| Actions 不自动跑 | 仓库 60 天无提交，定时任务被自动停用 | 随便提交一次，或手动 Run |

## 六、注意

- **token 不要写进代码**。只放 GitHub Secrets 和本地环境变量。
- 这个 token 之前在对话里贴出来过，等于已经泄露。建议去 [@BotFather](https://t.me/BotFather) 发 `/revoke` 重置，然后只更新 GitHub Secrets 里的 `BOT_TOKEN`，代码一行都不用改。
- GitHub 的 cron 是「最快每 2 小时」，实际会有 5-20 分钟延迟，这是平台特性，不影响使用。
