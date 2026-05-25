# ============================================================
# processors/topic.py —— 话题趋势
# ============================================================

import json
from collections import Counter
from storage import get_articles, _conn


def compute_trending(days: int = 7, top_n: int = 20) -> list:
    """用 SQL 直接聚合关键词频率，避免加载全部文章到内存"""
    with _conn() as c:
        rows = c.execute(
            "SELECT keywords FROM articles WHERE date(created_at)>=date('now',?)",
            (f"-{int(days)} days",)
        ).fetchall()
    counter = Counter()
    for r in rows:
        try:
            kws = json.loads(r["keywords"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        for kw in kws:
            if kw:
                counter[kw] += 1
    return counter.most_common(top_n)


def find_related(target: dict, days: int = 7, top_k: int = 5) -> list:
    """查找与目标文章关键词重叠的相关文章，使用 SQL 预筛选提升性能"""
    target_kw = set(target.get("keywords", []))
    if not target_kw:
        return []

    # 先通过 SQL 筛选同一分类且有相关关键词的文章，缩小计算范围
    target_cat = target.get("category", "")
    target_id = target.get("id")
    candidates = get_articles(days=days, limit=200, category=target_cat if target_cat else "")

    # 如果同分类不足，扩大到全部分类
    if len(candidates) < 20:
        candidates = get_articles(days=days, limit=200)

    scored = []
    seen_ids = set()
    for art in candidates:
        if art["id"] == target_id or art["id"] in seen_ids:
            continue
        seen_ids.add(art["id"])
        art_kw = set(art.get("keywords", []))
        if not art_kw:
            continue
        overlap = len(target_kw & art_kw)
        union   = len(target_kw | art_kw)
        if union > 0 and overlap > 0:
            art["similarity"]        = round(overlap / union, 3)
            art["overlap_keywords"]  = list(target_kw & art_kw)
            scored.append(art)
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]
