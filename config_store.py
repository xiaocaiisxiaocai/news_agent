# ============================================================
# config_store.py —— 所有配置存 SQLite，Web 页面可读写
# ============================================================
# 不再有 config.py 硬编码配置。所有设置通过 get(key) / set_config(key, value) 操作。
# 首次运行自动写入默认值。

import sqlite3, json, os, builtins
from datetime import datetime
from contextlib import contextmanager

# 项目根目录（基于本文件位置）
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_PROJECT_ROOT, "data", "news_agent.db")

DEFAULTS = {
    # LLM
    "llm.active":       "deepseek",
    "llm.deepseek.url": "https://api.deepseek.com/v1",
    "llm.deepseek.key": "",
    "llm.deepseek.model": "deepseek-chat",
    "llm.qwen.url":     "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "llm.qwen.key":     "",
    "llm.qwen.model":   "qwen-plus",
    "llm.siliconflow.url": "https://api.siliconflow.cn/v1",
    "llm.siliconflow.key": "",
    "llm.siliconflow.model": "deepseek-ai/DeepSeek-V3",
    "llm.timeout":      "60",
    "llm.max_retries":  "3",
    "llm.retry_delay":  "2",

    # 处理功能开关
    "feature.evaluator":        "true",
    "feature.eval_min_score":   "7",
    "feature.eval_max_retry":   "1",
    "feature.importance":       "true",
    "feature.bilingual":        "true",
    "feature.keywords":         "true",
    "feature.topic_cluster":    "true",
    "feature.max_workers":      "5",

    # RSS
    "rss.feeds": json.dumps([
        "https://sspai.com/feed",
        "https://www.ifanr.com/feed",
        "https://36kr.com/feed",
        "https://www.infoq.cn/feed",
    ]),
    "rss.sources": json.dumps([
        {"name": "少数派", "url": "https://sspai.com/feed", "category": "科技/AI", "enabled": True},
        {"name": "爱范儿", "url": "https://www.ifanr.com/feed", "category": "科技/AI", "enabled": True},
        {"name": "36氪", "url": "https://36kr.com/feed", "category": "商业", "enabled": True},
        {"name": "InfoQ", "url": "https://www.infoq.cn/feed", "category": "科技/AI", "enabled": True},
        {"name": "微博热搜", "url": "https://rsshub.app/weibo/search/hot", "category": "其他", "enabled": True},
        {"name": "知乎热榜", "url": "https://rsshub.app/zhihu/hot", "category": "其他", "enabled": True},
        {"name": "抖音热点", "url": "https://rsshub.app/douyin/hot", "category": "其他", "enabled": True},
        {"name": "GitHub Trending 日榜", "url": "https://rsshub.app/github/trending/daily", "category": "科技/AI", "enabled": True},
        {"name": "GitHub Trending 周榜", "url": "https://rsshub.app/github/trending/weekly", "category": "科技/AI", "enabled": True},
        {"name": "掘金一周热门", "url": "https://rsshub.app/juejin/trending/all/weekly", "category": "科技/AI", "enabled": True},
        {"name": "今日头条热榜", "url": "https://rsshub.app/toutiao/hot", "category": "其他", "enabled": True},
        {"name": "百度热搜", "url": "https://rsshub.app/baidu/hot", "category": "其他", "enabled": True},
        {"name": "36氪快讯", "url": "https://rsshub.app/36kr/newsflash", "category": "商业", "enabled": True},
        {"name": "B站全站排行", "url": "https://rsshub.app/bilibili/ranking/0/3/1", "category": "B站", "enabled": True},
    ], ensure_ascii=False),
    "rss.max_per_feed": "5",

    # 邮件
    "email.enabled":     "false",
    "email.imap_server": "imap.gmail.com",
    "email.username":    "",
    "email.password":    "",
    "email.label":       "Newsletter",
    "email.limit":       "10",

    # 个性化偏好（JSON）
    "pref.boost":    json.dumps({"Anthropic": 2, "Claude": 2, "开源": 1}),
    "pref.penalty":  json.dumps({"广告": -2, "营销": -1}),
    "pref.muted":    json.dumps([]),
    "pref.cat_weight": json.dumps({"科技/AI": 0, "商业": 0, "学术": 1, "即刻": -1, "B站": 0, "其他": 0}),

    # 简报
    "brief.top_n":        "5",
    "brief.min_importance": "3",

    # 定时
    "schedule.rss_time":   "08:30",
    "schedule.brief_time": "21:00",

    # 通知自动推送
    "notify.push.enabled": "false",
    "notify.push.time":    "09:00",
    "notify.push.limit":   "5",
    "notify.push.days":    "1",
    "notify.push.skip_sent": "true",
    "notify.push.dedupe_days": "7",
    "notify.template.email_subject": "[资讯] {title}",
    "notify.template.email_body": "{title}\n\n{conclusion}\n\n追踪链接：{tracking_url}\n原文链接：{original_url}",
    "notify.template.webhook_include_summary": "true",
    "notify.template.webhook_include_original_url": "true",

    # Web
    "web.host": "127.0.0.1",
    "web.port": "5000",
    "web.access_token.enabled": "false",
    "web.access_token": "",
}

