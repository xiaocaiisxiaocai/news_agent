# ============================================================
# web/app.py —— 完整 Web 控制台
# 启动：python web/app.py
# 访问：http://127.0.0.1:5000
# ============================================================

import os, sys, threading, uuid, logging, time, re, json, smtplib, csv, io
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import schedule
import requests
from flask import Flask, render_template, jsonify, request, send_from_directory, abort, redirect, url_for, Response
from urllib.parse import urlparse

import config_store as cfg
from storage import (
    init_db, get_stats, get_articles, get_article, save_article,
    get_eval_stats, get_rss_health, get_active_topics, record_rss_fetch,
    count_articles, cache_stats, cache_clear, record_article_event,
    get_recommended_articles, get_article_event_counts, find_similar_articles,
    explain_article_recommendation, get_preference_profile,
    save_notification_channel, get_notification_channels, get_notification_channel,
    delete_notification_channel, record_notification_log, has_successful_notification,
    get_notification_logs, get_notification_stats, record_preference_tuning,
    get_preference_tuning_logs, get_recommendation_effectiveness,
)
from fetchers.fetch import fetch_url, from_text, fetch_all_rss, fetch_emails, fetch_rss_feed
from processors.batch import batch_process, process_one
from processors.topic import compute_trending, find_related
from outputs.brief import generate as gen_brief, generate_html as gen_brief_html, save_to_file as save_brief

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_ENSURE_ASCII"] = False
app.secret_key = "dev-secret-key-change-in-production"  # 运行时会被 run() 覆盖

logger = logging.getLogger("web")

# ── 项目根目录 ─────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BRIEFS_DIR = os.path.join(_PROJECT_ROOT, "briefs")

# ── 任务状态 ──────────────────────────────────────────────

_jobs: dict = {}
_scheduler_started = False


def new_job(kind: str) -> str:
    jid = uuid.uuid4().hex[:10]
    _jobs[jid] = {"id": jid, "kind": kind, "status": "running",
                  "progress": 0, "total": 0, "logs": [], "result": None,
                  "started_at": datetime.now().isoformat()}
    # 只保留最近 50 个任务
    if len(_jobs) > 50:
        oldest = sorted(_jobs, key=lambda k: _jobs[k]["started_at"])[:10]
        for k in oldest:
            del _jobs[k]
    return jid


def job_log(jid, msg):
    if jid in _jobs:
        _jobs[jid]["logs"].append({"t": datetime.now().strftime("%H:%M:%S"), "m": msg})


def job_done(jid, result=None):
    if jid in _jobs:
        _jobs[jid].update({"status": "done", "result": result,
                           "finished_at": datetime.now().isoformat()})


def job_err(jid, err):
    if jid in _jobs:
        _jobs[jid].update({"status": "error", "result": {"error": str(err)}})


def make_on_complete(jid):
    def cb(article):
        _jobs[jid]["progress"] = _jobs[jid].get("progress", 0) + 1
        if article.get("skipped"):
            reason = article.get("skip_reason", "")
            job_log(jid, f"⊘ 跳过（{reason}）：{article.get('title','')[:40]}")
            return
        if article.get("error"):
            job_log(jid, f"✗ 失败：{article.get('title','')[:40]}")
            return
        save_article(article)
        imp = article.get("importance", 3)
        cat = article.get("category", "?")
        job_log(jid, f"✓ [{cat}|★{imp}] {article.get('title','')[:40]}")
    return cb


def _run_rss_job(jid):
    try:
        job_log(jid, "拉取 RSS 订阅源...")
        arts = fetch_all_rss()
        if not arts:
            job_log(jid, "没有获取到文章，请检查 RSS 源配置")
            job_done(jid, {"total": 0})
            return
        _jobs[jid]["total"] = len(arts)
        job_log(jid, f"共 {len(arts)} 篇，开始处理（并发={cfg.get_int('feature.max_workers',5)}）")
        stats = batch_process(arts, on_complete=make_on_complete(jid))
        job_done(jid, stats)
    except Exception as e:
        job_log(jid, f"✗ {e}")
        job_err(jid, e)


def _run_brief_job(jid, fmt="md"):
    try:
        job_log(jid, "生成简报...")
        content = gen_brief_html() if fmt == "html" else gen_brief()
        path = save_brief(content, fmt=fmt)
        job_log(jid, f"✓ 已保存：{path}")
        job_done(jid, {"path": path, "content": content, "format": fmt})
    except Exception as e:
        job_log(jid, f"✗ {e}")
        job_err(jid, e)


