# ============================================================
# outputs/brief.py —— 每日简报（Markdown + HTML）
# ============================================================

import os
import html
from datetime import date
from storage import get_recommended_articles, get_stats
import config_store as cfg


def _smart_articles() -> list:
    n = cfg.get_int("brief.top_n", 5)
    min_imp = cfg.get_int("brief.min_importance", 3)
    # 多取一些，保证分区简报不会被单一类别挤占。
    return get_recommended_articles(days=1, limit=max(n * 4, 20), min_importance=min_imp)


def _stars(article: dict) -> str:
    importance = int(article.get("importance") or 0)
    return "★" * importance + "☆" * max(0, 5 - importance)


def _score_line(article: dict) -> str:
    return (
        f"基础分 {int(article.get('base_score') or 0)} / "
        f"反馈分 {int(article.get('feedback_score') or 0)} / "
        f"记忆分 {int(article.get('memory_score') or 0)} / "
        f"推荐分 {int(article.get('recommend_score') or 0)}"
    )


def _group_articles(articles: list) -> list[tuple[str, list]]:
    return [
        ("今日重点", articles[:3]),
        ("AI/科技", [a for a in articles if a.get("category") == "科技/AI"][:5]),
        ("商业动态", [a for a in articles if a.get("category") == "商业"][:5]),
        ("学术论文", [a for a in articles if a.get("category") == "学术"][:5]),
        (
            "低置信/待观察",
            [a for a in articles if a.get("quality_label") in ("有争议推荐", "缺少行为数据")][:5],
        ),
    ]


def _md_article(article: dict, index: int) -> list[str]:
    title = article.get("title", "")
    url = article.get("url", "")
    title_md = f"[{title}]({url})" if url else title
    lines = [
        f"### {index}. {title_md}",
        f"{_stars(article)}  `{article.get('category','')}`  `{article.get('quality_label','')}`",
        "",
    ]
    if article.get("conclusion"):
        lines += [f"> {article['conclusion']}", ""]
    lines += [
        f"- 推荐理由：{article.get('recommend_reason') or '按重要性和时间推荐'}",
        f"- 推荐质量：{article.get('quality_label') or '未标注'}",
        f"- {_score_line(article)}",
        "",
    ]
    if article.get("points"):
        for p in article["points"].split("\n"):
            if p.strip():
                lines.append(f"- {p.strip()}")
        lines.append("")
    return lines


def _html_article(article: dict, index: int) -> str:
    title = html.escape(article.get("title", ""))
    url = html.escape(article.get("url", ""))
    category = html.escape(article.get("category", ""))
    quality = html.escape(article.get("quality_label", ""))
    reason = html.escape(article.get("recommend_reason") or "按重要性和时间推荐")
    score = html.escape(_score_line(article))
    conclusion = html.escape(article.get("conclusion", ""))
    title_html = f'<a href="{url}" style="color:#1a6fb8;text-decoration:none">{title}</a>' if url else title
    conclusion_html = f'<p style="color:#555;font-size:14px;margin:6px 0">{conclusion}</p>' if conclusion else ""
    points_html = ""
    if article.get("points"):
        pts = "\n".join(
            f'<li style="color:#555;font-size:13px">{html.escape(p.strip())}</li>'
            for p in article["points"].split("\n") if p.strip()
        )
        points_html = f'<ul style="margin:6px 0 0;padding-left:20px">{pts}</ul>' if pts else ""
    return f'''
    <div style="margin:12px 0;padding:12px 14px;border-left:3px solid #c96442;background:#fafaf7;border-radius:4px">
      <h3 style="margin:0 0 4px;font-size:15px">{index}. {title_html}</h3>
      <div style="color:#999;font-size:12px;margin-bottom:6px">
        <span style="color:#b87a14">{_stars(article)}</span>
        <span style="margin-left:8px">{category}</span>
        <span style="margin-left:8px">{quality}</span>
      </div>
      {conclusion_html}
      <p style="color:#555;font-size:13px;margin:4px 0"><strong>推荐理由：</strong>{reason}</p>
      <p style="color:#555;font-size:13px;margin:4px 0"><strong>推荐质量：</strong>{quality or "未标注"}</p>
      <p style="color:#777;font-size:12px;margin:4px 0">{score}</p>
      {points_html}
    </div>'''


def generate_md() -> str:
    """生成 Markdown 格式简报"""
    today   = date.today().isoformat()
    tops    = _smart_articles()
    stats   = get_stats(days=1)

    lines = [f"# 资讯简报 · {today}", ""]
    total = stats["total"]
    lines.append(f"今日共收录 **{total}** 篇，精选 **{len(tops)}** 篇。")

    if stats["by_category"]:
        cats = "、".join(f"{k} {v}" for k, v in stats["by_category"].items())
        lines += ["", f"**分类**：{cats}", ""]

    lines += ["---", ""]
    if not tops:
        lines += ["## 今日重点", "", "> 今日暂无高优先级资讯。", ""]
    for section, articles in _group_articles(tops):
        lines += [f"## {section}", ""]
        if not articles:
            lines += ["> 暂无匹配资讯。", ""]
            continue
        for i, article in enumerate(articles, 1):
            lines += _md_article(article, i)

    return "\n".join(lines)


def generate_html() -> str:
    """生成 HTML 格式简报，适合邮件推送"""
    today   = date.today().isoformat()
    tops    = _smart_articles()
    stats   = get_stats(days=1)

    cat_html = ""
    if stats["by_category"]:
        cats = " / ".join(f"{html.escape(k)}({v})" for k, v in stats["by_category"].items())
        cat_html = f'<p style="color:#666;font-size:13px">分类：{cats}</p>'

    sections_html = ""
    if not tops:
        sections_html = '<h2 style="font-size:16px;font-weight:500;margin-bottom:12px">今日重点</h2><p style="color:#999">今日暂无高优先级资讯。</p>'
    else:
        for section, articles in _group_articles(tops):
            sections_html += f'<h2 style="font-size:16px;font-weight:500;margin:18px 0 10px">{html.escape(section)}</h2>'
            if not articles:
                sections_html += '<p style="color:#999;font-size:13px">暂无匹配资讯。</p>'
                continue
            sections_html += "\n".join(_html_article(article, i) for i, article in enumerate(articles, 1))

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>资讯简报 · {today}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#111">
  <h1 style="font-size:20px;font-weight:500;border-bottom:2px solid #c96442;padding-bottom:8px">资讯简报 · {today}</h1>
  <p style="color:#666;font-size:14px">今日共收录 <strong>{stats["total"]}</strong> 篇，精选 <strong>{len(tops)}</strong> 篇。</p>
  {cat_html}
  <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
  {sections_html}
  <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
  <p style="color:#999;font-size:11px;text-align:center">由资讯 Agent 自动生成</p>
</body></html>'''


# 向后兼容
def generate():
    return generate_md()


def save_to_file(content: str, fmt: str = "md", output_dir: str = "briefs") -> str:
    """保存简报到文件，支持 md 和 html 格式
    
    Args:
        content: 简报内容
        fmt: 文件格式 "md" 或 "html"
    """
    os.makedirs(output_dir, exist_ok=True)
    ext = "html" if fmt == "html" else "md"
    path = os.path.join(output_dir, f"brief-{date.today().isoformat()}.{ext}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
