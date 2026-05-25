# ============================================================
# processors/summarize.py —— 摘要流水线，功能开关来自 config_store
# ============================================================

import re, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from llm_client import chat
import config_store as cfg
from storage import save_eval_score, find_or_create_cluster

logger = logging.getLogger("summarize")

CATEGORIES = ["科技/AI", "商业", "学术", "即刻", "B站", "其他"]

# ── 分类路由 ─────────────────────────────────────────────

ROUTING_SYS = """你是内容分类助手。根据标题和开头判断分类。

选项：科技/AI / 商业 / 学术 / 即刻 / B站 / 其他

只输出分类名称，不解释。"""


def classify(title: str, preview: str) -> str:
    result = chat(ROUTING_SYS, f"标题：{title}\n开头：{preview[:400]}", temperature=0.1)
    for cat in CATEGORIES:
        if cat in result:
            return cat
    return "其他"


# ── 分类专用 prompt ──────────────────────────────────────

PROMPTS = {
    "科技/AI": """\
你是科技编辑。用以下格式总结，中文输出：

【一句话结论】（20字内，说清"发生了什么+为什么重要"）

【技术要点】
- 要点1
- 要点2
- 要点3

【影响判断】（2句话）

【值得关注】（1句话，无则写"无"）

只输出上述格式。""",

    "商业": """\
你是商业分析师。用以下格式总结，中文输出：

【一句话结论】（20字内）

【关键事实】
- 涉及公司/人物：
- 金额/规模（如有）：
- 事件时间：

【商业逻辑】（2句话）

【值得关注】（1句话）

只输出上述格式。""",

    "学术": """\
你是科研助手。用以下格式总结，中文输出：

【一句话结论】（20字内）

【研究方法】（1-2句）

【核心发现】
- 发现1
- 发现2

【局限性】（1句话）

【实际意义】（1句话）

只输出上述格式。""",

    "即刻": """\
提炼社交媒体观点，中文输出：

【核心观点】（20字内）

【主要论据】
- 论据1
- 论据2

【是否值得深入】（1句话）

只输出上述格式。""",

    "B站": """\
中文输出：

【一句话结论】（20字内）

【主要内容】
- 内容点1
- 内容点2
- 内容点3

【适合谁看】（1句话）

只输出上述格式。""",

    "其他": """\
中文简洁总结：

【一句话结论】（20字内）

【主要内容】（3个要点）

只输出上述格式。""",
}

BILINGUAL_SUFFIX = "\n注意：原文是英文。请用中文输出摘要，并在【一句话结论】后附英文原版（括号内）。"

# ── 解析摘要 ─────────────────────────────────────────────

def parse_summary(raw: str) -> dict:
    r = {"conclusion":"", "points":[], "extra":"", "action":"", "raw":raw}
    m = re.search(r"【(?:一句话结论|核心观点)】\s*(.+?)(?=\n【|\Z)", raw, re.S)
    if m: r["conclusion"] = m.group(1).strip()

    pb = re.search(r"【(?:技术要点|关键事实|核心发现|主要论据|主要内容)】(.*?)(?=\n【|\Z)", raw, re.S)
    if pb:
        lines = pb.group(1).strip().split("\n")
        r["points"] = [l.lstrip("-•· ").strip() for l in lines if l.strip() and l.strip()!="-"]

    m2 = re.search(r"【值得关注】\s*(.+?)(?=\n【|\Z)", raw, re.S)
    if m2: r["action"] = m2.group(1).strip()

    others = re.findall(
        r"【(?!一句话结论|核心观点|技术要点|关键事实|核心发现|主要论据|主要内容|值得关注)(.+?)】\s*(.+?)(?=\n【|\Z)",
        raw, re.S)
    r["extra"] = "\n".join(f"{k}：{v.strip()}" for k, v in others)
    return r


# ── 质量评估 ─────────────────────────────────────────────

EVAL_SYS = """\
你是摘要质量评审。评估标准：
1. 抓住原文核心
2. 有具体信息（公司/数字/事件）
3. 格式完整
4. 表达清晰

输出（严格按此格式）：
分数：X
问题：（一句话，无则写"无"）"""


def evaluate(summary_raw: str, title: str) -> tuple[int, str]:
    result = chat(EVAL_SYS, f"原文标题：{title}\n\n摘要：\n{summary_raw}", temperature=0.1)
    sm = re.search(r"分数[:：]\s*(\d+)", result)
    im = re.search(r"问题[:：]\s*(.+)", result, re.S)
    return (int(sm.group(1)) if sm else 7), (im.group(1).strip() if im else "")


# ── 重要性打分 ────────────────────────────────────────────

