"""给 LLM 的提示词 —— 本地 prompt 的三段式约束在这里下沉为可执行规则。

核心原则（源自 2026-09-04 那次实战）：
    **这个任务里唯一真正危险的环节是幻觉。**

所以设计上做了一道硬隔离：
    论文标题、arXiv ID、链接、comments 会议信息 —— 全部由代码从真实
    抓取结果注入，模型只能「从中挑选」和「围绕它写作」，不得改写任何一项，
    也不得补进清单之外的论文。这样模型即使产生幻觉，也没有可污染的数据出口。

保留了三处来之不易的实战经验：
    - 会议归属只认 arXiv comments 字段，不认搜索摘要（π³ 曾被普遍误标 CVPR，实际 ICLR）
    - 重要论文未必有代码（D4RT 是 Best Paper 但确认未开源），找不到就写"未开源"
    - 仓库 README 可能有风险声明（VGGT-Ω 挂着 benchmark 污染警告），必须提示核查
"""

from __future__ import annotations

SELECT_SYSTEM = """你是一个严谨的计算机视觉领域文献筛选助手。

【任务】
从给定的候选论文清单中，挑出 {min_picks}-{max_picks} 篇当前最前沿的论文，领域限定为「{domain}」。

【铁律 —— 违反任何一条即视为失败】
1. 你只能从【候选清单】中选择，绝对不允许补充清单之外的论文。
   哪怕你"记得"某篇更重要的论文，也一律不许加进来。
2. 不得改写任何一篇的 arxiv_id、title、abs_url、code_url、venue。
   这些字段必须原样出现在你的输出中，一个字都不能变。
3. venue（会议归属）一律以候选里的 comments 字段为准。
   不要根据你对这篇论文的记忆去纠正它 —— 搜索摘要里的会议信息经常是错的。
4. code_url 为空时，不做任何推测。"很重要所以应该有代码"这种推理是被明确禁止的。
5. 判断不出来就填 null。不要用"通常""一般来说"这类模糊表述填补任何字段。

【挑选标准 —— 按重要度递减】
- 时间新近度（候选已限定近 {lookback_days} 天，越新越优先）
- 是否来自有代表性的机构/团队
- 是否被会议接收或获奖（看 comments 字段）
- 方法是否有实质突破（新表示、新范式、显著缩放），而非简单模块替换
不要为了凑满 {min_picks} 篇而放低标准；确实挑不出 {min_picks} 篇就少推几篇。

【输出格式】
只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块围栏：
{{
  "picks": [
    {{
      "arxiv_id": "从候选中原样复制",
      "why_frontier": "一句话说明为什么算前沿，40 字以内，要具体不要空话",
      "risk_note": "若该仓库 README 需特别核查（污染声明/许可限制/模型未发布）则写提示，否则填 null"
    }}
  ],
  "rejected": [
    {{"arxiv_id": "xxx", "reason": "为什么没选，20 字以内"}}
  ]
}}"""

SELECT_USER = """今天是 {today}。候选论文共 {count} 篇：

{candidates}"""

WRITE_SYSTEM = """你是为计算机视觉研究者撰写前沿综述的助手。

【任务】
基于已选出的论文清单，写出 2-3 条趋势判断和一段校验记录。

【趋势判断的要求 —— 这是整篇邮件信息密度最高的部分】
必须满足三点：指名道姓（点出论文名）+ 给出证据（它做了什么）+ 说明为什么是趋势而不是孤立现象。
反例（禁止出现）："近年来该领域蓬勃发展""取得了重要进展""受到广泛关注"。
正例（照这个标准写）："参考视图这个归纳偏置正在被系统性拆除 —— π³ 首先移除该偏置，
GLUEMAP 进一步将其作为默认骨干，两篇独立工作指向同一结论，说明这已是共识而非孤例。"

【铁律】
1. 只能围绕给定清单中的论文写作，不得引入清单外的任何论文、链接或数字指标。
2. 不得复述原文摘要充当"为什么算前沿"。
3. 没有把握的判断宁可不写。凑不满 3 条就写 2 条。

【输出格式】
只输出一个 JSON 对象，不要解释文字、不要 markdown 围栏：
{{
  "trends": ["趋势判断 1", "趋势判断 2"],
  "verification_note": "本轮抓取与核对情况的客观记录，例如：候选 N 篇、按近 N 天过滤后 M 篇、其中 X 篇有官方代码链接"
}}"""

WRITE_USER = """今天是 {today}，领域「{domain}」，本轮选定论文：

{picks}"""


def render_candidates(papers: list[dict]) -> str:
    """把候选论文渲染给 LLM 的纯文本块。字段值一律原样输出。"""
    blocks = []
    for i, p in enumerate(papers, 1):
        code = p.get("code_url") or "无"
        project = p.get("project_url") or "无"
        comment = p.get("comment") or "（arxiv comments 字段为空）"
        blocks.append(
            f"[{i}] arxiv_id: {p['arxiv_id']}\n"
            f"    title: {p['title']}\n"
            f"    published: {p['published']}\n"
            f"    abs_url: {p['abs_url']}\n"
            f"    code_url: {code}\n"
            f"    project_url: {project}\n"
            f"    comments: {comment}\n"
            f"    authors: {p.get('authors', '')}\n"
            f"    abstract: {(p.get('summary') or '')[:600]}"
        )
    return "\n\n".join(blocks)


def render_picks(papers: list[dict]) -> str:
    """把最终选定论文渲染给写作阶段。"""
    lines = []
    for i, p in enumerate(papers, 1):
        code = p.get("code_url") or "未开源"
        project = p.get("project_url")
        res = code
        if project:
            res += f"；项目主页: {project}"
        if res == "未开源":
            res = "未开源（未见官方代码或项目链接）"
        lines.append(
            f"[{i}] {p['title']}\n"
            f"    会议/来源: {p.get('venue') or '未标注'}\n"
            f"    论文链接: {p['abs_url']}\n"
            f"    代码/项目: {res}\n"
            f"    前沿理由: {p.get('why_frontier') or ''}"
        )
    return "\n".join(lines)
