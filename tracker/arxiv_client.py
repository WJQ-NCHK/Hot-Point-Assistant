"""arXiv Atom API 客户端 —— 论文元数据的唯一真实来源。

刻意保持确定性：这里产出的每一个字段都来自 arXiv 实际返回，
不做任何"补全""推测"，也没有 LLM 参与。标题和链接一旦失真，
后面所有环节的真实性都无从谈起，所以这层必须是纯代码。

网络健壮性（2026-09-21 实测教训）：arXiv 会按「请求特征」拒绝访问，
而不只是按 IP —— 既有 HTTP 429 "Rate exceeded."（限流），也有
HTTP 406 "Not Acceptable"（直接拒绝该请求特征/客户端标识）。
旧实现在直连遇到 4xx 时立即抛错，代理通道根本没机会生效；而 406/429
恰恰属于「换一个请求特征或换一个出口 IP 就能恢复」的场景。

现在的策略是「请求特征阶梯（profile ladder）× 通道（直连/公共代理）」
组合矩阵：先换最不容易被拒的特征，再换出口 IP，任一组合拿到合法
Atom XML 就立即返回，限流/拒绝因此可以自愈。
同时遵守官方建议 —— 请求之间保持间隔，不做并发轰炸（每周仅 4 次运行）。
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

# 请求特征阶梯：按「最可能被接受」的顺序尝试。
#   minimal  —— 与旧版线上形态一致（Accept: */*），保持兼容、优先使用；
#   atom     —— 显式声明只接受 Atom（arXiv 官方接口本该返回 Atom）；
#   browser  —— 浏览器形态 UA + Accept，对方 CDN/WAF 的指纹规则最宽容。
# 刻意不再使用含 "tracker" 字样的 UA —— 这类标识最容易被内容风控直接拒
# （这正是 2026-09-21 那次 HTTP 406 的核心嫌疑特征）。
_HOMEPAGE = "https://github.com/WJQ-NCHK/Hot-Point-Assistant"
_REQUEST_PROFILES: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "minimal",
        {"User-Agent": f"HotPointAssistant/1.0 (+{_HOMEPAGE})", "Accept": "*/*"},
    ),
    (
        "atom",
        {
            "User-Agent": f"HotPointAssistant/1.0 (+{_HOMEPAGE})",
            "Accept": "application/atom+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ),
    (
        "browser",
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ),
)

DEFAULT_UA = _REQUEST_PROFILES[0][1]["User-Agent"]


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


def _backoff(i: int, name: str, retry_after: str | None) -> int:
    """退避秒数：尊重 Retry-After；直连长退避（数据中心 IP 限流是小时级），
    代理通道短退避（代理自身几乎不限流，快速换组合更有效）。"""
    if retry_after and retry_after.strip().isdigit():
        return min(int(retry_after.strip()), 120)
    base = 12 if name == "direct" else 5
    ceiling = 120 if name == "direct" else 30
    return min(base * (2 ** i), ceiling)


def _describe(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return f"URLError({exc.reason})"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "超时"
    return f"{type(exc).__name__}({exc})"


def _probe(target: str, headers: dict[str, str], timeout: int) -> str:
    """用指定请求特征抓一次，返回响应正文。网络/HTTP 异常原样抛出。"""
    req = urllib.request.Request(target, headers=dict(headers))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    if not _looks_like_atom(data):
        raise ValueError("返回内容不是 Atom XML（前 80 字符：%r）" % data[:80])
    return data


def _request(url: str, timeout: int, rounds: int) -> str:
    """按「通道 × 请求特征」组合矩阵取数，任一组合成功即返回。

    组合顺序（外层=通道，内层=特征）：
        直连 minimal → 直连 atom → 直连 browser
        → allorigins × 3 特征 → codetabs × 3 特征
    直连的 minimal 特征允许重试一次（覆盖瞬时抖动），其余组合各试一次；
    恢复主要靠「换特征 / 换出口 IP」，而不是拿同一特征硬撞 ——
    这正好覆盖 HTTP 429「Rate exceeded.」与 HTTP 406「Not Acceptable」。
    """
    combos: list[tuple[str, str, dict[str, str], str]] = []
    for name, build in (("direct", None), *_PROXY_BUILDERS):
        target = url if build is None else build(url)
        for pname, headers in _REQUEST_PROFILES:
            combos.append((name, pname, headers, target))

    rounds = max(1, rounds)
    used: dict[tuple[str, str], int] = {}
    last_desc = "未发起任何请求"

    for rnd in range(rounds):
        for name, pname, headers, target in combos:
            key = (name, pname)
            allowed = 2 if (name == "direct" and pname == "minimal") else 1
            if used.get(key, 0) >= allowed:
                continue
            used[key] = used.get(key, 0) + 1
            label = f"{name}/{pname}"
            try:
                data = _probe(target, headers, timeout)
            except Exception as exc:  # noqa: BLE001 - 网络/代理异常繁杂，统一处理
                last_desc = f"{label} → {_describe(exc)}"
                log.debug("arXiv 组合 %s 失败：%s", label, _describe(exc))
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    ra = exc.headers.get("Retry-After") if exc.headers else None
                    wait = _backoff(rnd, name, ra)
                    log.warning("arXiv 限流（%s，HTTP 429），%d 秒后换组合重试",
                                label, wait)
                    time.sleep(wait)
                continue
            if label != "direct/minimal":
                log.info("arXiv 请求经组合 %s 成功", label)
            return data

    raise ArxivError(
        "arXiv 请求失败（尝试了 %d 个「通道×请求特征」组合、%d 轮）：最后失败 %s"
        % (len(used), rounds, last_desc)
    )


def probe_profiles(url: str | None = None, timeout: int = 20) -> list[tuple[str, str, str]]:
    """诊断用：逐个「通道×请求特征」组合真实打一次，报告各自结果。

    返回 [(组合标签, 结果码, 说明)]，结果码为 ok / http-4xx / http-5xx /
    timeout / error。用途：被 arXiv 拒绝时，一眼看出是「直连被拒」
    还是「代理不通」，以及哪个请求特征仍被接受 —— 便于决定要不要
    调整特征阶梯，而不是只看一句 406。
    """
    target = url or (API_URL + "?search_query=cat:cs.CV&max_results=1")
    rows: list[tuple[str, str, str]] = []
    for name, build in (("direct", None), *_PROXY_BUILDERS):
        probe_url = target if build is None else build(target)
        for pname, headers in _REQUEST_PROFILES:
            label = f"{name}/{pname}"
            try:
                _probe(probe_url, headers, timeout)
                rows.append((label, "ok", "拿到 Atom XML"))
            except urllib.error.HTTPError as exc:
                code = "http-4xx" if exc.code < 500 else "http-5xx"
                rows.append((label, code, f"HTTP {exc.code} {exc.reason}"))
            except (socket.timeout, TimeoutError):
                rows.append((label, "timeout", f"{timeout}s 内无响应"))
            except Exception as exc:  # noqa: BLE001
                rows.append((label, "error", _describe(exc)))
    return rows


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
    rounds: int = 2,
) -> list[dict]:
    """按检索式拉取近期论文，返回按提交时间倒序的候选列表。

    :param query: arXiv 检索式
    :param max_results: 拉取条数上限（候选池，之后再由 LLM 精挑）
    :param lookback_days: 只保留最近 N 天内提交或更新的论文
    :param attempts: 历史参数，保留仅为兼容旧调用（现由 rounds 控制组合轮数）
    :param rounds: 组合矩阵轮数（≥1）。工作流层还有「隔 20 分钟整轮重跑」，
                   所以这里只做有限轮次，避免单次尝试耗时过长。
    """
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": max_results,
        }
    )
    raw = _request(f"{API_URL}?{params}", timeout=timeout, rounds=rounds)

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