def _run_rss_test_job(jid):
    try:
        sources = cfg.get_rss_sources()
        targets = [s for s in sources if s.get("enabled", True)]
        skipped = len(sources) - len(targets)
        _jobs[jid]["total"] = len(targets)
        job_log(jid, f"测试 RSS 源：启用 {len(targets)} 个，跳过关闭 {skipped} 个")
        ok = failed = 0

        def test_one(source):
            url = source["url"]
            name = source.get("name") or url
            try:
                arts = fetch_rss_feed(url, max_items=3)
            except Exception as e:
                record_rss_fetch(url, False, error=str(e))
                return False, name, 0
            if arts:
                record_rss_fetch(url, True, item_count=len(arts))
                return True, name, len(arts)
            # fetch_rss_feed 已写入真实错误；这里只在异常 mock 或异常路径没有记录时兜底。
            if not any(h["feed_url"] == url for h in get_rss_health()):
                record_rss_fetch(url, False, error="RSS 测试未获取到文章")
            return False, name, 0

        workers = min(10, max(1, cfg.get_int("feature.max_workers", 5)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(test_one, source) for source in targets]
            for future in as_completed(futures):
                success, name, count = future.result()
                if success:
                    ok += 1
                    job_log(jid, f"✓ {name}（{count} 篇）")
                else:
                    failed += 1
                    job_log(jid, f"✗ {name}")
                _jobs[jid]["progress"] += 1
        job_done(jid, {"ok": ok, "failed": failed, "skipped": skipped, "total": len(targets)})
    except Exception as e:
        job_log(jid, f"✗ {e}")
        job_err(jid, e)


def _tracking_url(article_id: int) -> str:
    try:
        return url_for("notification_click", aid=article_id, _external=True)
    except RuntimeError:
        host = cfg.get("web.host", "127.0.0.1")
        port = cfg.get_int("web.port", 5000)
        return f"http://{host}:{port}/notifications/click/{article_id}"


def _article_notification_payload(article: dict) -> dict:
    return {
        "title": article.get("title", ""),
        "url": _tracking_url(article.get("id")),
        "original_url": article.get("url", ""),
        "category": article.get("category", ""),
        "importance": article.get("importance", 0),
        "conclusion": article.get("conclusion", ""),
        "created_at": article.get("created_at", ""),
    }


def _template_context(article: dict) -> dict:
    tracking_url = _tracking_url(article.get("id"))
    return {
        "title": article.get("title", ""),
        "conclusion": article.get("conclusion", ""),
        "category": article.get("category", ""),
        "importance": article.get("importance", 0),
        "tracking_url": tracking_url,
        "original_url": article.get("url", ""),
        "created_at": article.get("created_at", ""),
    }


def _render_notification_template(template: str, context: dict) -> str:
    try:
        return template.format(**context)
    except KeyError as e:
        raise ValueError(f"模板变量不存在: {e.args[0]}") from e


def _access_token_enabled() -> bool:
    return cfg.get_bool("web.access_token.enabled") and bool(cfg.get("web.access_token", "").strip())


def _require_access_token():
    """可选令牌只保护会改数据或导出数据的通知接口。"""
    if not _access_token_enabled():
        return None
    expected = cfg.get("web.access_token", "").strip()
    supplied = request.headers.get("X-Access-Token", "").strip() or request.args.get("token", "").strip()
    if supplied != expected:
        return jsonify({"error": "访问令牌无效"}), 403
    return None


def build_notification_content(channel: dict, article: dict) -> dict:
    context = _template_context(article)
    if channel.get("channel_type") == "email":
        subject_t = cfg.get("notify.template.email_subject", "[资讯] {title}")
        body_t = cfg.get("notify.template.email_body", "{title}\n\n{conclusion}\n\n{tracking_url}")
        return {
            "channel_type": "email",
            "subject": _render_notification_template(subject_t, context),
            "body": _render_notification_template(body_t, context),
            **context,
        }
    payload = {
        "title": context["title"],
        "url": context["tracking_url"],
        "category": context["category"],
        "importance": context["importance"],
        "created_at": context["created_at"],
    }
    if cfg.get_bool("notify.template.webhook_include_summary"):
        payload["conclusion"] = context["conclusion"]
    if cfg.get_bool("notify.template.webhook_include_original_url"):
        payload["original_url"] = context["original_url"]
    return {
        "channel_type": "webhook",
        "payload": payload,
        **context,
    }


def _email_smtp_missing() -> list:
    if not cfg.get_bool("notify.email.enabled"):
        return ["未启用邮件通知发送"]
    required = [
        ("notify.email.smtp_server", "SMTP 服务器"),
        ("notify.email.smtp_port", "SMTP 端口"),
        ("notify.email.from", "发件人"),
    ]
    return [label for key, label in required if not str(cfg.get(key, "")).strip()]


def _validate_notification_channel(data: dict) -> tuple[bool, str]:
    channel_type = str(data.get("channel_type") or "").strip()
    target = str(data.get("target") or "").strip()
    if channel_type not in ("email", "webhook"):
        return False, "通知渠道仅支持 email 或 webhook"
    if not str(data.get("name") or "").strip():
        return False, "渠道名称不能为空"
    if not target:
        return False, "目标不能为空"
    if channel_type == "webhook":
        parsed = urlparse(target)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False, "Webhook URL 无效"
    if channel_type == "email":
        if "@" not in target or target.startswith("@") or target.endswith("@"):
            return False, "收件邮箱无效"
    return True, ""


def get_notification_health_summary() -> dict:
    channels = get_notification_channels()
    enabled = [c for c in channels if c.get("enabled")]
    missing = _email_smtp_missing()
    logs = get_notification_logs(limit=100)
    last_success = next((l for l in logs if l.get("status") in ("ok", "test_ok")), None)
    last_failure = next((l for l in logs if l.get("status") in ("error", "test_error")), None)
    return {
        "enabled_channels": len(enabled),
        "total_channels": len(channels),
        "smtp_ready": not missing,
        "smtp_status": "SMTP 配置完整" if not missing else "SMTP 配置不完整：" + "、".join(missing),
        "last_success_at": last_success.get("created_at") if last_success else "",
        "last_failure_at": last_failure.get("created_at") if last_failure else "",
        "last_failure_error": last_failure.get("error", "") if last_failure else "",
    }


def send_article_notification(channel_id: int, article_id: int) -> dict:
    channel = get_notification_channel(channel_id)
    article = get_article(article_id)
    if not channel:
        return {"ok": False, "error": "通知渠道不存在"}
    if not article:
        return {"ok": False, "error": "文章不存在"}
    if not channel.get("enabled"):
        return {"ok": False, "error": "通知渠道已停用"}

    content = build_notification_content(channel, article)
    payload = content.get("payload") or {
        "title": content.get("title", ""),
        "url": content.get("tracking_url", ""),
        "original_url": content.get("original_url", ""),
        "category": content.get("category", ""),
        "importance": content.get("importance", 0),
        "conclusion": content.get("conclusion", ""),
    }
    payload_text = json.dumps(payload, ensure_ascii=False)
    try:
        if channel["channel_type"] == "webhook":
            resp = requests.post(channel["target"], json=payload, timeout=15)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        elif channel["channel_type"] == "email":
            missing = _email_smtp_missing()
            if missing:
                raise RuntimeError("SMTP 配置不完整：" + "、".join(missing))
            msg = EmailMessage()
            sender = cfg.get("notify.email.from") or cfg.get("notify.email.username")
            msg["From"] = sender
            msg["To"] = channel["target"]
            msg["Subject"] = content["subject"][:160]
            msg.set_content(content["body"], charset="utf-8")
            server = cfg.get("notify.email.smtp_server")
            port = cfg.get_int("notify.email.smtp_port", 465)
            username = cfg.get("notify.email.username")
            password = cfg.get("notify.email.password")
            with smtplib.SMTP_SSL(server, port, timeout=15) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(msg)
        else:
            raise RuntimeError("通知渠道仅支持 email 或 webhook")
        record_notification_log(channel_id, article_id, "ok", payload_text)
        return {"ok": True}
    except Exception as e:
        record_notification_log(channel_id, article_id, "error", payload_text, str(e))
        return {"ok": False, "error": str(e)}


def send_recommended_notifications(limit: int = 5, days: int = 1,
                                   skip_sent: bool = True,
                                   dedupe_days: int = 7) -> dict:
    channels = get_notification_channels(enabled_only=True)
    articles = get_recommended_articles(days=days, limit=limit)
    if not channels:
        return {"ok": False, "error": "没有启用的通知渠道", "channels": 0, "articles": len(articles), "sent": 0, "failed": 0}
    if not articles:
        return {"ok": False, "error": "没有可推送文章", "channels": len(channels), "articles": 0, "sent": 0, "failed": 0}
    sent = failed = skipped = 0
    for channel in channels:
        for article in articles:
            if skip_sent and has_successful_notification(article["id"], dedupe_days=dedupe_days):
                skipped += 1
                record_notification_log(
                    channel["id"],
                    article["id"],
                    "skipped",
                    _article_notification_payload(article).get("title", ""),
                    f"近 {dedupe_days} 天已成功推送",
                )
                continue
            result = send_article_notification(channel["id"], article["id"])
            if result.get("ok"):
                sent += 1
            else:
                failed += 1
    return {
        "ok": failed == 0,
        "channels": len(channels),
        "articles": len(articles),
        "sent": sent,
        "failed": failed,
        "skipped": skipped,
    }


def classify_rss_error(error: str = "", status: str = "") -> str:
    """把底层错误归类为页面可读原因。"""
    err = (error or "").lower()
    st = (status or "").lower()
    if not error and st == "ok":
        return "正常"
    if not error:
        return "—"
    if "timed out" in err or "timeout" in err or "connecttimeout" in err:
        return "连接超时"
    if "certificate_verify_failed" in err or "certificateverify" in err:
        return "证书验证失败"
    if "ssl" in err:
        return "SSL 连接失败"
    if "http 404" in err:
        return "HTTP 404"
    if "http " in err:
        return "HTTP 错误"
    if "no entries" in err or "未获取到文章" in err:
        return "未解析到文章"
    if "not well-formed" in err or "invalid token" in err:
        return "返回内容不是有效 RSS"
    if "connection" in err:
        return "连接失败"
    return "其他错误"


def run_scheduled_rss():
    jid = new_job("schedule-rss")
    job_log(jid, "定时任务触发")
    threading.Thread(target=_run_rss_job, args=(jid,), daemon=True).start()


def run_scheduled_brief():
    jid = new_job("schedule-brief")
    job_log(jid, "定时任务触发")
    threading.Thread(target=_run_brief_job, args=(jid, "md"), daemon=True).start()


def run_scheduled_push():
    limit = cfg.get_int("notify.push.limit", 5)
    days = cfg.get_int("notify.push.days", 1)
    skip_sent = cfg.get_bool("notify.push.skip_sent")
    dedupe_days = cfg.get_int("notify.push.dedupe_days", 7)
    result = send_recommended_notifications(
        limit=limit,
        days=days,
        skip_sent=skip_sent,
        dedupe_days=dedupe_days,
    )
    logger.info(f"定时推荐推送完成: {result}")
    return result


def _scheduler_loop():
    while True:
        schedule.run_pending()
        time.sleep(30)


def setup_scheduler(start_thread=True) -> int:
    """注册 RSS、简报和通知推送定时任务。"""
    global _scheduler_started
    schedule.clear("news_agent")
    jobs = 0
    rss_time = cfg.get("schedule.rss_time", "").strip()
    brief_time = cfg.get("schedule.brief_time", "").strip()
    push_time = cfg.get("notify.push.time", "").strip()
    if rss_time:
        schedule.every().day.at(rss_time).do(run_scheduled_rss).tag("news_agent")
        jobs += 1
    if brief_time:
        schedule.every().day.at(brief_time).do(run_scheduled_brief).tag("news_agent")
        jobs += 1
    if cfg.get_bool("notify.push.enabled") and push_time:
        schedule.every().day.at(push_time).do(run_scheduled_push).tag("news_agent")
        jobs += 1
    if start_thread and not _scheduler_started:
        threading.Thread(target=_scheduler_loop, daemon=True).start()
        _scheduler_started = True
    return jobs


# ============================================================
# 页面路由
# ============================================================

@app.route("/")
def index():
    return render_template("index.html",
                           stats=get_stats(7),
                           cs=cache_stats(),
                           llm=cfg.get("llm.active","—"))


@app.route("/articles")
def articles():
    days    = int(request.args.get("days", 7))
    cat     = request.args.get("cat", "")
    min_imp = int(request.args.get("imp", 0))
    search  = request.args.get("q", "")
    sort    = request.args.get("sort", "")
    page    = int(request.args.get("page", 1))
    per_page = 20
    offset  = (page - 1) * per_page
    if sort == "recommend":
        arts = get_recommended_articles(days=days, limit=per_page, offset=offset,
                                        category=cat, min_importance=min_imp, search=search)
    else:
        arts = get_articles(days=days, limit=per_page, offset=offset,
                            category=cat, min_importance=min_imp, search=search)
    total   = count_articles(days=days, category=cat, min_importance=min_imp, search=search)
    return render_template("articles.html", articles=arts,
                           days=days, cat=cat, min_imp=min_imp, search=search,
                           sort=sort, page=page, per_page=per_page, total=total)


@app.route("/article/<int:aid>")
def article_detail(aid):
    art = get_article(aid)
    if not art:
        return "文章不存在", 404
    try:
        record_article_event(aid, "open")
    except Exception as e:
        logger.warning(f"记录文章打开事件失败: {e}")
    related = find_similar_articles(aid, limit=5, days=30) or find_related(art, days=30, top_k=5)
    channels = get_notification_channels(enabled_only=True)
    explanation = explain_article_recommendation(aid)
    return render_template("article_detail.html", article=art, related=related,
                           channels=channels, explanation=explanation)


@app.route("/process")
def process_page():
    return render_template("process.html")


@app.route("/rss")
def rss_page():
    sources = cfg.get_rss_sources()
    feeds   = [s["url"] for s in sources if s.get("enabled", True)]
    health  = get_rss_health()
    hmap    = {h["feed_url"]: h for h in health}
    max_per = cfg.get_int("rss.max_per_feed", 5)
    categories = ["科技/AI", "商业", "学术", "即刻", "B站", "其他"]
    return render_template("rss.html", sources=sources, feeds=feeds, hmap=hmap,
                           max_per=max_per, categories=categories,
                           classify_rss_error=classify_rss_error)


@app.route("/topics")
def topics_page():
    trending = compute_trending(days=7, top_n=30)
    topics   = get_active_topics(days=7, min_articles=2)
    return render_template("topics.html", trending=trending, topics=topics)


@app.route("/quality")
def quality_page():
    return render_template("quality.html", stats=get_eval_stats(7))


@app.route("/settings")
def settings_page():
    return render_template("settings.html", all_cfg=cfg.get_all())


@app.route("/preferences")
def preferences_page():
    days = int(request.args.get("days", 30))
    return render_template("preferences.html", profile=get_preference_profile(days=days),
                           prefs=cfg.get_preferences(), days=days,
                           effectiveness=get_recommendation_effectiveness(days=days),
                           tuning_logs=get_preference_tuning_logs(days=days, limit=20))


@app.route("/briefs")
def briefs_page():
    brief_map = {}
    briefs_path = Path(_BRIEFS_DIR)
    if briefs_path.exists():
        # 安全：只匹配 brief-YYYY-MM-DD.md/html 格式的文件
        files = sorted(
            list(briefs_path.glob("brief-????-??-??.md")) +
            list(briefs_path.glob("brief-????-??-??.html")),
            reverse=True
        )
        for f in files:
            name = f.name
            date_str = name.replace("brief-", "").replace(".md", "").replace(".html", "")
            fmt = f.suffix.lstrip(".")
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            item = brief_map.setdefault(date_str, {"date": date_str, "files": {}, "content": ""})
            item["files"][fmt] = name
            if fmt == "md" or not item["content"]:
                item["content"] = content
    briefs = sorted(brief_map.values(), key=lambda b: b["date"], reverse=True)[:30]
    return render_template("briefs.html", briefs=briefs)


@app.route("/notifications")
def notifications_page():
    channels = get_notification_channels()
    recommendations = get_recommended_articles(days=1, limit=10)
    log_status = request.args.get("status", "").strip()
    logs = get_notification_logs(limit=30, status=log_status)
    notification_stats = get_notification_stats(days=7)
    notification_health = get_notification_health_summary()
    notify_cfg = cfg.get_all()
    return render_template("notifications.html",
                           channels=channels,
                           recommendations=recommendations,
                           logs=logs,
                           notify_cfg=notify_cfg,
                           log_status=log_status,
                           notification_stats=notification_stats,
                           notification_health=notification_health)


@app.route("/notifications/click/<int:aid>")
def notification_click(aid):
    article = get_article(aid)
    if not article:
        return "文章不存在", 404
    try:
        record_article_event(aid, "click")
    except Exception as e:
        logger.warning(f"记录推送点击事件失败: {e}")
    target = article.get("url") or f"/article/{aid}"
    return redirect(target)


@app.route("/briefs/<path:filename>")
def download_brief(filename):
    if not re.match(r"^brief-\d{4}-\d{2}-\d{2}\.(md|html)$", filename):
        abort(404)
    briefs_path = Path(_BRIEFS_DIR).resolve()
    target = (briefs_path / filename).resolve()
    if not str(target).startswith(str(briefs_path)) or not target.is_file():
        abort(404)
    return send_from_directory(_BRIEFS_DIR, filename, as_attachment=True)


# ============================================================
# API — 任务
# ============================================================

@app.route("/api/job/<jid>")
def api_job(jid):
    job = _jobs.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/api/process-url", methods=["POST"])
def api_process_url():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL 不能为空"}), 400
    jid = new_job("url")
    _jobs[jid]["total"] = 1

    def run():
        try:
            job_log(jid, f"抓取：{url}")
            art = fetch_url(url)
            if not art.get("text"):
                job_log(jid, f"✗ 抓取失败：{art.get('error','')}")
                job_done(jid, {"success": False, "error": art.get("error","")})
                return
            job_log(jid, f"处理中（{art.get('language','')}，{len(art['text'])} 字）")
            art = process_one(art)
            if art.get("skipped"):
                job_log(jid, f"⊘ {art.get('skip_reason','')}")
                job_done(jid, {"skipped": True, "reason": art.get("skip_reason")})
                return
            save_article(art)
            job_log(jid, "✓ 完成")
            _jobs[jid]["progress"] = 1
            job_done(jid, {
                "title": art.get("title"), "category": art.get("category"),
                "importance": art.get("importance"),
                "conclusion": art.get("summary",{}).get("conclusion",""),
                "keywords": art.get("keywords",[]),
            })
        except Exception as e:
            job_log(jid, f"✗ {e}")
            job_err(jid, e)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/process-text", methods=["POST"])
def api_process_text():
    d = request.json or {}
    text = d.get("text","").strip()
    if not text:
        return jsonify({"error": "内容不能为空"}), 400
    jid = new_job("text")
    _jobs[jid]["total"] = 1

    def run():
        try:
            art = from_text(text, title=d.get("title","") or "手动输入",
                            url=d.get("url",""), source=d.get("source",""))
            job_log(jid, f"处理中（{len(text)} 字）")
            art = process_one(art)
            if art.get("skipped"):
                job_log(jid, f"⊘ {art.get('skip_reason','')}")
                job_done(jid, {"skipped": True})
                return
            save_article(art)
            _jobs[jid]["progress"] = 1
            job_log(jid, "✓ 完成")
            job_done(jid, {
                "title": art.get("title"), "category": art.get("category"),
                "importance": art.get("importance"),
                "conclusion": art.get("summary",{}).get("conclusion",""),
                "keywords": art.get("keywords",[]),
            })
        except Exception as e:
            job_log(jid, f"✗ {e}")
            job_err(jid, e)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/run-rss", methods=["POST"])
def api_run_rss():
    jid = new_job("rss")
    threading.Thread(target=_run_rss_job, args=(jid,), daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/run-email", methods=["POST"])
def api_run_email():
    if not cfg.get_bool("email.enabled"):
        return jsonify({"error": "邮件功能未启用，请在设置页面开启"}), 400
    jid = new_job("email")

    def run():
        try:
            job_log(jid, "连接邮箱...")
            emails = fetch_emails()
            if not emails:
                job_log(jid, "没有未读邮件")
                job_done(jid, {"total": 0})
                return
            _jobs[jid]["total"] = len(emails)
            job_log(jid, f"共 {len(emails)} 封，开始处理")
            stats = batch_process(emails, on_complete=make_on_complete(jid))
            job_done(jid, stats)
        except Exception as e:
            job_log(jid, f"✗ {e}")
            job_err(jid, e)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/run-brief", methods=["POST"])
def api_run_brief():
    fmt = (request.json or {}).get("format", "md")  # md 或 html
    jid = new_job("brief")
    threading.Thread(target=_run_brief_job, args=(jid, fmt), daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/test-rss", methods=["POST"])
def api_test_rss():
    url = (request.json or {}).get("url","").strip()
    if not url:
        return jsonify({"error": "URL 不能为空"}), 400
    arts = fetch_rss_feed(url, max_items=3)
    if arts:
        return jsonify({"ok": True, "count": len(arts),
                        "sample": arts[0]["title"] if arts else ""})
    return jsonify({"ok": False, "count": 0})


@app.route("/api/test-rss-all", methods=["POST"])
def api_test_rss_all():
    jid = new_job("rss-test")
    threading.Thread(target=_run_rss_test_job, args=(jid,), daemon=True).start()
    return jsonify({"job_id": jid})


@app.route("/api/rss-enable-ok-only", methods=["POST"])
def api_rss_enable_ok_only():
    health = {h["feed_url"]: h for h in get_rss_health()}
    sources = []
    enabled = disabled = 0
    for source in cfg.get_rss_sources():
        is_ok = health.get(source["url"], {}).get("status") == "ok"
        source["enabled"] = is_ok
        if is_ok:
            enabled += 1
        else:
            disabled += 1
        sources.append(source)
    cfg.set_rss_sources(sources)
    return jsonify({"ok": True, "enabled": enabled, "disabled": disabled})


# ============================================================
# API — 数据查询
# ============================================================

@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats(int(request.args.get("days", 7))))


@app.route("/api/trending")
def api_trending():
    t = compute_trending(days=int(request.args.get("days",7)),
                         top_n=int(request.args.get("n",20)))
    return jsonify([{"keyword": k, "count": c} for k, c in t])


@app.route("/api/topics")
def api_topics():
    return jsonify(get_active_topics())


@app.route("/api/recommendations")
def api_recommendations():
    days = int(request.args.get("days", 7))
    limit = int(request.args.get("limit", 20))
    return jsonify(get_recommended_articles(days=days, limit=limit))


@app.route("/api/articles/<int:aid>/recommendation-explanation")
def api_article_recommendation_explanation(aid):
    explanation = explain_article_recommendation(aid)
    if not explanation:
        return jsonify({"error": "文章不存在"}), 404
    return jsonify(explanation)


@app.route("/api/articles/<int:aid>/event", methods=["POST"])
def api_article_event(aid):
    event_type = (request.json or {}).get("event", "").strip()
    try:
        event_id = record_article_event(aid, event_type)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "event_id": event_id, "counts": get_article_event_counts(aid)})


