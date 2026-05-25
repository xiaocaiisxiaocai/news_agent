# ============================================================
# processors/batch.py —— 并发批处理
# ============================================================

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable
import config_store as cfg
from storage import make_hash, is_duplicate
from processors.summarize import process_article

logger = logging.getLogger("batch")


def process_one(article: dict, skip_dup: bool = True) -> dict:
    article["url_hash"] = make_hash(article.get("url",""), article.get("title",""))
    if skip_dup and is_duplicate(article["url_hash"]):
        article["skipped"] = True
        article["skip_reason"] = "重复"
        return article
    if not article.get("text"):
        article["skipped"] = True
        article["skip_reason"] = "无内容"
        return article
    try:
        article = process_article(article)
        article["skipped"] = False
    except Exception as e:
        article["error"]       = str(e)
        article["skipped"]     = True
        article["skip_reason"] = f"处理出错：{e}"
    return article


def batch_process(articles: list, on_complete: Callable = None,
                  task_timeout: int = 120) -> dict:
    """并发批处理文章，支持超时和有序回调

    Args:
        articles: 待处理文章列表
        on_complete: 每篇文章处理完的回调
        task_timeout: 单个任务超时秒数（默认120秒）
    """
    workers = cfg.get_int("feature.max_workers", 5)
    total   = len(articles)
    stats   = {"total": total, "success": 0, "skipped": 0, "failed": 0}
    if not total:
        return stats

    # 用 enumerate 保持输入顺序，使日志可追踪
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_one, art): (idx, art) for idx, art in enumerate(articles)}
        for future in as_completed(futures, timeout=task_timeout * len(articles)):
            idx, orig_art = futures[future]
            try:
                result = future.result(timeout=task_timeout)
            except Exception as e:
                stats["failed"] += 1
                if on_complete:
                    on_complete({"skipped": True, "skip_reason": str(e),
                                 "title": orig_art.get("title",""), "error": str(e)})
                continue

            if result.get("skipped"):
                stats["skipped"] += 1
            elif result.get("error"):
                stats["failed"] += 1
            else:
                stats["success"] += 1

            if on_complete:
                try:
                    on_complete(result)
                except Exception as e:
                    logger.warning(f"回调异常: {e}")

    return stats
