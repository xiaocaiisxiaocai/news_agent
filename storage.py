# ============================================================
# storage.py —— 数据持久层（SQLite）
# ============================================================

import sqlite3, hashlib, json, os, logging
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
    question_count = title.count("?")
    if question_count < 3:
        return False
    visible_len = len(title.strip())
    return question_count / max(visible_len, 1) >= 0.35


# ── 查询文章 ─────────────────────────────────────────────

def _article_filter_sql(days=7, category="", min_importance=0, search="") -> tuple[str, list]:
    clauses = ["date(created_at) >= date('now', ?)"]
    params  = [f"-{int(days)} days"]
    if category:
        # 白名单校验防止SQL注入
        valid_cats = ["科技/AI", "商业", "学术", "即刻", "B站", "其他"]
        if category in valid_cats:
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

def cache_get(key: str) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT response FROM llm_cache WHERE cache_key=?", (key,)).fetchone()
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
