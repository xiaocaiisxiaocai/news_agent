# ============================================================
# storage.py —— 数据持久层（SQLite）
# ============================================================

import sqlite3, hashlib, json, os, logging, re
from datetime import datetime, date
from contextlib import contextmanager
from config_store import DB_PATH

logger = logging.getLogger("storage")

_db_initialized = False


def _ensure():
    """确保数据目录存在，仅在建表时调用一次"""
    global _db_initialized
    if not _db_initialized:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _db_initialized = True


@contextmanager
def _conn():
    """数据库连接上下文管理器，自动提交/回滚/关闭"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # WAL模式提升并发读性能
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """建所有业务表，幂等"""
    _ensure()
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash         TEXT UNIQUE NOT NULL,
                title            TEXT DEFAULT '',
                url              TEXT DEFAULT '',
                source           TEXT DEFAULT '',
                category         TEXT DEFAULT '其他',
                importance       INTEGER DEFAULT 3,
                language         TEXT DEFAULT '中文',
                keywords         TEXT DEFAULT '[]',
                topic_cluster_id INTEGER,
                summary          TEXT DEFAULT '',
                conclusion       TEXT DEFAULT '',
                points           TEXT DEFAULT '',
                action           TEXT DEFAULT '',
                created_at       TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_art_hash ON articles(url_hash)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_art_dt   ON articles(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_art_imp  ON articles(importance)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_art_cat  ON articles(category)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS eval_scores (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash    TEXT,
                category    TEXT,
                score       INTEGER,
                issue       TEXT DEFAULT '',
                retry_count INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_eval_dt ON eval_scores(created_at)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS topic_clusters (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_keywords TEXT DEFAULT '[]',
                article_count INTEGER DEFAULT 1,
                first_seen    TEXT NOT NULL,
                last_updated  TEXT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS rss_health (
                feed_url             TEXT PRIMARY KEY,
                last_fetch           TEXT,
                last_success         TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                total_items_fetched  INTEGER DEFAULT 0,
                status               TEXT DEFAULT 'unknown',
                last_error           TEXT DEFAULT ''
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key  TEXT PRIMARY KEY,
                response   TEXT NOT NULL,
                model      TEXT DEFAULT '',
                hit_count  INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cache_dt ON llm_cache(created_at)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS article_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                weight     INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_article_events_article ON article_events(article_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_article_events_type ON article_events(event_type)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS notification_channels (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                channel_type TEXT NOT NULL,
                target       TEXT NOT NULL,
                enabled      INTEGER DEFAULT 1,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_notify_channels_enabled ON notification_channels(enabled)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS notification_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                article_id INTEGER,
                status     TEXT NOT NULL,
                payload    TEXT DEFAULT '',
                error      TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_notify_logs_channel ON notification_logs(channel_id)")


# ── 去重工具 ─────────────────────────────────────────────

def make_hash(url: str, title: str = "") -> str:
    key = url.split("?")[0].rstrip("/") if url else title.strip()
    return hashlib.md5(key.encode("utf-8")).hexdigest()


def is_duplicate(url_hash: str) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM articles WHERE url_hash=? LIMIT 1", (url_hash,)
        ).fetchone() is not None


# ── 写入文章 ─────────────────────────────────────────────

def save_article(art: dict) -> bool:
    """保存文章。重复 url_hash 返回 False。"""
    url_hash = art.get("url_hash") or make_hash(art.get("url",""), art.get("title",""))
    s = art.get("summary", {})
    keywords = art.get("keywords", [])
    points   = s.get("points", [])

    # 字段截断日志
    title_raw = art.get("title","")
    if _looks_garbled_title(title_raw):
        fallback = s.get("conclusion") or art.get("url") or title_raw
        logger.warning(f"文章标题疑似乱码，使用兜底标题: {title_raw[:80]} -> {fallback[:80]}")
        title_raw = fallback
    if len(title_raw) > 500:
        logger.warning(f"文章标题截断: {title_raw[:80]}... ({len(title_raw)}字)")

    try:
        with _conn() as c:
            c.execute("""
                INSERT INTO articles
                (url_hash,title,url,source,category,importance,language,
                 keywords,topic_cluster_id,summary,conclusion,points,action,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                url_hash,
                title_raw[:500],
                art.get("url","")[:1000],
                art.get("source","")[:200],
                art.get("category","其他"),
                art.get("importance", 3),
                art.get("language","中文"),
                json.dumps(keywords, ensure_ascii=False),
                art.get("topic_cluster_id"),
                art.get("summary_raw","")[:5000],
                s.get("conclusion","")[:500],
                "\n".join(str(p) for p in points)[:2000],
                s.get("action","")[:500],
                datetime.now().isoformat(),
            ))
        return True
    except sqlite3.IntegrityError:
        return False


