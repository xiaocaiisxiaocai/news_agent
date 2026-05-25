# ============================================================
# outputs/brief.py —— 每日简报（Markdown + HTML）
# ============================================================

import os
from datetime import date
from storage import get_today_top, get_stats
import config_store as cfg


def generate_md() -> str:
    """生成 Markdown 格式简报"""
    n       = cfg.get_int("brief.top_n", 5)
    min_imp = cfg.get_int("brief.min_importance", 3)
    today   = date.today().isoformat()
    tops    = get_today_top(n=n, min_importance=min_imp)
    stats   = get_stats(days=1)

    lines = [f"# 资讯简报 · {today}", ""]
    total = stats["total"]
    lines.append(f"今日共收录 **{total}** 篇，精选 **{len(tops)}** 篇。")

    if stats["by_category"]:
        cats = "、".join(f"{k} {v}" for k, v in stats["by_category"].items())
        lines += ["", f"**分类**：{cats}", ""]

    lines += ["---", "## 今日精选", ""]

    if not tops:
        lines.append("> 今日暂无高优先级资讯。")
    else:
        for i, a in enumerate(tops, 1):
            stars  = "★" * a["importance"] + "☆" * (5 - a["importance"])
            title  = a.get("title","")
            url    = a.get("url","")
            title_md = f"[{title}]({url})" if url else title
            lines.append(f"### {i}. {title_md}")
            lines.append(f"{stars}  `{a.get('category','')}`")
            lines.append("")
            if a.get("conclusion"):
                lines.append(f"> {a['conclusion']}")
                lines.append("")
            if a.get("points"):
                for p in a["points"].split("\n"):
                    if p.strip():
                        lines.append(f"- {p.strip()}")
                lines.append("")

    return "\n".join(lines)


def generate_html() -> str:
    """生成 HTML 格式简报，适合邮件推送"""
    n       = cfg.get_int("brief.top_n", 5)
    min_imp = cfg.get_int("brief.min_importance", 3)
    today   = date.today().isoformat()
    tops    = get_today_top(n=n, min_importance=min_imp)
    stats   = get_stats(days=1)

    cat_html = ""
    if stats["by_category"]:
        cats = " / ".join(f"{k}({v})" for k, v in stats["by_category"].items())
        cat_html = f'<p style="color:#666;font-size:13px">分类：{cats}</p>'

    items_html = ""
    if not tops:
        items_html = '<p style="color:#999">今日暂无高优先级资讯。</p>'
    else:
        for i, a in enumerate(tops, 1):
            stars = "★" * a["importance"] + "☆" * (5 - a["importance"])
            title = a.get("title", "")
            url = a.get("url", "")
            title_html = f'<a href="{url}" style="color:#1a6fb8;text-decoration:none">{title}</a>' if url else title
            conclusion = f'<p style="color:#555;font-size:14px;margin:6px 0">{a["conclusion"]}</p>' if a.get("conclusion") else ""
            points_html = ""
            if a.get("points"):
                pts = "\n".join(f'<li style="color:#555;font-size:13px">{p.strip()}</li>'
                               for p in a["points"].split("\n") if p.strip())
                points_html = f'<ul style="margin:4px 0;padding-left:20px">{pts}</ul>'
            items_html += f'''
            <div style="margin:16px 0;padding:12px 16px;border-left:3px solid #c96442;background:#fafaf7;border-radius:4px">
              <h3 style="margin:0 0 4px;font-size:15px">{i}. {title_html}</h3>
              <span style="color:#b87a14;font-size:12px">{stars}</span>
              <span style="color:#999;font-size:12px;margin-left:8px">{a.get("category","")}</span>
              {conclusion}
              {points_html}
            </div>'''

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>资讯简报 · {today}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#111">
  <h1 style="font-size:20px;font-weight:500;border-bottom:2px solid #c96442;padding-bottom:8px">资讯简报 · {today}</h1>
  <p style="color:#666;font-size:14px">今日共收录 <strong>{stats["total"]}</strong> 篇，精选 <strong>{len(tops)}</strong> 篇。</p>
  {cat_html}
  <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
  <h2 style="font-size:16px;font-weight:500;margin-bottom:12px">今日精选</h2>
  {items_html}
  <hr style="border:none;border-top:1px solid #eee;margin:16px 0">
  <p style="color:#999;font-size:11px;text-align:center">由资讯 Agent 自动生成</p>
</body></html>'''


# 向后兼容
def generate():
    return generate_md()


def save_to_file(content: str, fmt: str = "md") -> str:
    """保存简报到文件，支持 md 和 html 格式
    
    Args:
        content: 简报内容
        fmt: 文件格式 "md" 或 "html"
    """
    os.makedirs("briefs", exist_ok=True)
    ext = "html" if fmt == "html" else "md"
    path = f"briefs/brief-{date.today().isoformat()}.{ext}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path
