"""热点追踪助手 · 云端/本地共用的核心逻辑。

设计要点：
1. 零第三方依赖 —— 只用标准库（urllib / smtplib / xml.etree），
   省掉 CI 里的安装步骤，也省掉本地虚拟环境。
2. 确定性事实由代码提供，LLM 只做判断与写作 —— 论文标题、链接、
   arXiv comments 全部来自真实抓取，模型不许改写，从源头掐断幻觉。
"""

from __future__ import annotations

__version__ = "1.0.0"