def _looks_garbled_title(title: str) -> bool:
    if not title:
        return False
    garbled_count = title.count("?") + title.count("\ufffd")
    if garbled_count < 3:
        return False
    visible_len = len(title.strip())
    return garbled_count / max(visible_len, 1) >= 0.35


# ── 查询文章 ─────────────────────────────────────────────

def _article_filter_sql(days=7, category="", min_importance=0, search="") -> tuple[str, list]:
    clauses = ["date(created_at) >= date('now', ?)"]
    params  = [f"-{int(days)} days"]
    if category:
        clauses.append("category=?"); params.append(category)
    if min_importance:
        clauses.append("importance>=?"); params.append(int(min_importance))
    if search:
        clauses.append("(title LIKE ? OR conclusion LIKE ?)")
        params += [f"%{search}%", f"%{search}%"]
    return " AND ".join(clauses), params


def get_articles(days=7, limit=100, offset=0, category="", min_importance=0, search="") -> list:
    where, params = _article_filter_sql(days, category, min_importance, search)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM articles WHERE {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)]
        ).fetchall()
    return [_art_dict(r) for r in rows]


EVENT_WEIGHTS = {
    "open": 1,
    "click": 2,
    "favorite": 5,
    "share": 3,
    "hide": -30,
    "not_interested": -20,
}


