# ============================================================
# fetchers/fetch.py —— 内容抓取（RSS 源从 config_store 读）
# ============================================================

import re, time, logging, feedparser, requests
from bs4 import BeautifulSoup
try:
    from readability import Document
    HAS_READABILITY = True
except ImportError:
    HAS_READABILITY = False

import config_store as cfg
from storage import record_rss_fetch

logger = logging.getLogger("fetcher")

if not HAS_READABILITY:
    logger.warning("readability-lxml 未安装，网页提取质量可能降低。建议: pip install readability-lxml")

# ── 请求限速 ─────────────────────────────────────────────
_last_request_time = 0.0
_REQUEST_INTERVAL = 1.0  # 秒，两次请求之间的最小间隔


def _throttle():
    """简单限速，避免对同一站点过快请求"""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < _REQUEST_INTERVAL:
        time.sleep(_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.time()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

SITE_RULES = [
    (r"mp\.weixin\.qq\.com",            "wechat"),
    (r"juejin\.(cn|im)",                "juejin"),
    (r"36kr\.com",                      "36kr"),
    (r"bilibili\.com/read",             "bilibili"),
]

# ── 语言检测 ─────────────────────────────────────────────

def detect_language(text: str) -> str:
    if not text: return "未知"
    sample = text[:2000]
    zh = sum(1 for c in sample if "\u4e00" <= c <= "\u9fff")
    al = sum(1 for c in sample if c.isalpha() or "\u4e00" <= c <= "\u9fff")
    if al == 0: return "未知"
    return "中文" if zh / al > 0.3 else "英文"


# ── 通用网页抓取 ─────────────────────────────────────────

def _extract_text(html: str) -> str:
    """从 HTML 提取正文，优先 readability"""
    if HAS_READABILITY:
        try:
            doc  = Document(html)
            soup = BeautifulSoup(doc.summary(), "html.parser")
            text = soup.get_text("\n", strip=True)
            if len(text) > 200:
                return text
        except Exception:
            pass
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script","style","nav","footer","header","aside"]):
        t.decompose()
    paras = [p.get_text(strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 20]
    return "\n".join(paras) or soup.get_text("\n", strip=True)


def fetch_url(url: str) -> dict:
    """通用 URL 抓取，自动路由到站点专用规则"""
    for pattern, site in SITE_RULES:
        if re.search(pattern, url):
            return _fetch_site(url, site)
    return _fetch_generic(url)


def _fetch_generic(url: str) -> dict:
    _throttle()
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = r.apparent_encoding

        # 检查 HTTP 状态码
        if r.status_code == 404:
            return {"title": "页面不存在", "text": "", "url": url,
                    "error": "404 页面不存在", "language": "未知"}
        if r.status_code == 403:
            return {"title": "抓取失败", "text": "", "url": url,
                    "error": "403 拒绝访问，可能需要登录或触发站点反爬", "language": "未知"}
        if r.status_code >= 400:
            return {"title": "抓取失败", "text": "", "url": url,
                    "error": f"HTTP {r.status_code}", "language": "未知"}

        text = _extract_text(r.text)

        # 内容过短检测（可能是 404 页面、登录页、空内容等）
        if len(text.strip()) < 100:
            return {"title": "内容不足", "text": "", "url": url,
                    "error": f"提取到的正文过短（{len(text.strip())}字），可能是404页面或需要登录",
                    "language": "未知"}

        # 获取标题
        soup  = BeautifulSoup(r.text, "html.parser")
        title = soup.title.string.strip() if soup.title else url[:60]
        return {"title": title, "text": text[:10000], "url": url,
                "language": detect_language(text)}
    except Exception as e:
        return {"title":"抓取失败","text":"","url":url,"error":str(e),"language":"未知"}


def _fetch_site(url: str, site: str) -> dict:
    """站点专用抓取规则，支持RSS回退"""
    _throttle()
    SELECTORS = {
        "wechat":   [("div","js_content"), ("div","rich_media_content")],
        "juejin":   [("div","markdown-body"), ("article", None)],
        "36kr":     [("div","articleDetailContent"), ("article", None)],
        "bilibili": [("div","article-holder"), ("div","read-article-holder")],
    }
    TITLE_SELS = {
        "wechat":   [("h1","rich_media_title"), ("h2","activity-name")],
        "juejin":   [("h1","article-title"), ("h1", None)],
        "36kr":     [("h1","article-title"), ("h1", None)],
        "bilibili": [("h1","title"), ("h1", None)],
    }
    # 部分站点支持RSS回退，当URL抓取正文不足时可从RSS获取
    RSS_FALLBACK = {
        "36kr": "https://36kr.com/feed",
    }
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = r.apparent_encoding or "utf-8"

        # 检查 HTTP 状态码
        if r.status_code == 404:
            return {"title": "页面不存在", "text": "", "url": url,
                    "error": "404 页面不存在", "language": "未知"}
        if r.status_code == 403:
            return {"title": "抓取失败", "text": "", "url": url,
                    "error": "403 拒绝访问，可能需要登录或触发站点反爬", "language": "未知"}
        if r.status_code >= 400:
            return {"title": "抓取失败", "text": "", "url": url,
                    "error": f"HTTP {r.status_code}", "language": "未知"}

        soup = BeautifulSoup(r.text, "html.parser")

        # 标题
        title = ""
        for tag, cls in TITLE_SELS.get(site, []):
            el = soup.find(tag, class_=cls) if cls else soup.find(tag)
            if el:
                title = el.get_text(strip=True); break
        if not title and soup.title:
            title = soup.title.string.strip()

        # 正文
        text = ""
        for tag, cls in SELECTORS.get(site, []):
            el = soup.find(tag, id=cls) or (soup.find(tag, class_=cls) if cls else None)
            if el:
                for s in el(["script","style"]): s.decompose()
                text = el.get_text("\n", strip=True); break
        if not text:
            text = _extract_text(r.text)

        # 内容过短检测 — 尝试RSS回退
        if len(text.strip()) < 100:
            rss_url = RSS_FALLBACK.get(site)
            if rss_url:
                logger.info(f"{site} 正文过短（{len(text.strip())}字），尝试RSS回退: {rss_url}")
                rss_result = _rss_fallback(url, rss_url, site)
                if rss_result:
                    return rss_result
            return {"title": title or "内容不足", "text": "", "url": url,
                    "error": f"提取到的正文过短（{len(text.strip())}字），可能是404页面或需要登录",
                    "language": "未知"}

        source_map = {
            "wechat":"微信公众号","juejin":"掘金",
            "36kr":"36氪","bilibili":"B站专栏"
        }
        return {
            "title": title or "无标题",
            "text":  text[:10000],
            "url":   url,
            "source": source_map.get(site, site),
            "language": detect_language(text),
        }
    except Exception as e:
        return {"title":"抓取失败","text":"","url":url,"error":str(e),"language":"未知"}


def _rss_fallback(article_url: str, rss_feed_url: str, site: str) -> dict | None:
    """当URL直接抓取正文不足时，从RSS feed中查找对应文章获取正文"""
    try:
        # 从文章URL中提取文章ID用于匹配
        match = re.search(r'/p/(\d+)', article_url)
        if not match:
            return None
        article_id = match.group(1)

        _throttle()
        # RSS请求使用更合适的请求头
        rss_headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/rss+xml,application/xml,text/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        r = requests.get(rss_feed_url, headers=rss_headers, timeout=15)
        if r.status_code != 200:
            return None
        # 检查返回内容是否真的是RSS/XML，而不是WAF验证码页面
        content_type = r.headers.get("Content-Type", "")
        if "html" in content_type and "xml" not in content_type:
            logger.info(f"RSS回退: 返回了HTML（可能是WAF验证页），跳过")
            return None
        if len(r.content) < 500:
            logger.info(f"RSS回退: 内容过短（{len(r.content)}字节），跳过")
            return None

        feed = feedparser.parse(r.content)
        if not feed.entries:
            return None
        for entry in feed.entries:
            # 匹配RSS条目中的文章ID
            entry_url = entry.get("link", "")
            if article_id in entry_url:
                # RSS <description> 中通常包含HTML格式的正文
                desc = entry.get("description", "")
                if desc:
                    soup = BeautifulSoup(desc, "html.parser")
                    text = soup.get_text("\n", strip=True)
                else:
                    text = entry.get("summary", "")
                if len(text.strip()) >= 100:
                    source_map = {
                        "36kr":"36氪",
                    }
                    return {
                        "title": entry.get("title", "无标题"),
                        "text": text[:10000],
                        "url": article_url,
                        "source": source_map.get(site, site),
                        "language": detect_language(text),
                    }
        logger.info(f"RSS回退: 在RSS feed中未找到文章ID {article_id}")
        return None
    except Exception as e:
        logger.warning(f"RSS回退失败: {e}")
        return None


# ── 文本粘贴 ─────────────────────────────────────────────

def from_text(text: str, title: str = "手动输入", url: str = "", source: str = "") -> dict:
    return {
        "title": title, "text": text.strip(),
        "url": url, "source": source,
        "language": detect_language(text),
    }


# ── RSS ─────────────────────────────────────────────────

def fetch_rss_feed(feed_url: str, max_items: int = 5) -> list:
    """拉取单个 RSS 源，带健康状态记录"""
    _throttle()
    try:
        r = requests.get(feed_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            record_rss_fetch(feed_url, False, error=f"HTTP {r.status_code}")
            return []
        feed = feedparser.parse(r.content)
        if not feed.entries:
            content_type = r.headers.get("content-type", "").lower()
            sample = (r.content or b"")[:200].lstrip().lower()
            if "html" in content_type or sample.startswith(b"<!doctype html") or sample.startswith(b"<html"):
                error = "返回 HTML，非 RSS/Atom 内容"
            elif getattr(feed, "bozo", 0):
                error = f"RSS 解析失败：{getattr(feed, 'bozo_exception', '')}"
            else:
                error = "RSS/Atom 未包含文章条目"
            record_rss_fetch(feed_url, False, error=error)
            return []
        articles = []
        for entry in feed.entries[:max_items]:
            html = ""
            if hasattr(entry,"content") and entry.content:
                html = entry.content[0].value
            elif hasattr(entry,"summary"):
                html = entry.summary
            text = BeautifulSoup(html, "html.parser").get_text(strip=True)
            # 如果摘要太短，尝试抓全文
            if len(text) < 200 and entry.get("link"):
                full = _fetch_generic(entry["link"])
                if len(full.get("text","")) > len(text):
                    text = full["text"]
            articles.append({
                "title": entry.get("title","无标题"),
                "text": text[:8000],
                "url": entry.get("link",""),
                "language": detect_language(text),
            })
        record_rss_fetch(feed_url, True, item_count=len(articles))
        return articles
    except Exception as e:
        record_rss_fetch(feed_url, False, error=str(e))
        return []


def fetch_all_rss() -> list:
    """读 config_store 里的 RSS 源列表，批量拉取"""
    sources = cfg.get_rss_sources(enabled_only=True)
    max_per = cfg.get_int("rss.max_per_feed", 5)
    all_articles = []
    for source in sources:
        url = source["url"]
        arts = fetch_rss_feed(url, max_items=max_per)
        for art in arts:
            art.setdefault("source", source.get("name", "RSS"))
            art["rss_category"] = source.get("category", "其他")
            art["category"] = art["rss_category"]
        print(f"  {'✓' if arts else '✗'} RSS {source.get('name','RSS')} {url[:60]} ({len(arts)}篇)")
        all_articles.extend(arts)
    return all_articles


# ── 邮件 ─────────────────────────────────────────────────

def fetch_emails() -> list:
    """IMAP 拉取 Newsletter，配置来自 config_store"""
    import imaplib, email as emaillib
    from email.header import decode_header

    ec = {
        "enabled":     cfg.get_bool("email.enabled"),
        "imap_server": cfg.get("email.imap_server",""),
        "username":    cfg.get("email.username",""),
        "password":    cfg.get("email.password",""),
        "label":       cfg.get("email.label","INBOX"),
        "limit":       cfg.get_int("email.limit", 10),
    }
    if not ec["enabled"]:
        return []

    def _decode(s):
        if not s: return ""
        parts = decode_header(s)
        out = []
        for content, enc in parts:
            if isinstance(content, bytes):
                out.append(content.decode(enc or "utf-8", errors="ignore"))
            else:
                out.append(content or "")
        return "".join(out)

    def _body(msg):
        html, plain = "", ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                disp = str(part.get("Content-Disposition",""))
                if "attachment" in disp: continue
                try:
                    payload = part.get_payload(decode=True)
                    if not payload: continue
                    cs = part.get_content_charset() or "utf-8"
                    content = payload.decode(cs, errors="ignore")
                    if ct == "text/html": html += content
                    elif ct == "text/plain": plain += content
                except Exception: pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    cs = msg.get_content_charset() or "utf-8"
                    content = payload.decode(cs, errors="ignore")
                    if msg.get_content_type() == "text/html": html = content
                    else: plain = content
            except Exception: pass
        if html:
            soup = BeautifulSoup(html, "html.parser")
            for t in soup(["script","style","img"]): t.decompose()
            return soup.get_text("\n", strip=True)
        return plain

    try:
        mail = imaplib.IMAP4_SSL(ec["imap_server"])
        mail.login(ec["username"], ec["password"])
        folder = f'"{ec["label"]}"' if "gmail" in ec["imap_server"].lower() else ec["label"]
        status, _ = mail.select(folder)
        if status != "OK":
            mail.select("INBOX")
        _, data = mail.search(None, "UNSEEN")
        if not data[0]:
            return []
        ids = data[0].split()[-ec["limit"]:]
        emails = []
        for mid in ids:
            try:
                _, mdata = mail.fetch(mid, "(BODY.PEEK[])")
                msg = emaillib.message_from_bytes(mdata[0][1])
                body = _body(msg)
                if len(body) < 100: continue
                emails.append({
                    "title": _decode(msg.get("Subject","")) or "无标题",
                    "text":  body[:8000],
                    "url": "",
                    "source": f"Newsletter/{_decode(msg.get('From',''))[:40]}",
                    "language": detect_language(body),
                })
                mail.store(mid, "+FLAGS", "\\Seen")
            except Exception: continue
        mail.close(); mail.logout()
        return emails
    except Exception as e:
        raise RuntimeError(f"邮件拉取失败：{e}")
