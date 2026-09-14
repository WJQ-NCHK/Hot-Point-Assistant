"""arXiv Atom API 客户端 —— 论文元数据的唯一真实来源。

刻意保持确定性：这里产出的每一个字段都来自 arXiv 实际返回，
不做任何"补全""推测"，也没有 LLM 参与。标题和链接一旦失真，
后面所有环节的真实性都无从谈起，所以这层必须是纯代码。

网络健壮性：arXiv 偶发 5xx / 超时 / 429 限流，采用指数退避重试
（429 是数据中心 IP 常态，退避更长并尊重 Retry-After 头）；
同时遵守官方建议 —— 单次请求后留出间隔，不做并发轰炸。
"""

from __future__ import annotations

import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

log = logging.getLogger("tracker.arxiv")

API_URL = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"

# 公共转发代理（字节透传、免注册）。背景：GitHub Actions 等数据中心 IP
# 会被 arXiv 整段限流（HTTP 429），且一限常是小时级 —— 2026-09-14 实测
# 直连退避 5 分钟仍全程 429。直连失败后依次切换这些代理换出口 IP。
# 返回内容会经 _looks_like_atom 校验 + 上层 XML 解析双重把关，
# 代理返回错误页/垃圾内容时会被当作失败换下一通道，不会污染数据。
_PROXY_BUILDERS = (
    ("allorigins", lambda u: "https://api.allorigins.win/raw?url=" + urllib.parse.quote(u, safe="")),
    ("codetabs", lambda u: "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(u, safe="")),
)

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


def _looks_like_atom(data: str) -> bool:
    """粗校验：arXiv API 的 Atom 响应以 <feed 开头（空结果集也如此）。
    代理通道可能 200 返回错误页，这里把住第一道关。"""
    return "<feed" in data[:2000]


def _request(url: str, timeout: int, attempts: int) -> str:
    # 通道顺序：直连 → 公共代理。直连配满额重试（含 429 长退避），
    # 代理通道各给 3 次短重试；任一通道成功即返回。
    channels: list[tuple[str, str, int]] = [("direct", url, attempts)]
    channels += [(name, build(url), 3) for name, build in _PROXY_BUILDERS]

    last: Exception | None = None
    for name, target, tries in channels:
        for i in range(tries):
            try:
                req = urllib.request.Request(
                    target, headers={"User-Agent": DEFAULT_UA}
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = resp.read().decode("utf-8", errors="replace")
                if not _looks_like_atom(data):
                    raise ValueError(
                        f"通道 {name} 返回内容不是 Atom XML（前 80 字符：{data[:80]!r}）"
                    )
                if name != "direct":
                    log.info("arXiv 请求经由 %s 代理通道成功", name)
                return data
            except Exception as exc:  # noqa: BLE001 - 网络/代理异常繁杂，统一处理
                last = exc
                fatal = False
                wait: int | None = None
                if isinstance(exc, urllib.error.HTTPError):
                    if exc.code == 429:
                        # 限流响应可能带 Retry-After，优先尊重官方指示
                        if exc.headers:
                            ra = exc.headers.get("Retry-After")
                            if ra and ra.strip().isdigit():
                                wait = min(int(ra.strip()), 120)
                        if wait is None and name == "direct":
                            # 直连 429 多为数据中心 IP 段被整段限流，
                            # 需要数十秒级长退避；代理通道短退避即可
                            wait = min(10 * (2 ** i), 120)
                    elif exc.code < 500:
                        # 直连遇 403/404 等：请求本身有问题，换代理无意义
                        if name == "direct":
                            raise ArxivError(f"arXiv 返回 HTTP {exc.code}") from exc
                        # 代理自身 4xx：本通道作废，换下一个
                        fatal = True
                if fatal or i >= tries - 1:
                    if name == "direct" and tries > 1:
                        log.warning("直连 arXiv 失败（%s），切换代理通道", exc)
                    break
                if wait is None:
                    wait = min(2 * (2 ** i), 30)
                time.sleep(wait)
    raise ArxivError(
        f"arXiv 请求失败（直连与 {len(_PROXY_BUILDERS)} 个代理通道均失败）：{last}"
    )


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