RSS_FEED_MIGRATIONS = {
    # 该地址当前长期超时/断连，换成稳定的科技资讯源。
    "https://www.geekpark.net/rss": "https://www.infoq.cn/feed",
}


RSS_SOURCE_SEEDS_TSV = r"""
OpenAI 博客	https://openai.com/news/rss.xml	科技/AI	1	官方 RSS	GPT 官方动态
Google DeepMind	https://deepmind.google/blog/rss.xml	科技/AI	1	官方 RSS	DeepMind 研究
Google AI Blog	https://blog.google/technology/ai/rss/	科技/AI	1	官方 RSS	Google AI 综合动态
arXiv AI	https://rss.arxiv.org/rss/cs.AI	学术	1	官方 RSS	AI 论文预印本
arXiv 机器学习	https://rss.arxiv.org/rss/cs.LG	学术	1	官方 RSS	机器学习论文
arXiv NLP	https://rss.arxiv.org/rss/cs.CL	学术	1	官方 RSS	自然语言处理
arXiv 计算机视觉	https://rss.arxiv.org/rss/cs.CV	学术	1	官方 RSS	CV 论文预印本
Hacker News AI	https://hnrss.org/newest?q=AI	科技/AI	1	官方 RSS	HN AI 相关
Hacker News LLM	https://hnrss.org/newest?q=LLM	科技/AI	1	官方 RSS	HN 大模型相关
Hugging Face 博客	https://huggingface.co/blog/feed.xml	科技/AI	1	官方 RSS	开源 AI 社区
Stability AI	https://stability.ai/news?format=rss	科技/AI	1	官方 RSS	Stable Diffusion
机器之心	https://www.jiqizhixin.com/rss	科技/AI	1	官方 RSS	国内 AI 媒体
Simon Willison 博客	https://simonwillison.net/atom/everything/	科技/AI	1	官方 RSS	LLM 洞察
LinuxDo 最新话题	https://linux.do/latest.rss	科技/AI	1	官方 RSS	社区最新话题
LinuxDo 热门话题	https://linux.do/top.rss	科技/AI	1	官方 RSS	热门讨论
LinuxDo 最新帖子	https://linux.do/posts.rss	科技/AI	1	官方 RSS	所有新帖
V2EX 最热主题	https://www.v2ex.com/feed/tab/hot.xml	科技/AI	1	官方 RSS	今日热门
V2EX 最新主题	https://www.v2ex.com/feed/tab/all.xml	科技/AI	1	官方 RSS	全站最新
V2EX 技术节点	https://www.v2ex.com/feed/tab/tech.xml	科技/AI	1	官方 RSS	技术讨论
V2EX 创意节点	https://www.v2ex.com/feed/tab/creative.xml	其他	1	官方 RSS	创意分享
V2EX 好玩节点	https://www.v2ex.com/feed/tab/play.xml	其他	1	官方 RSS	好玩内容
Hacker News 首页	https://hnrss.org/frontpage	科技/AI	1	官方 RSS	首页热门
Hacker News 最新	https://hnrss.org/newest	科技/AI	1	官方 RSS	最新提交
Hacker News 最佳	https://hnrss.org/best	科技/AI	1	官方 RSS	最佳文章
Hacker News Ask	https://hnrss.org/ask	科技/AI	1	官方 RSS	问答帖
Hacker News Show	https://hnrss.org/show	科技/AI	1	官方 RSS	项目展示
GitHub 仓库 Release	https://github.com/用户名/仓库名/releases.atom	科技/AI	0	官方 RSS 模板	版本发布，需替换用户名和仓库名
GitHub 仓库 Commits	https://github.com/用户名/仓库名/commits.atom	科技/AI	0	官方 RSS 模板	提交记录，需替换用户名和仓库名
GitHub 仓库 Tags	https://github.com/用户名/仓库名/tags.atom	科技/AI	0	官方 RSS 模板	标签更新，需替换用户名和仓库名
少数派	https://sspai.com/feed	科技/AI	1	官方 RSS	首页文章
阮一峰科技爱好者周刊	https://www.ruanyifeng.com/blog/atom.xml	科技/AI	1	官方 RSS	技术周刊
IT之家	https://www.ithome.com/rss/	科技/AI	1	官方 RSS	IT 资讯全文
TechCrunch	https://techcrunch.com/feed/	科技/AI	1	官方 RSS	硅谷科技新闻
The Verge	https://www.theverge.com/rss/index.xml	科技/AI	1	官方 RSS	科技与文化
Wired	https://www.wired.com/feed/rss	科技/AI	1	官方 RSS	连线杂志
Ars Technica	https://feeds.arstechnica.com/arstechnica/index	科技/AI	1	官方 RSS	深度技术分析
MIT Technology Review	https://www.technologyreview.com/feed/	科技/AI	1	官方 RSS	麻省理工科技评论
Krebs on Security	https://krebsonsecurity.com/feed/	科技/AI	1	官方 RSS	安全博客
The Hacker News	https://feeds.feedburner.com/TheHackersNews	科技/AI	1	官方 RSS	黑客新闻
Schneier on Security	https://www.schneier.com/feed/	科技/AI	1	官方 RSS	安全专家博客
CISA News	https://www.cisa.gov/news.xml	科技/AI	1	官方 RSS	美国网络安全预警
Google Security Blog	https://security.googleblog.com/atom.xml	科技/AI	1	官方 RSS	Google 安全更新
FreeBuf	https://www.freebuf.com/feed	科技/AI	1	官方 RSS	国内安全资讯
安全客	https://api.anquanke.com/data/v1/rss	科技/AI	1	官方 RSS	安全技术资讯
Smashing Magazine	https://www.smashingmagazine.com/feed/	科技/AI	1	官方 RSS	前端设计杂志
A List Apart	https://alistapart.com/main/feed/	科技/AI	1	官方 RSS	Web 标准与设计
Codrops	https://tympanus.net/codrops/feed/	科技/AI	1	官方 RSS	创意前端效果
CSS-Tricks	https://css-tricks.com/feed/	科技/AI	1	官方 RSS	CSS 技巧教程
Astro Blog	https://astro.build/rss.xml	科技/AI	1	官方 RSS	Astro 框架动态
Svelte Blog	https://svelte.dev/blog/rss.xml	科技/AI	1	官方 RSS	Svelte 更新
Next.js Blog	https://nextjs.org/feed.xml	科技/AI	1	官方 RSS	Next.js 官方动态
Nuxt Blog	https://nuxt.com/blog/rss.xml	科技/AI	1	官方 RSS	Nuxt 框架更新
Tailwind CSS Blog	https://tailwindcss.com/feeds/feed.xml	科技/AI	1	官方 RSS	Tailwind CSS 更新
Dev.to	https://dev.to/feed	科技/AI	1	官方 RSS	开发者社区
Chrome Developer Blog	https://developer.chrome.com/blog/feed.xml	科技/AI	1	官方 RSS	Chrome 开发博客
Dribbble Popular	https://dribbble.com/shots/popular.rss	其他	1	官方 RSS	设计作品精选
Product Hunt	https://www.producthunt.com/feed	商业	1	官方 RSS	新产品发现
React Blog	https://react.dev/rss.xml	科技/AI	1	官方 RSS	React 官方博客
Vue Blog	https://blog.vuejs.org/feed.rss	科技/AI	1	官方 RSS	Vue 官方博客
Rust Blog	https://blog.rust-lang.org/feed.xml	科技/AI	1	官方 RSS	Rust 官方博客
Go Blog	https://go.dev/blog/feed.atom	科技/AI	1	官方 RSS	Go 官方博客
Python Blog	https://blog.python.org/feeds/posts/default	科技/AI	1	官方 RSS	Python 官方博客
Node.js Blog	https://nodejs.org/en/feed/blog.xml	科技/AI	1	官方 RSS	Node.js 官方博客
Deno Blog	https://deno.com/blog/feed.xml	科技/AI	1	官方 RSS	Deno 官方博客
TypeScript Blog	https://devblogs.microsoft.com/typescript/feed/	科技/AI	1	官方 RSS	TypeScript 官方博客
Swift Blog	https://www.swift.org/atom.xml	科技/AI	1	官方 RSS	Swift 官方博客
Kotlin Blog	https://blog.jetbrains.com/kotlin/feed/	科技/AI	1	官方 RSS	Kotlin 官方博客
GitHub Blog	https://github.blog/feed/	科技/AI	1	官方 RSS	GitHub 官方博客
Netflix Tech Blog	https://netflixtechblog.com/feed	科技/AI	1	官方 RSS	Netflix 技术博客
AWS Blog	https://aws.amazon.com/blogs/aws/feed/	科技/AI	1	官方 RSS	AWS 官方博客
Cloudflare Blog	https://blog.cloudflare.com/rss/	科技/AI	1	官方 RSS	Cloudflare 技术博客
Google Developers	https://developers.googleblog.com/feeds/posts/default/	科技/AI	1	官方 RSS	Google 开发者博客
Mozilla Hacks	https://hacks.mozilla.org/feed/	科技/AI	1	官方 RSS	Mozilla 开发者博客
Vercel Blog	https://vercel.com/atom	科技/AI	1	官方 RSS	Vercel 官方博客
Supabase Blog	https://supabase.com/rss.xml	科技/AI	1	官方 RSS	Supabase 官方博客
Stripe Blog	https://stripe.com/blog/feed.rss	商业	1	官方 RSS	Stripe 技术博客
Spotify Engineering	https://engineering.atspotify.com/feed/	科技/AI	1	官方 RSS	Spotify 技术博客
Meta Engineering	https://engineering.fb.com/feed/	科技/AI	1	官方 RSS	Meta 技术博客
JavaScript Weekly	https://javascriptweekly.com/rss/	科技/AI	1	官方 RSS	JS 生态精选周刊
This Week in Rust	https://this-week-in-rust.org/atom.xml	科技/AI	1	官方 RSS	Rust 社区周报
Golang Weekly	https://golangweekly.com/rss/	科技/AI	1	官方 RSS	Go 生态精选周刊
ByteByteGo	https://blog.bytebytego.com/feed	科技/AI	1	官方 RSS	系统设计 Newsletter
Nature	https://www.nature.com/nature.rss	学术	1	官方 RSS	Nature 期刊
RSSHub Releases	https://github.com/DIYgod/RSSHub/releases.atom	科技/AI	1	官方 RSS	RSSHub 版本更新
RSSHub Radar Releases	https://github.com/DIYgod/RSSHub-Radar/releases.atom	科技/AI	1	官方 RSS	Radar 扩展更新
Fluent Reader Releases	https://github.com/yang991178/fluent-reader/releases.atom	科技/AI	1	官方 RSS	跨平台阅读器
NetNewsWire Releases	https://github.com/Ranchero-Software/NetNewsWire/releases.atom	科技/AI	1	官方 RSS	macOS/iOS 阅读器
FreshRSS Releases	https://github.com/FreshRSS/FreshRSS/releases.atom	科技/AI	1	官方 RSS	自建 RSS 服务
微博热搜	https://rsshub.app/weibo/search/hot	其他	1	RSSHub	实时热搜榜
用户微博	https://rsshub.app/weibo/user/:uid	其他	0	RSSHub 模板	指定用户微博，需替换 uid
知乎热榜	https://rsshub.app/zhihu/hot	其他	1	RSSHub	热门话题
知乎用户动态	https://rsshub.app/zhihu/people/activities/:id	其他	0	RSSHub 模板	用户动态，需替换 id
知乎专栏文章	https://rsshub.app/zhihu/zhuanlan/:id	其他	0	RSSHub 模板	专栏更新，需替换 id
抖音热搜榜	https://rsshub.app/douyin/hot	其他	1	RSSHub	抖音热搜
小红书用户笔记	https://rsshub.app/xiaohongshu/user/:user_id/notes	其他	0	RSSHub 模板	用户笔记动态，需替换 user_id
小红书用户收藏	https://rsshub.app/xiaohongshu/user/:user_id/collect	其他	0	RSSHub 模板	用户收藏内容，需替换 user_id
Telegram 频道消息	https://rsshub.app/telegram/channel/:username	其他	0	RSSHub 模板	公开频道更新，需替换 username
GitHub Trending 每日	https://rsshub.app/github/trending/daily	科技/AI	1	RSSHub	每日热门项目
GitHub Trending 每周	https://rsshub.app/github/trending/weekly	科技/AI	1	RSSHub	每周热门项目
GitHub Trending 语言	https://rsshub.app/github/trending/daily/:language	科技/AI	0	RSSHub 模板	指定语言热门，需替换 language
掘金全站热榜	https://rsshub.app/juejin/trending/all/weekly	科技/AI	1	RSSHub	本周热门
掘金前端热榜	https://rsshub.app/juejin/trending/frontend/weekly	科技/AI	1	RSSHub	前端热门
掘金后端热榜	https://rsshub.app/juejin/trending/backend/weekly	科技/AI	1	RSSHub	后端热门
CSDN 博客热榜	https://rsshub.app/csdn/blog	科技/AI	1	RSSHub	热门博客
今日头条	https://rsshub.app/toutiao/hot	其他	1	RSSHub	头条热榜
百度热搜	https://rsshub.app/baidu/hot	其他	1	RSSHub	百度热点
36氪快讯	https://rsshub.app/36kr/newsflash	商业	1	RSSHub	科技快讯
Bilibili UP主视频	https://rsshub.app/bilibili/user/video/:uid	B站	0	RSSHub 模板	UP 主更新，需替换 uid
Bilibili 排行榜	https://rsshub.app/bilibili/ranking/0/3/1	B站	1	RSSHub	全站热门
豆瓣电影正在热映	https://rsshub.app/douban/movie/playing	其他	1	RSSHub	院线热映
豆瓣电影即将上映	https://rsshub.app/douban/movie/later	其他	1	RSSHub	待映电影
什么值得买数码好价	https://rsshub.app/smzdm/ranking/pinlei/11	其他	1	RSSHub	数码产品
什么值得买电脑配件	https://rsshub.app/smzdm/ranking/pinlei/12	其他	1	RSSHub	电脑外设
什么值得买关键词	https://rsshub.app/smzdm/keyword/:keyword	其他	0	RSSHub 模板	关键词好价，需替换 keyword
"""


