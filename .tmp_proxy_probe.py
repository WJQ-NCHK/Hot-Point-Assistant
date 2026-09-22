"""CI 侧代理候选枚举：把多个 CORS/内容代理套在 arXiv API 前面逐个试。

背景（2026-09-22 实测于 GitHub Actions runner）：
  直连 arxiv.org / export.arxiv.org / cn.arxiv.org 一律 406 Not Acceptable
  （数据中心 IP 段被拒，与 UA / Accept 无关），
  而 https://api.allorigins.win/raw?url=... 能拿到完整 Atom feed。
因此需要一层「代理阶梯」，并从中挑出稳定可用的几个做主力通道。

用法：
  ARXIV_EXTRA_PROXIES='名称=URL模板;名称=URL模板' python .tmp_proxy_probe.py
模板里 {url} 会被替换成 URL 编码后的 arXiv API 地址。
"""
import os
import socket
import time
import urllib.parse
import urllib.request

TARGET = ("https://export.arxiv.org/api/query?search_query=cat:cs.CV+AND+"
          "abs%3A%223D+Gaussian+splatting%22&sortBy=submittedDate&"
          "sortOrder=descending&max_results=5")
ENC = urllib.parse.quote(TARGET, safe="")
UA = {"User-Agent": "HotPointAssistant/1.0 (+https://github.com/WJQ-NCHK/Hot-Point-Assistant)",
      "Accept": "*/*"}

# 名称 -> URL 模板（{url} = 原始地址，{enc} = URL 编码后的地址）
PROXIES = [
    ("allorigins-raw", "https://api.allorigins.win/raw?url={enc}"),
    ("allorigins-get", "https://api.allorigins.win/get?url={enc}"),
    ("codetabs", "https://api.codetabs.com/v1/proxy?quest={enc}"),
    ("corsproxy-io", "https://corsproxy.io/?{enc}"),
    ("thingproxy", "https://thingproxy.freeboard.io/fetch/{url}"),
    ("whateverorigin", "http://www.whateverorigin.org/get?url={enc}"),
    ("jsonp-afeld", "https://jsonp.afeld.me/?url={enc}"),
    ("textance", "https://api.scraperapi.com/?url={enc}"),
    ("jina-relay", "https://r.jina.ai/{url}"),
    ("direct", "{url}"),
]


def proxies():
    out = list(PROXIES)
    extra = os.environ.get("ARXIV_EXTRA_PROXIES", "").strip()
    if extra:
        for chunk in extra.split(";"):
            if "=" in chunk:
                name, tpl = chunk.split("=", 1)
                out.append((name.strip(), tpl.strip()))
    return out


def looks_atom(body: str) -> bool:
    return "<feed" in body[:3000] and "arxiv.org/abs/" in body


print(f"probe time (UTC): {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
print(f"{'proxy':18s} {'status':10s} {'time':7s} atom  ids  detail")
print("-" * 94)
ok = []
for name, tpl in proxies():
    url = tpl.format(url=TARGET, enc=ENC)
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=dict(UA)), timeout=25) as resp:
            raw = resp.read(400000).decode("utf-8", "replace")
        dt = time.time() - t0
        # /get 这类接口会把内容包在 JSON 字符串里，先粗解一层
        body = raw
        if not looks_atom(body) and '"contents"' in raw:
            import json
            try:
                body = json.loads(raw).get("contents") or raw
            except Exception:  # noqa: BLE001
                pass
        n = body.count("arxiv.org/abs/")
        good = looks_atom(body)
        print(f"{name:18s} HTTP {resp.status:<5d} {dt:5.1f}s  {str(good):5s} {n:<4d} {'USABLE' if good else 'not-atom'}")
        if good:
            ok.append(name)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(120).decode("utf-8", "replace").replace("\n", " ")
        except Exception:  # noqa: BLE001
            pass
        print(f"{name:18s} HTTP {exc.code:<5d} {time.time() - t0:5.1f}s  False 0    {detail[:60]}")
    except (socket.timeout, TimeoutError):
        print(f"{name:18s} TIMEOUT       {time.time() - t0:5.1f}s")
    except Exception as exc:  # noqa: BLE001
        print(f"{name:18s} ERR {type(exc).__name__:9s} {time.time() - t0:5.1f}s  {str(exc)[:50]}")
print("-" * 94)
print(f"USABLE PROXIES: {ok or 'NONE'}")
