"""arXiv Atom API 客户端 —— 论文元数据的唯一真实来源。

刻意保持确定性：这里产出的每一个字段都来自 arXiv 实际返回，
不做任何"补全""推测"，也没有 LLM 参与。标题和链接一旦失真，
后面所有环节的真实性都无从谈起，所以这层必须是纯代码。

网络健壮性：arXiv 偶发 5xx / 超时 / 429 限流，采用指数退避重试
（429 是数据中心 IP 常态，退避更长并尊重 Retry-After 头）；
同时遵守官方建议 —— 单次请求后留出间隔，不做并发轰炸。
"""

from __future__ import annotations

import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

API_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"

# 官方 GitHub 代码仓库地址
CODE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+"
)
# 作者自建项目主页（*.github.io / 作者域名 / 机构官网）。
# 兼容两类写法：带 scheme 的完整 URL，以及 arXiv 摘要纯文本里常见的
# 裸域名 *.github.io/...（HTML 渲染为纯文本时常被剥掉 https:// 前缀）。
# *.github.io 是极强信号，可安全在无 scheme 时也匹配。
PROJECT_URL_RE = re.compile(
    r"(?:https?://)?(?:\w[\w.-]*\.)?(?:[\w-]+\.github\.io|[\w-]+\.github\.com/[\w.-]+)/[^\s<>\"']*"
)
_TAIL_PUNCT = ".,;:)]}、。，）】》"
_PROJECT_CTX = ("project page", "project website", "code is available", "code:",
                "code available", "website", "homepage", "源代码", "项目主页", "项目页")

DEFAULT_UA = "hotspot-tracker/1.0 (research digest script)"


def _clean_url(url: str) -> str:
    while url and url[-1] in _TAIL_PUNCT:
        url = url[:-1]
    # 只去掉「结尾的」.git 后缀（如 foo.git）；用 endswith 避免误伤
    # github.io / .github.com 这类含 .git 子串的域名
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/")


class ArxivError(RuntimeError):
    """抓取失败（含多次重试后仍失败）。"""


def _strip(text: str | None) -> str:
    return " ".join((text or "").split())


def _extract_code_url(*sources: str | None) -> str | None:
    """从 comments / summary 里提取官方 GitHub 代码仓库链接。

    只认作者在 arXiv comments / 摘要里明确给出的 github.com 仓库。
    找不到就返回 None —— 由上层落成「未开源」。宁可漏，不可猜。
    """
    for src in sources:
        if not src:
            continue
        m = CODE_URL_RE.search(src)
        if m:
            url = _clean_url(m.group(0))
            if not url.startswith("http"):
                url = "https://" + url
            return url
    return None


def _extract_project_url(*sources: str | None) -> str | None:
    """提取官方项目主页链接（*.github.io 项目页）。

    只有当该 URL 邻近 'project page'/'code'/'website'/'homepage' 等
    语境词时才采纳，避免把摘要里顺带提的无关链接当项目页。
    arXiv 纯文本里 *.github.io 常不带 scheme，这里自动补 https://。
    """
    for src in sources:
        if not src:
            continue
        low = src.lower()
        for m in PROJECT_URL_RE.finditer(src):
            url = _clean_url(m.group(0))
            if not url.startswith("http"):
                url = "https://" + url
            # 已是 github.com 仓库的（无论有无 scheme），不必当项目页
            if "github.com/" in url:
                continue
            start = max(0, m.start() - 80)
            if any(mark in low[start:m.end()] for mark in _PROJECT_CTX):
                return url
    return None


def _parse_entry(entry: ET.Element) -> dict | None:
    raw_id = _strip(entry.findtext(f"{ATOM}id"))
    if "/abs/" not in raw_id:
        return None
    arxiv_id = raw_id.split("/abs/")[-1]
    # 去掉版本号，预印本与正式版靠 ID 归并，避免重复计数
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id)

    published = _strip(entry.findtext(f"{ATOM}published"))
    updated = _strip(entry.findtext(f"{ATOM}updated"))
    if not published:
        return None

    authors = [
        _strip(a.findtext(f"{ATOM}name"))
        for a in entry.findall(f"{ATOM}author")
    ]
    authors = [a for a in authors if a][:6]

    comment = _strip(entry.findtext(f"{ARXIV}comment"))
    summary = _strip(entry.findtext(f"{ATOM}summary"))

    return {
        "arxiv_id": arxiv_id,
        "title": _strip(entry.findtext(f"{ATOM}title")),
        "summary": summary,
        "authors": ", ".join(authors),
        "published": published,
        "updated": updated,
        "comment": comment,
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "code_url": _extract_code_url(comment, summary),
        "project_url": _extract_project_url(comment, summary),
    }


def _request(url: str, timeout: int, attempts: int) -> str:
    last: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": DEFAULT_UA}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - 网络异常类型繁杂，统一退避重试
            last = exc
            wait: int | None = None
            if isinstance(exc, urllib.error.HTTPError):
                # 429(限流)与5xx属于「稍后重试有效」；其余4xx(404/403等)
                # 是请求本身的问题，重试无意义，直接放弃
                if exc.code != 429 and exc.code < 500:
                    raise ArxivError(f"arXiv 返回 HTTP {exc.code}") from exc
                if exc.code == 429 and exc.headers:
                    # 限流响应可能带 Retry-After，优先尊重官方指示
                    ra = exc.headers.get("Retry-After")
                    if ra and ra.strip().isdigit():
                        wait = min(int(ra.strip()), 120)
            if i < attempts - 1:
                if wait is None:
                    # 429 多发生在数据中心 IP 段（GitHub Actions runner 常见），
                    # 通常需要数十秒级退避才放行；5xx/网络抖动短退避即可
                    base = 10 if (
                        isinstance(exc, urllib.error.HTTPError) and exc.code == 429
                    ) else 2
                    wait = min(base * (2 ** i), 120)
                time.sleep(wait)
    raise ArxivError(f"arXiv 请求失败（重试 {attempts} 次）：{last}")


def _parse_published(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def fetch_recent(
    query: str,
    max_results: int = 60,
    lookback_days: int = 30,
    timeout: int = 45,
    attempts: int = 6,
) -> list[dict]:
    """按检索式拉取近期论文，返回按提交时间倒序的候选列表。

    :param query: arXiv 检索式
    :param max_results: 拉取条数上限（候选池，之后再由 LLM 精挑）
    :param lookback_days: 只保留最近 N 天内提交或更新的论文
    """
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        }
    )
    raw = _request(f"{API_URL}?{params}", timeout=timeout, attempts=attempts)

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ArxivError(f"arXiv 返回内容无法解析：{exc}") from exc

    # arXiv 的时间戳是 UTC
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    papers: list[dict] = []
    seen_ids: set[str] = set()
    for entry in root.findall(f"{ATOM}entry"):
        item = _parse_entry(entry)
        if not item or item["arxiv_id"] in seen_ids:
            continue
        dt = _parse_published(item["published"])
        if dt and dt < cutoff:
            continue
        seen_ids.add(item["arxiv_id"])
        papers.append(item)

    papers.sort(key=lambda p: p["published"], reverse=True)
    return papers