@app.route("/api/rss-health")
def api_rss_health():
    return jsonify(get_rss_health())


@app.route("/api/cache-stats")
def api_cache_stats():
    return jsonify(cache_stats())


@app.route("/api/cache-clear", methods=["POST"])
def api_cache_clear():
    days = (request.json or {}).get("days", 30)
    n = cache_clear(days=days)
    return jsonify({"cleared": n})


@app.route("/api/notification-channels", methods=["GET"])
def api_notification_channels_get():
    return jsonify(get_notification_channels())


@app.route("/api/notification-channels", methods=["POST"])
def api_notification_channels_post():
    denied = _require_access_token()
    if denied:
        return denied
    data = request.json or {}
    if data.get("channel_type") == "telegram":
        return jsonify({"error": "当前阶段不支持 Telegram"}), 400
    ok, msg = _validate_notification_channel(data)
    if not ok:
        return jsonify({"error": msg}), 400
    try:
        channel_id = save_notification_channel(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": channel_id})


@app.route("/api/notification-channels/<int:channel_id>", methods=["PUT"])
def api_notification_channels_put(channel_id):
    denied = _require_access_token()
    if denied:
        return denied
    if not get_notification_channel(channel_id):
        return jsonify({"error": "通知渠道不存在"}), 404
    data = request.json or {}
    data["id"] = channel_id
    if data.get("channel_type") == "telegram":
        return jsonify({"error": "当前阶段不支持 Telegram"}), 400
    ok, msg = _validate_notification_channel(data)
    if not ok:
        return jsonify({"error": msg}), 400
    try:
        save_notification_channel(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": channel_id})


@app.route("/api/notification-channels/<int:channel_id>", methods=["DELETE"])
def api_notification_channels_delete(channel_id):
    denied = _require_access_token()
    if denied:
        return denied
    deleted = delete_notification_channel(channel_id)
    if not deleted:
        return jsonify({"error": "通知渠道不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/notification-channels/<int:channel_id>/test", methods=["POST"])
def api_notification_channels_test(channel_id):
    denied = _require_access_token()
    if denied:
        return denied
    channel = get_notification_channel(channel_id)
    if not channel:
        return jsonify({"ok": False, "error": "通知渠道不存在"}), 404
    if channel["channel_type"] == "email":
        missing = _email_smtp_missing()
        if missing:
            return jsonify({"ok": False, "error": "SMTP 配置不完整：" + "、".join(missing)}), 400
    article_id = (request.json or {}).get("article_id")
    if not article_id:
        articles = get_recommended_articles(days=365, limit=1)
        if not articles:
            return jsonify({"ok": False, "error": "没有可用于测试发送的文章"}), 400
        article_id = articles[0]["id"]
    result = send_article_notification(channel_id, article_id)
    if result.get("ok"):
        record_notification_log(channel_id, article_id, "test_ok", "测试发送成功")
    else:
        record_notification_log(channel_id, article_id, "test_error", "测试发送失败", result.get("error", ""))
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/notification-channels/test-all", methods=["POST"])
def api_notification_channels_test_all():
    denied = _require_access_token()
    if denied:
        return denied
    channels = get_notification_channels(enabled_only=True)
    articles = get_recommended_articles(days=365, limit=1)
    if not articles:
        return jsonify({"ok": False, "error": "没有可用于测试发送的文章"}), 400
    article_id = articles[0]["id"]
    ok_count = failed = 0
    results = []
    for channel in channels:
        result = send_article_notification(channel["id"], article_id)
        if result.get("ok"):
            ok_count += 1
            record_notification_log(channel["id"], article_id, "test_ok", "测试发送成功")
        else:
            failed += 1
            record_notification_log(channel["id"], article_id, "test_error", "测试发送失败", result.get("error", ""))
        results.append({"channel_id": channel["id"], "name": channel["name"], **result})
    return jsonify({"success": failed == 0, "tested": len(channels), "ok": ok_count,
                    "failed": failed, "results": results})


@app.route("/api/notification-logs")
def api_notification_logs():
    channel_id = request.args.get("channel_id")
    status = request.args.get("status", "").strip()
    return jsonify(get_notification_logs(channel_id=int(channel_id) if channel_id else None, status=status))


@app.route("/api/notification-logs/export")
def api_notification_logs_export():
    denied = _require_access_token()
    if denied:
        return denied
    channel_id = request.args.get("channel_id")
    status = request.args.get("status", "").strip()
    logs = get_notification_logs(channel_id=int(channel_id) if channel_id else None,
                                 status=status, limit=10000)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["created_at", "channel_name", "article_title", "status", "error", "payload"])
    for log in logs:
        writer.writerow([
            log.get("created_at", ""),
            log.get("channel_name", ""),
            log.get("article_title") or "",
            log.get("status", ""),
            log.get("error", ""),
            log.get("payload", ""),
        ])
    resp = Response(out.getvalue(), mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = "attachment; filename=notification-logs.csv"
    return resp


@app.route("/api/notifications/send", methods=["POST"])
def api_notification_send():
    denied = _require_access_token()
    if denied:
        return denied
    data = request.json or {}
    result = send_article_notification(data.get("channel_id"), data.get("article_id"))
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/notifications/preview")
def api_notification_preview():
    channel_id = request.args.get("channel_id")
    article_id = request.args.get("article_id")
    channel = get_notification_channel(int(channel_id)) if channel_id else None
    article = get_article(int(article_id)) if article_id else None
    if not channel:
        return jsonify({"error": "通知渠道不存在"}), 404
    if not article:
        return jsonify({"error": "文章不存在"}), 404
    try:
        content = build_notification_content(channel, article)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({
        "channel": {"id": channel["id"], "name": channel["name"], "channel_type": channel["channel_type"], "target": channel["target"]},
        "article": {"id": article["id"], "title": article["title"], "category": article["category"]},
        "content": content,
    })


@app.route("/api/notifications/send-recommended", methods=["POST"])
def api_notification_send_recommended():
    denied = _require_access_token()
    if denied:
        return denied
    data = request.json or {}
    result = send_recommended_notifications(
        limit=int(data.get("limit", 5)),
        days=int(data.get("days", 1)),
        skip_sent=bool(data.get("skip_sent", cfg.get_bool("notify.push.skip_sent"))),
        dedupe_days=int(data.get("dedupe_days", cfg.get_int("notify.push.dedupe_days", 7))),
    )
    return jsonify(result), (200 if result.get("ok") else 400)


# ============================================================
# API — 配置读写（Web 页面配置的核心）
# ============================================================

@app.route("/api/config", methods=["GET"])
def api_config_get():
    """返回所有配置，前端 Settings 页面用"""
    return jsonify(cfg.get_all())


@app.route("/api/config", methods=["POST"])
def api_config_set():
    """批量保存配置（Settings 页面提交）"""
    data = request.json or {}
    if not data:
        return jsonify({"error": "空数据"}), 400
    # 安全过滤：只允许更新已知 key 或以合法前缀开头的 key
    allowed_prefixes = ("llm.", "feature.", "rss.", "email.", "brief.", "schedule.", "web.", "pref.", "notify.")
    filtered = {k: v for k, v in data.items()
                if any(k.startswith(p) for p in allowed_prefixes)}
    if any(k.startswith("notify.") or k.startswith("web.access_token") for k in filtered):
        denied = _require_access_token()
        if denied:
            return denied
    errors = cfg.set_many(filtered)
    if errors:
        return jsonify({"ok": False, "saved": len(filtered) - len(errors), "errors": errors}), 400
    if any(k.startswith("schedule.") for k in filtered):
        setup_scheduler()
    return jsonify({"ok": True, "saved": len(filtered)})


@app.route("/api/config/<key>", methods=["GET"])
def api_config_one(key):
    return jsonify({"key": key, "value": cfg.get(key)})


@app.route("/api/config/<key>", methods=["PUT"])
def api_config_put(key):
    value = (request.json or {}).get("value")
    if value is None:
        return jsonify({"error": "缺少 value"}), 400
    ok, msg = cfg.validate_config(key, value)
    if not ok:
        return jsonify({"error": msg}), 400
    cfg.set_config(key, value)
    return jsonify({"ok": True})


@app.route("/api/rss-feeds", methods=["GET"])
def api_rss_feeds_get():
    return jsonify(cfg.get_rss_sources())


@app.route("/api/rss-feeds", methods=["POST"])
def api_rss_feeds_set():
    data = request.json or {}
    sources = data.get("sources")
    if sources is None:
        sources = data.get("feeds", [])
    sources = cfg.set_rss_sources(sources)
    return jsonify({"ok": True, "count": len(sources)})


@app.route("/api/preferences", methods=["GET"])
def api_prefs_get():
    return jsonify(cfg.get_preferences())


@app.route("/api/preferences/profile")
def api_preferences_profile():
    days = int(request.args.get("days", 30))
    limit = int(request.args.get("limit", 12))
    return jsonify(get_preference_profile(days=days, limit=limit))


@app.route("/api/preferences/dashboard")
def api_preferences_dashboard():
    days = int(request.args.get("days", 7))
    return jsonify({
        "effectiveness": get_recommendation_effectiveness(days=days),
        "tuning_logs": get_preference_tuning_logs(days=days, limit=30),
    })


@app.route("/api/preferences/tune", methods=["POST"])
def api_preferences_tune():
    data = request.json or {}
    action = (data.get("action") or "").strip()
    prefs = cfg.get_preferences()

    def tune_term(term: str, mode: str):
        term = (term or "").strip()
        if not term:
            return
        if mode == "boost":
            prefs.setdefault("boost", {})[term] = int(prefs.get("boost", {}).get(term, 0)) + 1
            prefs.get("penalty", {}).pop(term, None)
            prefs["muted"] = [t for t in prefs.get("muted", []) if t != term]
        elif mode == "penalty":
            prefs.setdefault("penalty", {})[term] = int(prefs.get("penalty", {}).get(term, 0)) - 1
            prefs.get("boost", {}).pop(term, None)
            prefs["muted"] = [t for t in prefs.get("muted", []) if t != term]
        elif mode == "mute":
            if term not in prefs.setdefault("muted", []):
                prefs["muted"].append(term)
            prefs.get("boost", {}).pop(term, None)
            prefs.get("penalty", {}).pop(term, None)

    if action == "apply_profile":
        profile = get_preference_profile(days=int(data.get("days", 30)), limit=int(data.get("limit", 5)))
        applied_terms = []
        for topic in profile.get("positive_topics", [])[: int(data.get("limit", 5))]:
            tune_term(topic.get("term", ""), "boost")
            applied_terms.append(topic.get("term", ""))
        for topic in profile.get("negative_topics", [])[: int(data.get("limit", 5))]:
            tune_term(topic.get("term", ""), "penalty")
            applied_terms.append(topic.get("term", ""))
        record_preference_tuning("画像主题", "apply_profile", source=data.get("source", "api"),
                                 payload={"terms": applied_terms, "days": data.get("days", 30)})
    elif action in {"boost", "penalty", "mute"}:
        term = data.get("term", "")
        tune_term(term, action)
        record_preference_tuning(term, action, source=data.get("source", "api"))
    else:
        return jsonify({"error": "不支持的纠偏动作"}), 400

    cfg.set_config("pref.boost", prefs.get("boost", {}))
    cfg.set_config("pref.penalty", prefs.get("penalty", {}))
    cfg.set_config("pref.muted", prefs.get("muted", []))
    return jsonify({"ok": True, "preferences": cfg.get_preferences()})


@app.route("/api/preferences", methods=["POST"])
def api_prefs_set():
    d = request.json or {}
    mapping = {
        "boost":      "pref.boost",
        "penalty":    "pref.penalty",
        "muted":      "pref.muted",
        "cat_weight": "pref.cat_weight",
    }
    for k, ck in mapping.items():
        if k in d:
            cfg.set_config(ck, d[k])
    return jsonify({"ok": True})


@app.route("/api/test-llm", methods=["POST"])
def api_test_llm():
    """测试当前 LLM 配置是否可用"""
    try:
        from llm_client import chat
        result = chat("你是助手。", "请回复'OK'，不要其他内容。",
                      temperature=0, use_cache=False)
        return jsonify({"ok": True, "response": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ============================================================
# 启动
# ============================================================

def run():
    cfg.init_config()
    init_db()
    # 初始化 secret_key
    secret_key = cfg.get("web.secret_key")
    if not secret_key:
        secret_key = uuid.uuid4().hex
        cfg.set_config("web.secret_key", secret_key)
    app.secret_key = secret_key
    setup_scheduler()
    host  = cfg.get("web.host", "127.0.0.1")
    port  = cfg.get_int("web.port", 5000)
    debug = cfg.get_bool("web.debug", False)
    print(f"\n╔{'═'*50}╗")
    print(f"║  资讯 Agent 控制台")
    print(f"║  http://{host}:{port}")
    print(f"╚{'═'*50}╝\n")
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    run()