def _ensure():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)


@contextmanager
def _conn():
    _ensure()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_config():
    """建 config 表，填入缺失的默认值"""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS rss_sources (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                url         TEXT UNIQUE NOT NULL,
                category    TEXT DEFAULT '其他',
                enabled     INTEGER DEFAULT 1,
                source_type TEXT DEFAULT '自定义',
                note        TEXT DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_rss_sources_enabled ON rss_sources(enabled)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_rss_sources_cat ON rss_sources(category)")
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO config (key,value) VALUES (?,?)", (k, v))
        _migrate_rss_feeds(c)
        _migrate_rss_sources(c)
        _ensure_rss_source_table(c)


def _migrate_rss_feeds(conn):
    row = conn.execute("SELECT value FROM config WHERE key='rss.feeds'").fetchone()
    if not row:
        return
    try:
        feeds = json.loads(row["value"])
    except json.JSONDecodeError:
        return
    if not isinstance(feeds, list):
        return
    migrated = []
    changed = False
    for feed in feeds:
        replacement = RSS_FEED_MIGRATIONS.get(feed, feed)
        if replacement != feed:
            changed = True
        if replacement not in migrated:
            migrated.append(replacement)
    if changed:
        conn.execute(
            "UPDATE config SET value=? WHERE key='rss.feeds'",
            (json.dumps(migrated, ensure_ascii=False),),
        )


def _source_name_from_url(url: str) -> str:
    host = url.split("//", 1)[-1].split("/", 1)[0].replace("www.", "")
    return host or "RSS 源"


def _normalize_rss_source(item, default_category="其他") -> dict | None:
    if isinstance(item, str):
        url = item.strip()
        if not url:
            return None
        return {
            "name": _source_name_from_url(url), "url": url,
            "category": default_category, "enabled": True,
            "source_type": "自定义", "note": "",
        }
    if not isinstance(item, dict):
        return None
    url = str(item.get("url", "")).strip()
    if not url:
        return None
    return {
        "name": str(item.get("name") or _source_name_from_url(url)).strip(),
        "url": url,
        "category": str(item.get("category") or default_category).strip(),
        "enabled": bool(item.get("enabled", True)),
        "source_type": str(item.get("source_type") or item.get("type") or "自定义").strip(),
        "note": str(item.get("note") or "").strip(),
    }


def _seed_rss_sources() -> list:
    sources = []
    for raw in RSS_SOURCE_SEEDS_TSV.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        name, url, category, enabled, source_type, note = parts[:6]
        sources.append({
            "name": name.strip(),
            "url": url.strip(),
            "category": category.strip() or "其他",
            "enabled": enabled.strip() == "1",
            "source_type": source_type.strip() or "自定义",
            "note": note.strip(),
        })
    return sources


def _upsert_rss_source(conn, source: dict, overwrite: bool = False):
    source = _normalize_rss_source(source)
    if not source:
        return
    now = datetime.now().isoformat()
    if overwrite:
        conn.execute("""
            INSERT INTO rss_sources (name,url,category,enabled,source_type,note,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET
                name=excluded.name,
                category=excluded.category,
                enabled=excluded.enabled,
                source_type=excluded.source_type,
                note=excluded.note,
                updated_at=excluded.updated_at
        """, (
            source["name"], source["url"], source["category"], int(source["enabled"]),
            source["source_type"], source["note"], now, now,
        ))
    else:
        conn.execute("""
            INSERT OR IGNORE INTO rss_sources
            (name,url,category,enabled,source_type,note,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            source["name"], source["url"], source["category"], int(source["enabled"]),
            source["source_type"], source["note"], now, now,
        ))


def _sync_rss_config_from_table(conn):
    rows = conn.execute("""
        SELECT name,url,category,enabled,source_type,note
        FROM rss_sources ORDER BY id
    """).fetchall()
    sources = [
        {
            "name": r["name"], "url": r["url"], "category": r["category"],
            "enabled": bool(r["enabled"]), "source_type": r["source_type"], "note": r["note"],
        }
        for r in rows
    ]
    conn.execute(
        "INSERT OR REPLACE INTO config (key,value) VALUES ('rss.sources',?)",
        (json.dumps(sources, ensure_ascii=False),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO config (key,value) VALUES ('rss.feeds',?)",
        (json.dumps([s["url"] for s in sources if s["enabled"]], ensure_ascii=False),),
    )


def _ensure_rss_source_table(conn):
    for source in _seed_rss_sources():
        _upsert_rss_source(conn, source, overwrite=False)

    row = conn.execute("SELECT value FROM config WHERE key='rss.sources'").fetchone()
    try:
        config_sources = json.loads(row["value"]) if row else []
    except json.JSONDecodeError:
        config_sources = []
    if isinstance(config_sources, list):
        for source in config_sources:
            _upsert_rss_source(conn, source, overwrite=False)

    row = conn.execute("SELECT value FROM config WHERE key='rss.feeds'").fetchone()
    try:
        legacy_feeds = json.loads(row["value"]) if row else []
    except json.JSONDecodeError:
        legacy_feeds = []
    if isinstance(legacy_feeds, list):
        for feed in legacy_feeds:
            _upsert_rss_source(conn, feed, overwrite=False)

    _sync_rss_config_from_table(conn)


def _migrate_rss_sources(conn):
    row = conn.execute("SELECT value FROM config WHERE key='rss.sources'").fetchone()
    try:
        sources = json.loads(row["value"]) if row else []
    except json.JSONDecodeError:
        sources = []
    if not isinstance(sources, list):
        sources = []

    by_url = {}
    for item in sources:
        source = _normalize_rss_source(item)
        if source:
            by_url[source["url"]] = source

    row = conn.execute("SELECT value FROM config WHERE key='rss.feeds'").fetchone()
    try:
        legacy_feeds = json.loads(row["value"]) if row else []
    except json.JSONDecodeError:
        legacy_feeds = []
    if isinstance(legacy_feeds, list):
        for feed in legacy_feeds:
            url = str(feed).strip()
            if url and url not in by_url:
                by_url[url] = _normalize_rss_source(url)

    try:
        default_sources = json.loads(DEFAULTS["rss.sources"])
    except json.JSONDecodeError:
        default_sources = []
    for item in default_sources:
        source = _normalize_rss_source(item)
        if source and source["url"] not in by_url:
            by_url[source["url"]] = source

    sources = list(by_url.values())
    conn.execute(
        "INSERT OR REPLACE INTO config (key,value) VALUES ('rss.sources',?)",
        (json.dumps(sources, ensure_ascii=False),),
    )
    enabled_feeds = [s["url"] for s in sources if s.get("enabled", True)]
    conn.execute(
        "INSERT OR REPLACE INTO config (key,value) VALUES ('rss.feeds',?)",
        (json.dumps(enabled_feeds, ensure_ascii=False),),
    )


def get(key: str, fallback=None):
    """读一条配置，返回字符串。key 不存在返回 fallback。"""
    with _conn() as c:
        row = c.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else (DEFAULTS.get(key, fallback))


def get_int(key: str, fallback: int = 0) -> int:
    try:
        return int(get(key, str(fallback)))
    except (ValueError, TypeError):
        return fallback


def get_bool(key: str, fallback: bool = False) -> bool:
    return get(key, "true" if fallback else "false").lower() == "true"


def get_json(key: str, fallback=None):
    raw = get(key)
    if raw is None:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def set_config(key: str, value):
    """写一条配置。value 如果是 list/dict 会自动 JSON 序列化。"""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (key, str(value)))


put = set_config


def validate_config(key: str, value) -> tuple[bool, str]:
    """校验配置项合法性，返回 (是否合法, 错误信息)"""
    validators = {
        "web.port":      lambda v: (isinstance(v, (int, str)) and 1024 <= int(v) <= 65535, f"端口必须在 1024-65535 之间"),
        "llm.timeout":   lambda v: (isinstance(v, (int, str)) and 10 <= int(v) <= 300, f"超时必须在 10-300 秒之间"),
        "llm.max_retries": lambda v: (isinstance(v, (int, str)) and 0 <= int(v) <= 10, f"重试次数必须在 0-10 之间"),
        "feature.max_workers": lambda v: (isinstance(v, (int, str)) and 1 <= int(v) <= 20, f"并发数必须在 1-20 之间"),
    }
    if key in validators:
        try:
            return validators[key](value)
        except (ValueError, TypeError):
            return False, f"{key} 的值无效: {value}"
    return True, ""


def set_many(pairs: dict):
    """批量写入，带校验"""
    errors = []
    with _conn() as c:
        for k, v in pairs.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            ok, msg = validate_config(k, v)
            if not ok:
                errors.append(f"{k}: {msg}")
                continue
            c.execute("INSERT OR REPLACE INTO config (key,value) VALUES (?,?)", (k, str(v)))
    return errors  # 返回校验错误列表，空列表表示全部成功


def get_all() -> dict:
    """返回所有配置（用于 Settings 页面展示）"""
    with _conn() as c:
        rows = c.execute("SELECT key,value FROM config ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── 常用组合读取（让其他模块用起来方便）─────────────────

def get_llm_config() -> dict:
    active = get("llm.active", "deepseek")
    return {
        "active":   active,
        "base_url": get(f"llm.{active}.url", ""),
        "api_key":  get(f"llm.{active}.key", ""),
        "model":    get(f"llm.{active}.model", ""),
        "timeout":  get_int("llm.timeout", 60),
        "max_retries": get_int("llm.max_retries", 3),
        "retry_delay": get_int("llm.retry_delay", 2),
    }


def get_rss_feeds() -> list:
    sources = get_rss_sources()
    if sources:
        return [s["url"] for s in sources if s.get("enabled", True)]
    return get_json("rss.feeds", [])


def get_rss_sources(enabled_only: bool = False) -> list:
    try:
        with _conn() as c:
            where = "WHERE enabled=1" if enabled_only else ""
            rows = c.execute(f"""
                SELECT name,url,category,enabled,source_type,note
                FROM rss_sources {where}
                ORDER BY id
            """).fetchall()
        return [
            {
                "name": r["name"], "url": r["url"], "category": r["category"],
                "enabled": bool(r["enabled"]), "source_type": r["source_type"], "note": r["note"],
            }
            for r in rows
        ]
    except sqlite3.OperationalError:
        sources = get_json("rss.sources", [])
        if not isinstance(sources, list):
            sources = []
        normalized = []
        seen = builtins.set()
        for item in sources:
            source = _normalize_rss_source(item)
            if not source or source["url"] in seen:
                continue
            seen.add(source["url"])
            if enabled_only and not source.get("enabled", True):
                continue
            normalized.append(source)
        return normalized


def set_rss_sources(sources: list) -> list:
    normalized = []
    seen = builtins.set()
    for item in sources:
        source = _normalize_rss_source(item)
        if not source or source["url"] in seen:
            continue
        seen.add(source["url"])
        normalized.append(source)
    with _conn() as c:
        c.execute("DELETE FROM rss_sources")
        for source in normalized:
            _upsert_rss_source(c, source, overwrite=True)
        _sync_rss_config_from_table(c)
    return normalized


def get_preferences() -> dict:
    return {
        "boost":      get_json("pref.boost", {}),
        "penalty":    get_json("pref.penalty", {}),
        "muted":      get_json("pref.muted", []),
        "cat_weight": get_json("pref.cat_weight", {}),
    }
