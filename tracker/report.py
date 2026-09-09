"""邮件正文渲染 —— 速览表 + 前沿理由 + 趋势判断 + 校验记录。

格式与本地 prompt 第三段定义的正文结构一一对应：
1. 速览表（标题 | 发表 | 论文链接 | 代码链接）
2. 每篇一句"为什么算前沿"
3. 2-3 条趋势判断
4. 校验结果记录（推了几篇、几篇有代码、修正了什么）

所有链接均来自 arXiv 抓取结果，渲染层只做拼装，不产生新 URL。
"""

from __future__ import annotations

import html
from datetime import datetime, timezone


def esc(value: str | None) -> str:
    return html.escape(value or "", quote=True)


def has_official_link(p: dict) -> bool:
    """是否给出官方 GitHub 仓库或经核实的项目主页。"""
    return bool(p.get("code_url") or p.get("project_url"))


def link_cell(p: dict) -> str:
    """速览表『代码』列：GitHub 仓库优先，其次项目主页，都没有则"未开源"。"""
    code = p.get("code_url")
    if code:
        return f'<a href="{esc(code)}">GitHub</a>'
    project = p.get("project_url")
    if project:
        return f'<a href="{esc(project)}">项目主页</a>'
    return "未开源"


def link_text(p: dict) -> str:
    """纯文本版：返回可点击的官方链接；都没有则"未开源"。"""
    code = p.get("code_url")
    if code:
        return code
    project = p.get("project_url")
    if project:
        return f"{project}（项目主页）"
    return "未开源"


def render_html(
    domain: str,
    papers: list[dict],
    trends: list[str],
    verification_note: str,
    today: str | None = None,
    candidates_count: int = 0,
) -> str:
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with_code = sum(1 for p in papers if has_official_link(p))

    # ---- 速览表 ----
    rows = []
    for i, p in enumerate(papers, 1):
        venue = esc(p.get("venue") or "未标注")
        title = esc(p.get("title"))
        abs_url = esc(p.get("abs_url"))
        code_cell = link_cell(p)
        rows.append(
            f"<tr><td>{i}</td><td><a href=\"{abs_url}\">{title}</a></td>"
            f"<td>{venue}</td><td>{code_cell}</td></tr>"
        )
    table = (
        "<table border='1' cellpadding='8' cellspacing='0' "
        "style='border-collapse:collapse;font-size:13px;'>"
        "<tr style='background:#f2f2f2;'><th>#</th><th>论文</th>"
        "<th>发表</th><th>代码</th></tr>" + "".join(rows) + "</table>"
    )

    # ---- 每篇的前沿理由 + 风险提示 ----
    detail_items = []
    for i, p in enumerate(papers, 1):
        why = esc(p.get("why_frontier"))
        risk = p.get("risk_note")
        risk_html = ""
        if risk:
            risk_html = (
                f"<br><span style='color:#b00;'>⚠ {esc(risk)}</span>"
            )
        detail_items.append(
            f"<li><a href='{esc(p['abs_url'])}'>{esc(p['title'])}</a>"
            f"{risk_html}<br>{why}</li>"
        )
    details = "<ol style='padding-left:20px;line-height:1.8;'>" + \
        "".join(detail_items) + "</ol>"

    # ---- 趋势判断 ----
    trend_items = "".join(f"<li>{esc(t)}</li>" for t in trends if t)
    trends_html = (
        f"<ol style='padding-left:20px;line-height:1.8;'>{trend_items}</ol>"
        if trend_items
        else "<p>（本轮无趋势判断输出）</p>"
    )

    # ---- 校验记录 ----
    note = esc(verification_note or "")
    verification = (
        f"<p style='color:#666;font-size:12px;'>{note}</p>"
        if note else ""
    )

    return f"""\
<div style="font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;max-width:760px;margin:0 auto;color:#222;">
<h2 style="border-bottom:2px solid #3b82f6;padding-bottom:8px;">
【{esc(domain)}·周报】{today}</h2>

<p>本周选出 <b>{len(papers)}</b> 篇前沿论文，其中 <b>{with_code}</b> 篇有官方代码/项目链接
（候选池 {candidates_count} 篇，按时间过滤后精选）。</p>

<h3>① 速览</h3>
{table}

<h3>② 为什么算前沿</h3>
{details}

<h3>③ 趋势判断</h3>
{trends_html}

<h3>④ 校验记录</h3>
{verification}
<hr style="border:none;border-top:1px solid #ddd;">
<p style="color:#999;font-size:12px;">
本邮件由热点追踪助手自动生成：论文元数据抓取自 arXiv 官方 API，
链接均经真实请求验证后发出。如不想再收到，请回复本邮件说明。
</p>
</div>"""


def render_text(
    domain: str,
    papers: list[dict],
    trends: list[str],
    verification_note: str,
    today: str | None = None,
    candidates_count: int = 0,
) -> str:
    """纯文本备选体。"""
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with_code = sum(1 for p in papers if has_official_link(p))

    lines = [
        f"【{domain}·周报】{today}",
        f"本周选出 {len(papers)} 篇前沿论文，其中 {with_code} 篇有官方代码/项目链接。",
        "",
        "=== 速览 ===",
    ]
    for i, p in enumerate(papers, 1):
        link = link_text(p)
        lines.append(
            f"{i}. {p['title']}\n   {p.get('venue') or '未标注'}\n"
            f"   论文: {p['abs_url']}\n   代码: {link}"
        )
    lines.append("\n=== 为什么算前沿 ===")
    for i, p in enumerate(papers, 1):
        risk = f" [注意: {p['risk_note']}]" if p.get("risk_note") else ""
        lines.append(f"{i}. {p.get('why_frontier')}{risk}")
    if trends:
        lines.append("\n=== 趋势判断 ===")
        for i, t in enumerate(trends, 1):
            lines.append(f"{i}. {t}")
    if verification_note:
        lines.append(f"\n=== 校验记录 ===\n{verification_note}")
    lines.append(
        f"\n候选池 {candidates_count} 篇。"
        "本邮件由热点追踪助手自动生成，链接均经真实请求验证。"
    )
    return "\n".join(lines)