IMP_SYS = """\
评估文章重要性（1-5分）：
5=行业重大事件  4=值得关注  3=常规资讯  2=边缘信息  1=噪音
只输出数字。"""


def score_importance(title: str, summary: str) -> int:
    result = chat(IMP_SYS, f"标题：{title}\n摘要：{summary[:500]}", temperature=0.1)
    m = re.search(r"\b([1-5])\b", result)
    return int(m.group(1)) if m else 3


# ── 关键词提取 ────────────────────────────────────────────

KW_SYS = """\
从标题和摘要中提取3-5个核心关键词（名词/专有名词）。
用逗号分隔，只输出关键词。
例：OpenAI, GPT-5, 多模态, 苹果"""


def extract_keywords(title: str, summary: str) -> list:
    try:
        result = chat(KW_SYS, f"标题：{title}\n摘要：{summary[:400]}", temperature=0.1)
        kws = re.split(r"[,，、]", result)
        return [k.strip().strip("\"'.。") for k in kws if 1 < len(k.strip()) < 30][:5]
    except Exception:
        return []


# ── 偏好调整分数 ─────────────────────────────────────────

def adjust_importance(base: int, keywords: list, category: str) -> int:
    prefs = cfg.get_preferences()
    muted = set(prefs.get("muted", []))
    if any(k in muted for k in keywords):
        return 1
    adj = 0
    for k in keywords:
        adj += prefs.get("boost", {}).get(k, 0)
        adj += prefs.get("penalty", {}).get(k, 0)
    adj += prefs.get("cat_weight", {}).get(category, 0)
    return max(1, min(5, base + adj))


# ── 主流水线 ─────────────────────────────────────────────

def process_article(article: dict) -> dict:
    """完整处理：分类→摘要→质检→评分→关键词→偏好→话题聚合
    关键词提取和重要性评分可并发执行。"""
    title    = article.get("title", "")
    text     = article.get("text", "")
    language = article.get("language", "中文")
    url_hash = article.get("url_hash", "")

    if not text:
        article.update({"category":"其他","summary":{},"importance":1,"keywords":[]})
        return article

    # 1. 分类
    preset_category = article.get("rss_category") or article.get("category")
    category = preset_category if preset_category in CATEGORIES else classify(title, text)
    article["category"] = category

    # 2. 生成摘要
    prompt = PROMPTS.get(category, PROMPTS["其他"])
    if cfg.get_bool("feature.bilingual") and language == "英文":
        prompt += BILINGUAL_SUFFIX

    input_text = f"标题：{title}\n\n正文：{text[:4000]}"
    summary_raw = chat(prompt, input_text, temperature=0.3)

    # 3. 质量评估 + 自动重生
    if cfg.get_bool("feature.evaluator"):
        min_score  = cfg.get_int("feature.eval_min_score", 7)
        max_retry  = cfg.get_int("feature.eval_max_retry", 1)
        for attempt in range(max_retry + 1):
            score, issue = evaluate(summary_raw, title)
            if url_hash:
                save_eval_score(url_hash, category, score, issue, attempt)
            if score >= min_score:
                break
            if attempt < max_retry:
                retry_prompt = (
                    f"你之前的摘要质量评分为 {score}/10。\n"
                    f"问题：{issue}\n"
                    f"请重新生成更高质量的摘要。\n\n{prompt}"
                )
                summary_raw = chat(retry_prompt, input_text, temperature=0.4, use_cache=False)

    article["summary_raw"] = summary_raw
    article["summary"]     = parse_summary(summary_raw)

    # 4. 关键词提取 & 5. 重要性评分 —— 并发执行
    keywords = []
    base = 3
    use_keywords = cfg.get_bool("feature.keywords")
    use_importance = cfg.get_bool("feature.importance")

    if use_keywords or use_importance:
        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = {}
            if use_keywords:
                futures[ex.submit(extract_keywords, title, summary_raw)] = "keywords"
            if use_importance:
                futures[ex.submit(score_importance, title, summary_raw)] = "importance"
            for future in as_completed(futures):
                tag = futures[future]
                try:
                    result = future.result(timeout=30)
                    if tag == "keywords":
                        keywords = result
                    elif tag == "importance":
                        base = result
                except Exception as e:
                    logger.warning(f"并发执行 {tag} 失败: {e}")

    article["keywords"] = keywords

    # 6. 偏好调整
    article["importance"] = adjust_importance(base, keywords, category)
    article["base_importance"] = base

    # 7. 话题聚合
    if cfg.get_bool("feature.topic_cluster") and keywords:
        article["topic_cluster_id"] = find_or_create_cluster(keywords)

    return article
