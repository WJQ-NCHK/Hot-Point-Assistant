"""CI 侧候选通道列举：从 GitHub Actions runner 依次探测所有可能的 arXiv 取数路径。

背景：本机（家宽 IP）对所有路径都是 200，但 GitHub Actions 的数据中心 IP
访问 arxiv.org / export.arxiv.org 一律 406 Not Acceptable。因此必须让
runner 自己把候选通道跑一遍，找出哪条路还能走。

用法：ARXIV_EXTRA_URLS='名称=URL;名称=URL' python .tmp_ci_probe.py
"""
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

Q = 'cat:cs.CV AND abs:"3D Gaussian splatting"'
P = urllib.parse.urlencode({
    "search_query": Q, "sortBy": "submittedDate", "sortOrder": "descending",
    "max_results": "5",
})
API = f"https://export.arxiv.org/api/query?{P}"
UA = {"User-Agent": "HotPointAssistant/1.0 (+https://github.com/WJQ-NCHK/Hot-Point-Assistant)",
      "Accept": "*/*"}

DEFAULT_CANDIDATES = [
    ("export-https", API),
    ("export-http", API.replace("https://", "http://")),
    ("apex-https", API.replace("export.arxiv.org", "arxiv.org")),
    ("apex-http", API.replace("https://export.arxiv.org", "http://arxiv.org")),
    ("cn-mirror", API.replace("export.arxiv.org", "cn.arxiv.org")),
    ("s2-api", "https://api.semanticscholar.org/graph/v1/paper/search?query=3D+Gaussian+splatting&limit=5&fields=title,externalIds,publicationDate,abstract,authors"),
    ("jina-relay", "https://r.jina.ai/" + API),
    ("allorigins", "https://api.allorigins.win/raw?url=" + urllib.parse.quote(API, safe="")),
    ("codetabs", "https://api.codetabs.com/v1/proxy?quest=" + urllib.parse.quote(API, safe="")),
    ("corsproxy", "https://corsproxy.io/?" + urllib.parse.quote(API, safe="")),
]


def candidates():
    extra = os.environ.get("ARXIV_EXTRA_URLS", "").strip()
    out = list(DEFAULT_CANDIDATES)
    if extra:
        for chunk in extra.split(";"):
            if "=" in chunk:
                name, url = chunk.split("=", 1)
                out.append((name.strip(), url.strip()))
    return out


def looks_arxiv_atom(body: str) -> bool:
    return "<feed" in body[:3000] and "arxiv.org" in body


def looks_s2(body: str) -> bool:
    return '"total"' in body or '"data"' in body


def probe(name, url):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=dict(UA))
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read(200000).decode("utf-8", "replace")
        dt = time.time() - t0
        atom = looks_arxiv_atom(body)
        s2 = looks_s2(body)
        # 粗解析：arxiv_id 数量
        n_ids = body.count("arxiv.org/abs/") + body.count('"ArXiv":')
        verdict = "USABLE" if (atom or s2) else "not-atom"
        print(f"{name:14s} HTTP {resp.status} {dt:5.1f}s  atom={atom} s2={s2} ids~{n_ids}  -> {verdict}")
        if atom or s2:
            snippet = " ".join(body[:400].split())
            print(f"               head: {snippet[:200]}")
        return verdict == "USABLE"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(200).decode("utf-8", "replace").replace("\n", " ")
        except Exception:  # noqa: BLE001
            pass
        server = (exc.headers.get("server") if exc.headers else None) or "-"
        print(f"{name:14s} HTTP {exc.code}       server={server} body={body[:70]}")
    except (socket.timeout, TimeoutError):
        print(f"{name:14s} TIMEOUT after {time.time() - t0:.1f}s")
    except Exception as exc:  # noqa: BLE001
        print(f"{name:14s} ERR {type(exc).__name__}: {str(exc)[:70]}")
    return False


print(f"probe time (UTC): {datetime.now(timezone.utc).isoformat()}")
print(f"run id: {os.environ.get('GITHUB_RUN_ID', 'local')}")
print(f"{'channel':14s} result")
print("-" * 88)
ok = []
for name, url in candidates():
    if probe(name, url):
        ok.append(name)
print("-" * 88)
print(f"USABLE CHANNELS: {ok or 'NONE'}")