def record_article_event(article_id: int, event_type: str) -> int:
    if event_type not in EVENT_WEIGHTS:
        raise ValueError(f"不支持的行为类型: {event_type}")
    now = datetime.now().isoformat()
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO article_events (article_id,event_type,weight,created_at)
            VALUES (?,?,?,?)
        """, (int(article_id), event_type, EVENT_WEIGHTS[event_type], now))
        return cur.lastrowid


def get_article_event_counts(article_id: int) -> dict:
    with _conn() as c:
        rows = c.execute("""
            SELECT event_type, COUNT(*) cnt FROM article_events
            WHERE article_id=?
            GROUP BY event_type
        """, (int(article_id),)).fetchall()
    return {r["event_type"]: r["cnt"] for r in rows}


def _normalize_term(term: str) -> str:
    return (term or "").strip().lower()


def _text_terms(text: str) -> set[str]:
    """把中英文文本拆成可解释的本地记忆词，不依赖外部向量服务。"""
    terms = set()
    for raw in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text or ""):
        token = _normalize_term(raw)
        if len(token) < 2:
            continue
        terms.add(token)
        if re.fullmatch(r"[\u4e00-\u9fff]+", token) and len(token) > 2:
            terms.update(token[i:i + 2] for i in range(len(token) - 1))
    return terms


def _article_term_weights(article: dict) -> dict[str, int]:
    weights: dict[str, int] = {}

    def add(term: str, weight: int):
        term = _normalize_term(term)
        if len(term) >= 2:
            weights[term] = max(weights.get(term, 0), weight)

    for kw in article.get("keywords") or []:
        add(str(kw), 5)
    for part in str(article.get("category") or "").split("/"):
        add(part, 3)
    for term in _text_terms(article.get("title") or ""):
        add(term, 2)
    body = " ".join(str(article.get(k) or "") for k in ("conclusion", "summary", "points", "action"))
    for term in _text_terms(body):
        add(term, 1)
    return weights


def _article_keyword_lookup(article: dict) -> dict[str, str]:
    return {_normalize_term(str(k)): str(k) for k in article.get("keywords") or [] if str(k).strip()}


def _similarity_parts(left: dict, right: dict) -> tuple[float, list[str]]:
    left_terms = _article_term_weights(left)
    right_terms = _article_term_weights(right)
    if not left_terms or not right_terms:
        return 0.0, []
    keys = set(left_terms) | set(right_terms)
    overlap_keys = set(left_terms) & set(right_terms)
    overlap_weight = sum(min(left_terms[k], right_terms[k]) for k in overlap_keys)
    union_weight = sum(max(left_terms.get(k, 0), right_terms.get(k, 0)) for k in keys)
    if union_weight <= 0:
        return 0.0, []

    keyword_lookup = _article_keyword_lookup(left) | _article_keyword_lookup(right)
    overlap = [keyword_lookup.get(k, k) for k in overlap_keys]
    overlap.sort(key=lambda k: (-max(left_terms.get(_normalize_term(k), 0), right_terms.get(_normalize_term(k), 0)), k))
    return round(overlap_weight / union_weight, 3), overlap[:6]


def find_similar_articles(article_id: int, limit: int = 5, days: int = 30) -> list:
    target = get_article(article_id)
    if not target:
        return []
    candidates = get_articles(days=days, limit=500)
    scored = []
    for article in candidates:
        if article["id"] == target["id"]:
            continue
        similarity, overlap = _similarity_parts(target, article)
        if similarity <= 0:
            continue
        article["similarity"] = similarity
        article["overlap_keywords"] = overlap
        article["memory_reason"] = "相似主题：" + "、".join(overlap[:3]) if overlap else "标题或摘要相似"
        scored.append(article)
    scored.sort(key=lambda a: (a["similarity"], a.get("importance", 0), a.get("created_at", "")), reverse=True)
    return scored[:int(limit)]


def _event_memory_profiles() -> list[tuple[dict, str, int]]:
    with _conn() as c:
        rows = c.execute("""
            SELECT a.*, e.event_type, e.weight
            FROM article_events e
            JOIN articles a ON a.id = e.article_id
            WHERE e.event_type IN ('favorite','share','click','open','hide','not_interested')
            ORDER BY e.created_at DESC
            LIMIT 300
        """).fetchall()
    profiles = []
    for row in rows:
        article = _art_dict(row)
        profiles.append((article, row["event_type"], int(row["weight"] or 0)))
    return profiles


def _memory_score_for_article(article: dict, profiles: list[tuple[dict, str, int]]) -> tuple[int, str]:
    score, reason, _sources = _memory_score_parts_for_article(article, profiles, include_sources=False)
    return score, reason


def _memory_score_parts_for_article(article: dict, profiles: list[tuple[dict, str, int]],
                                    include_sources: bool = True) -> tuple[int, str, list[dict]]:
    total = 0.0
    positive_reason = ""
    negative_reason = ""
    positive_rank = {"favorite": 4, "share": 3, "click": 2, "open": 1}
    negative_rank = {"hide": 2, "not_interested": 1}
    best_positive = (0.0, 0, "")
    best_negative = (0.0, 0, "")
    sources = []

    for memory_article, event_type, weight in profiles:
        if memory_article["id"] == article["id"]:
            continue
        similarity, overlap = _similarity_parts(article, memory_article)
        if similarity < 0.18:
            continue
        weighted = similarity * weight
        total += weighted
        words = "、".join(overlap[:2]) if overlap else "主题"
        if weight > 0:
            rank = positive_rank.get(event_type, 0)
            if (similarity, rank) > (best_positive[0], best_positive[1]):
                best_positive = (similarity, rank, f"与{_event_label(event_type)}文章相似：{words}")
        elif weight < 0:
            rank = negative_rank.get(event_type, 0)
            if (similarity, rank) > (best_negative[0], best_negative[1]):
                best_negative = (similarity, rank, f"与{_event_label(event_type)}文章相似：{words}")
        if include_sources:
            sources.append({
                "id": memory_article["id"],
                "title": memory_article["title"],
                "event_type": event_type,
                "event_label": _event_label(event_type),
                "weight": weight,
                "similarity": similarity,
                "score_delta": round(weighted, 2),
                "overlap_keywords": overlap,
            })

    score = int(round(total))
    if best_positive[2] and score > 0:
        positive_reason = best_positive[2]
    if best_negative[2] and score < 0:
        negative_reason = best_negative[2]
    sources.sort(key=lambda s: abs(s["score_delta"]), reverse=True)
    return score, positive_reason or negative_reason, sources[:5]


def _event_label(event_type: str) -> str:
    return {
        "open": "打开过的",
        "click": "点击过的",
        "favorite": "收藏过的",
        "share": "分享过的",
        "hide": "隐藏过的",
        "not_interested": "不感兴趣的",
    }.get(event_type, "历史")


def _recommend_reason(article: dict, memory_reason: str) -> str:
    reasons = []
    if int(article.get("importance") or 0) >= 4:
        reasons.append("重要性高")
    if int(article.get("feedback_score") or 0) > 0:
        reasons.append("有正向反馈")
    elif int(article.get("feedback_score") or 0) < 0:
        reasons.append("有负向反馈")
    if memory_reason:
        reasons.append(memory_reason)
    return "；".join(reasons) or "按时间和重要性推荐"


def explain_article_recommendation(article_id: int) -> dict | None:
    article = get_article(article_id)
    if not article:
        return None
    counts = get_article_event_counts(article_id)
    feedback_score = 0
    for event_type, count in counts.items():
        feedback_score += EVENT_WEIGHTS.get(event_type, 0) * int(count)
    base_score = int(article.get("importance") or 0) * 10
    memory_score, memory_reason, sources = _memory_score_parts_for_article(article, _event_memory_profiles())
    recommend_score = base_score + feedback_score + memory_score
    article.update({
        "base_score": base_score,
        "feedback_score": feedback_score,
        "memory_score": memory_score,
        "recommend_score": recommend_score,
        "recommend_reason": _recommend_reason(article | {"feedback_score": feedback_score}, memory_reason),
        "open_count": counts.get("open", 0),
        "favorite_count": counts.get("favorite", 0),
        "hide_count": counts.get("hide", 0),
        "not_interested_count": counts.get("not_interested", 0),
    })
    return {
        "article_id": article_id,
        "title": article["title"],
        "base_score": base_score,
        "feedback_score": feedback_score,
        "memory_score": memory_score,
        "recommend_score": recommend_score,
        "recommend_reason": article["recommend_reason"],
        "base_reason": f"重要性 {article.get('importance', 0)} × 10",
        "feedback_counts": counts,
        "memory_sources": sources,
    }


def get_preference_profile(days: int = 30, limit: int = 12) -> dict:
    with _conn() as c:
        rows = c.execute("""
            SELECT a.*, e.event_type, e.weight
            FROM article_events e
            JOIN articles a ON a.id = e.article_id
            WHERE date(e.created_at) >= date('now', ?)
            ORDER BY e.created_at DESC
        """, (f"-{int(days)} days",)).fetchall()
    positive: dict[str, float] = {}
    negative: dict[str, float] = {}
    categories: dict[str, float] = {}
    behavior_counts: dict[str, int] = {}

    for row in rows:
        article = _art_dict(row)
        event_type = row["event_type"]
        weight = int(row["weight"] or 0)
        behavior_counts[event_type] = behavior_counts.get(event_type, 0) + 1
        target = positive if weight > 0 else negative
        for keyword in article.get("keywords") or []:
            term = str(keyword).strip()
            if term:
                target[term] = target.get(term, 0) + abs(weight)
        category = article.get("category") or "其他"
        categories[category] = categories.get(category, 0) + weight

    def top_items(items: dict[str, float]) -> list[dict]:
        ranked = sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))[:int(limit)]
        return [{"term": term, "score": round(score, 2)} for term, score in ranked]

    return {
        "days": int(days),
        "behavior_counts": behavior_counts,
        "positive_topics": top_items(positive),
        "negative_topics": top_items(negative),
        "category_weights": top_items(categories),
    }


def get_recommended_articles(days=7, limit=20, offset=0, category="", min_importance=0, search="") -> list:
    where, params = _article_filter_sql(days, category, min_importance, search)
    where = where.replace("created_at", "a.created_at")
    where = where.replace("category=?", "a.category=?")
    where = where.replace("importance>=?", "a.importance>=?")
    where = where.replace("title LIKE ?", "a.title LIKE ?")
    where = where.replace("conclusion LIKE ?", "a.conclusion LIKE ?")
    with _conn() as c:
        rows = c.execute(f"""
            SELECT a.*,
                   a.importance * 10 AS base_score,
                   COALESCE(SUM(e.weight), 0) AS feedback_score,
                   SUM(CASE WHEN e.event_type='open' THEN 1 ELSE 0 END) AS open_count,
                   SUM(CASE WHEN e.event_type='favorite' THEN 1 ELSE 0 END) AS favorite_count,
                   SUM(CASE WHEN e.event_type='hide' THEN 1 ELSE 0 END) AS hide_count,
                   SUM(CASE WHEN e.event_type='not_interested' THEN 1 ELSE 0 END) AS not_interested_count
            FROM articles a
            LEFT JOIN article_events e ON e.article_id = a.id
            WHERE {where}
            GROUP BY a.id
            ORDER BY a.created_at DESC
        """, params).fetchall()

    profiles = _event_memory_profiles()
    articles = []
    for row in rows:
        article = _art_dict(row)
        memory_score, memory_reason = _memory_score_for_article(article, profiles)
        article["memory_score"] = memory_score
        article["recommend_score"] = int(article.get("base_score") or 0) + int(article.get("feedback_score") or 0) + memory_score
        article["recommend_reason"] = _recommend_reason(article, memory_reason)
        articles.append(article)
    articles.sort(key=lambda a: (a["recommend_score"], a.get("created_at", "")), reverse=True)
    return articles[int(offset):int(offset) + int(limit)]


def count_articles(days=7, category="", min_importance=0, search="") -> int:
    """返回符合当前筛选条件的文章数量，用于分页。"""
    where, params = _article_filter_sql(days, category, min_importance, search)
    with _conn() as c:
        return c.execute(f"SELECT COUNT(*) FROM articles WHERE {where}", params).fetchone()[0]


def get_article(article_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM articles WHERE id=?", (int(article_id),)).fetchone()
    return _art_dict(row) if row else None


def _art_dict(row) -> dict:
    d = dict(row)
    try:    d["keywords"] = json.loads(d.get("keywords") or "[]")
    except: d["keywords"] = []
    return d


def get_today_top(n=5, min_importance=3) -> list:
    today = date.today().isoformat()
    with _conn() as c:
        rows = c.execute("""
            SELECT * FROM articles
            WHERE date(created_at)=? AND importance>=?
            ORDER BY importance DESC, created_at DESC LIMIT ?
        """, (today, min_importance, n)).fetchall()
    return [_art_dict(r) for r in rows]


def get_stats(days=7) -> dict:
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM articles WHERE date(created_at)>=date('now',?)",
            (f"-{days} days",)
        ).fetchone()[0]
        by_cat = c.execute("""
            SELECT category, COUNT(*) cnt FROM articles
            WHERE date(created_at)>=date('now',?)
            GROUP BY category ORDER BY cnt DESC
        """, (f"-{days} days",)).fetchall()
        daily = c.execute("""
            SELECT date(created_at) day, COUNT(*) cnt FROM articles
            WHERE date(created_at)>=date('now',?)
            GROUP BY day ORDER BY day
        """, (f"-{days} days",)).fetchall()
        imp = c.execute("""
            SELECT importance, COUNT(*) cnt FROM articles
            WHERE date(created_at)>=date('now',?)
            GROUP BY importance ORDER BY importance DESC
        """, (f"-{days} days",)).fetchall()
    return {
        "total": total, "days": days,
        "by_category": {r["category"]: r["cnt"] for r in by_cat},
        "daily":       [dict(r) for r in daily],
        "importance":  {str(r["importance"]): r["cnt"] for r in imp},
    }


# ── 质量评分 ─────────────────────────────────────────────

def save_eval_score(url_hash, category, score, issue, retry_count=0):
    with _conn() as c:
        c.execute(
            "INSERT INTO eval_scores (url_hash,category,score,issue,retry_count,created_at) VALUES (?,?,?,?,?,?)",
            (url_hash, category, score, issue or "", retry_count, datetime.now().isoformat())
        )


def get_eval_stats(days=7) -> dict:
    with _conn() as c:
        rows = c.execute("""
            SELECT category,
                   COUNT(*) count,
                   ROUND(AVG(score),2) avg_score,
                   MIN(score) min_score,
                   MAX(score) max_score,
                   SUM(CASE WHEN retry_count>0 THEN 1 ELSE 0 END) retried
            FROM eval_scores WHERE date(created_at)>=date('now',?)
            GROUP BY category ORDER BY avg_score
        """, (f"-{days} days",)).fetchall()
        low = c.execute("""
            SELECT category,score,issue FROM eval_scores
            WHERE score<7 AND date(created_at)>=date('now',?)
            ORDER BY score LIMIT 10
        """, (f"-{days} days",)).fetchall()
    return {"by_category": [dict(r) for r in rows], "low_score_samples": [dict(r) for r in low]}


# ── RSS 健康 ─────────────────────────────────────────────

def record_rss_fetch(feed_url, success, item_count=0, error=""):
    now = datetime.now().isoformat()
    with _conn() as c:
        ex = c.execute("SELECT * FROM rss_health WHERE feed_url=?", (feed_url,)).fetchone()
        if ex:
            if success:
                c.execute("""UPDATE rss_health SET last_fetch=?,last_success=?,
                    consecutive_failures=0,total_items_fetched=total_items_fetched+?,
                    status='ok',last_error='' WHERE feed_url=?""",
                    (now, now, item_count, feed_url))
            else:
                nf = (ex["consecutive_failures"] or 0) + 1
                st = "dead" if nf>=5 else ("warning" if nf>=2 else "ok")
                c.execute("""UPDATE rss_health SET last_fetch=?,consecutive_failures=?,
                    status=?,last_error=? WHERE feed_url=?""",
                    (now, nf, st, error[:300], feed_url))
        else:
            c.execute("""INSERT INTO rss_health
                (feed_url,last_fetch,last_success,consecutive_failures,
                 total_items_fetched,status,last_error)
                VALUES (?,?,?,?,?,?,?)""",
                (feed_url, now, now if success else None,
                 0 if success else 1, item_count if success else 0,
                 "ok" if success else "warning", error[:300]))


def get_rss_health() -> list:
    with _conn() as c:
        rows = c.execute("""SELECT * FROM rss_health
            ORDER BY CASE status WHEN 'dead' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END""").fetchall()
    return [dict(r) for r in rows]


# ── LLM 缓存 ─────────────────────────────────────────────

def cache_get(key: str, max_age_days: int | None = None) -> str | None:
    with _conn() as c:
        sql = "SELECT response FROM llm_cache WHERE cache_key=?"
        params = [key]
        if max_age_days is not None:
            sql += " AND datetime(created_at) >= datetime('now', ?)"
            params.append(f"-{int(max_age_days)} days")
        row = c.execute(sql, params).fetchone()
        if row:
            c.execute("UPDATE llm_cache SET hit_count=hit_count+1 WHERE cache_key=?", (key,))
            return row["response"]
    return None


def cache_set(key: str, response: str, model: str = ""):
    with _conn() as c:
        c.execute("""INSERT OR REPLACE INTO llm_cache (cache_key,response,model,hit_count,created_at)
            VALUES (?,?,?,COALESCE((SELECT hit_count FROM llm_cache WHERE cache_key=?),0),?)""",
            (key, response, model, key, datetime.now().isoformat()))


def cache_clear(days: int = 30) -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM llm_cache WHERE created_at < date('now',?)", (f"-{days} days",))
        return cur.rowcount


def cache_stats() -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) total, SUM(hit_count) hits FROM llm_cache"
        ).fetchone()
    return {"total_entries": row["total"] or 0, "total_hits": row["hits"] or 0}


# ── 通知渠道与发送日志 ─────────────────────────────────────

def save_notification_channel(channel: dict) -> int:
    channel_type = str(channel.get("channel_type") or "").strip()
    if channel_type not in ("email", "webhook"):
        raise ValueError("通知渠道仅支持 email 或 webhook")
    now = datetime.now().isoformat()
    enabled = 1 if channel.get("enabled", True) else 0
    with _conn() as c:
        channel_id = channel.get("id")
        if channel_id:
            c.execute("""
                UPDATE notification_channels
                SET name=?, channel_type=?, target=?, enabled=?, updated_at=?
                WHERE id=?
            """, (
                str(channel.get("name") or channel_type).strip(),
                channel_type,
                str(channel.get("target") or "").strip(),
                enabled,
                now,
                int(channel_id),
            ))
            return int(channel_id)
        cur = c.execute("""
            INSERT INTO notification_channels
            (name,channel_type,target,enabled,created_at,updated_at)
            VALUES (?,?,?,?,?,?)
        """, (
            str(channel.get("name") or channel_type).strip(),
            channel_type,
            str(channel.get("target") or "").strip(),
            enabled,
            now,
            now,
        ))
        return cur.lastrowid


def get_notification_channels(enabled_only: bool = False) -> list:
    where = "WHERE enabled=1" if enabled_only else ""
    with _conn() as c:
        rows = c.execute(f"""
            SELECT c.*,
                   t.status AS last_test_status,
                   t.payload AS last_test_payload,
                   t.error AS last_test_error,
                   t.created_at AS last_test_at
            FROM notification_channels c
            LEFT JOIN (
                SELECT l.* FROM notification_logs l
                JOIN (
                    SELECT channel_id, MAX(id) AS max_id
                    FROM notification_logs
                    WHERE status IN ('test_ok','test_error')
                    GROUP BY channel_id
                ) latest ON latest.max_id = l.id
            ) t ON t.channel_id = c.id
            {where}
            ORDER BY c.id DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_notification_channel(channel_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM notification_channels WHERE id=?", (int(channel_id),)).fetchone()
    return dict(row) if row else None


def delete_notification_channel(channel_id: int) -> int:
    with _conn() as c:
        cur = c.execute("DELETE FROM notification_channels WHERE id=?", (int(channel_id),))
        return cur.rowcount


def record_notification_log(channel_id: int, article_id: int | None, status: str,
                            payload: str = "", error: str = "") -> int:
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO notification_logs
            (channel_id,article_id,status,payload,error,created_at)
            VALUES (?,?,?,?,?,?)
        """, (
            int(channel_id) if channel_id else None,
            int(article_id) if article_id else None,
            status,
            payload[:5000],
            error[:1000],
            datetime.now().isoformat(),
        ))
        return cur.lastrowid


def has_successful_notification(article_id: int, dedupe_days: int = 7,
                                channel_id: int | None = None) -> bool:
    params = [int(article_id), f"-{int(dedupe_days)} days"]
    channel_clause = ""
    if channel_id is not None:
        channel_clause = " AND channel_id=?"
        params.append(int(channel_id))
    with _conn() as c:
        row = c.execute(f"""
            SELECT 1 FROM notification_logs
            WHERE article_id=? AND status='ok'
              AND datetime(created_at) >= datetime('now', ?)
              {channel_clause}
            LIMIT 1
        """, params).fetchone()
    return row is not None


def get_notification_logs(channel_id: int | None = None, limit: int = 50,
                          status: str = "") -> list:
    params = []
    clauses = []
    if channel_id is not None:
        clauses.append("l.channel_id=?")
        params.append(int(channel_id))
    if status:
        clauses.append("l.status=?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with _conn() as c:
        rows = c.execute(f"""
            SELECT l.*,
                   COALESCE(c.name, '已删除渠道') AS channel_name,
                   a.title AS article_title
            FROM notification_logs l
            LEFT JOIN notification_channels c ON c.id = l.channel_id
            LEFT JOIN articles a ON a.id = l.article_id
            {where}
            ORDER BY l.id DESC LIMIT ?
        """, params + [int(limit)]).fetchall()
    return [dict(r) for r in rows]


def get_notification_stats(days: int = 7) -> dict:
    since = f"-{int(days)} days"
    with _conn() as c:
        row = c.execute("""
            SELECT
                SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
                SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed
            FROM notification_logs
            WHERE datetime(created_at) >= datetime('now', ?)
        """, (since,)).fetchone()
        clicks = c.execute("""
            SELECT COUNT(*) AS cnt FROM article_events
            WHERE event_type='click' AND datetime(created_at) >= datetime('now', ?)
        """, (since,)).fetchone()["cnt"] or 0
        channels = c.execute("""
            SELECT COALESCE(c.name, '已删除渠道') AS channel_name,
                   SUM(CASE WHEN l.status='ok' THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN l.status='error' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN l.status='skipped' THEN 1 ELSE 0 END) AS skipped
            FROM notification_logs l
            LEFT JOIN notification_channels c ON c.id = l.channel_id
            WHERE datetime(l.created_at) >= datetime('now', ?)
            GROUP BY l.channel_id, channel_name
            ORDER BY sent DESC, failed ASC
        """, (since,)).fetchall()
    sent = row["sent"] or 0
    skipped = row["skipped"] or 0
    failed = row["failed"] or 0
    channel_stats = []
    for ch in channels:
        ch_sent = ch["sent"] or 0
        ch_failed = ch["failed"] or 0
        attempts = ch_sent + ch_failed
        channel_stats.append({
            "channel_name": ch["channel_name"],
            "sent": ch_sent,
            "skipped": ch["skipped"] or 0,
            "failed": ch_failed,
            "success_rate": round(ch_sent * 100 / attempts) if attempts else 0,
            "click_rate": round(clicks * 100 / ch_sent) if ch_sent else 0,
        })
    return {
        "days": int(days),
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "clicks": clicks,
        "click_rate": round(clicks * 100 / sent) if sent else 0,
        "channels": channel_stats,
    }


# ── 话题聚合 ─────────────────────────────────────────────

def find_or_create_cluster(keywords: list, threshold=0.5) -> int | None:
    if not keywords:
        return None
    kw = set(keywords)
    with _conn() as c:
        rows = c.execute("""SELECT id,topic_keywords FROM topic_clusters
            WHERE date(last_updated)>=date('now','-7 days')""").fetchall()
        best, best_score = None, 0
        for r in rows:
            try: ckw = set(json.loads(r["topic_keywords"]))
            except: continue
            if not ckw: continue
            score = len(kw & ckw) / min(len(kw), len(ckw))
            if score > best_score and score >= threshold:
                best_score, best = score, r
        now = datetime.now().isoformat()
        if best:
            merged = list(set(json.loads(best["topic_keywords"])) | kw)
            c.execute("""UPDATE topic_clusters SET topic_keywords=?,
                article_count=article_count+1,last_updated=? WHERE id=?""",
                (json.dumps(merged, ensure_ascii=False), now, best["id"]))
            return best["id"]
        else:
            cur = c.execute("""INSERT INTO topic_clusters
                (topic_keywords,article_count,first_seen,last_updated) VALUES (?,1,?,?)""",
                (json.dumps(keywords, ensure_ascii=False), now, now))
            return cur.lastrowid


def get_active_topics(days=7, min_articles=2) -> list:
    with _conn() as c:
        rows = c.execute("""SELECT id,topic_keywords,article_count,first_seen,last_updated
            FROM topic_clusters
            WHERE date(last_updated)>=date('now',?) AND article_count>=?
            ORDER BY article_count DESC,last_updated DESC""",
            (f"-{days} days", min_articles)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try: d["topic_keywords"] = json.loads(d["topic_keywords"])
            except: d["topic_keywords"] = []
            arts = c.execute("""SELECT title,url,importance,conclusion FROM articles
                WHERE topic_cluster_id=? ORDER BY importance DESC LIMIT 5""",
                (d["id"],)).fetchall()
            d["articles"] = [dict(a) for a in arts]
            result.append(d)
    return result
