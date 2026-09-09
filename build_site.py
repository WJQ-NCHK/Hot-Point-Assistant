# -*- coding: utf-8 -*-
"""把项目里的 Markdown 文档构建成一个自包含的静态站点：site/index.html

用法：
    python build_site.py

说明：
只输出 site/ 一个目录，不打包 .workbuddy（里面有邮箱地址、发件别名等，
不应当出现在公网）。文档更新后重跑此脚本即可刷新页面内容。
"""
import re
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent
# 注意：不要叫 site/ 或 dist/，某些部署工具会把这类名字当成构建产物目录直接跳过
SITE = ROOT / "web"

DOCS = [
    ("papers", "前沿论文清单", ROOT / "三维重建前沿论文清单_2026-09-04.md"),
    ("prompts", "科研助手提示词包", ROOT / "科研助手提示词包.md"),
]

CSS = """
:root{
  --bg:#0f1115; --panel:#151a23; --panel2:#1b2130; --line:#262d3a;
  --fg:#d8dee9; --muted:#8b93a5; --accent:#58a6ff; --accent-dim:#1f3a5f;
  --ok:#3fb950; --warn:#d29922; --code-bg:#0b0e14;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
  "Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
header{
  border-bottom:1px solid var(--line); background:var(--panel);
  padding:20px 28px 0; position:sticky; top:0; z-index:10;
}
.wrap{max-width:1080px; margin:0 auto; width:100%}
h1.site{font-size:19px; margin:0 0 4px; letter-spacing:.3px}
.sub{color:var(--muted); font-size:13px; margin-bottom:14px}
.tabs{display:flex; gap:6px}
.tab-btn{
  appearance:none; border:1px solid transparent; border-bottom:none;
  background:transparent; color:var(--muted); cursor:pointer;
  font:inherit; font-size:14px; padding:9px 16px; border-radius:8px 8px 0 0;
  transition:.15s;
}
.tab-btn:hover{color:var(--fg); background:var(--panel2)}
.tab-btn.active{
  color:var(--accent); background:var(--bg);
  border-color:var(--line); border-bottom:1px solid var(--bg);
  margin-bottom:-1px;
}
main{padding:32px 28px 96px}
.doc{display:none}
.doc.active{display:block}
.doc > :first-child{margin-top:0}
h1.doc-title{font-size:24px; padding-bottom:12px; border-bottom:1px solid var(--line)}
.doc h2{
  font-size:19px; margin:38px 0 14px; padding-left:11px;
  border-left:3px solid var(--accent);
}
.doc h3{font-size:16px; margin:26px 0 10px; color:#e6ebf5}
.doc h4{font-size:14px; margin:20px 0 8px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px}
p{margin:12px 0}
a{color:var(--accent); text-decoration:none; border-bottom:1px solid rgba(88,166,255,.28)}
a:hover{border-bottom-color:var(--accent)}
ul,ol{padding-left:24px}
li{margin:6px 0}
hr{border:none; border-top:1px solid var(--line); margin:34px 0}
table{
  width:100%; border-collapse:collapse; margin:18px 0; font-size:13.5px;
  display:block; overflow-x:auto; white-space:nowrap;
}
th,td{border:1px solid var(--line); padding:9px 12px; text-align:left; vertical-align:top}
th{background:var(--panel2); color:#e6ebf5; font-weight:600}
tbody tr:nth-child(even){background:rgba(255,255,255,.022)}
tbody tr:hover{background:rgba(88,166,255,.06)}
code{
  background:var(--code-bg); border:1px solid var(--line); border-radius:5px;
  padding:1.5px 5px; font-size:12.5px;
  font-family:"SF Mono",Consolas,"Liberation Mono",Menlo,monospace;
  white-space:pre-wrap;
}
pre{
  background:var(--code-bg); border:1px solid var(--line); border-radius:8px;
  padding:14px 16px; overflow-x:auto; margin:16px 0;
}
pre code{background:none; border:none; padding:0; white-space:pre}
blockquote{
  margin:16px 0; padding:10px 16px; border-left:3px solid var(--accent-dim);
  background:rgba(88,166,255,.05); color:#c3ccdb; border-radius:0 6px 6px 0;
}
blockquote p{margin:6px 0}
strong{color:#f0f4fa}
footer{
  border-top:1px solid var(--line); color:var(--muted); font-size:12.5px;
  padding:20px 28px 40px;
}
footer code{font-size:11.5px}
@media(max-width:640px){
  header{padding:16px 16px 0} main{padding:24px 16px 72px}
  footer{padding:16px} table{font-size:12.5px}
}
"""

JS = """
document.querySelectorAll('.tab-btn').forEach(function(btn){
  btn.addEventListener('click', function(){
    var target = btn.dataset.target;
    document.querySelectorAll('.tab-btn').forEach(function(b){
      b.classList.toggle('active', b === btn);
    });
    document.querySelectorAll('.doc').forEach(function(d){
      d.classList.toggle('active', d.id === 'doc-' + target);
    });
    if (history.replaceState) history.replaceState(null, '', '#' + target);
  });
});
(function(){
  var h = (location.hash || '').replace('#','');
  if (!h) return;
  var btn = document.querySelector('.tab-btn[data-target="' + h + '"]');
  if (btn) btn.click();
})();
"""


URL_RE = re.compile(r"https?://[^\s<>\"']+")
TAIL_PUNCT = ".,;:!?)]}、。，）】》》\"'"


def _short_label(url: str) -> str:
    """把长链接压成短标签，避免表格被 URL 撑爆。"""
    m = re.match(r"https?://(?:www\.)?arxiv\.org/abs/(.+?)$", url)
    if m:
        return "arXiv:" + m.group(1)
    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/?#]+)", url)
    if m:
        return f"GitHub: {m.group(1)}/{m.group(2)}"
    m = re.match(r"https?://([^/#?]+)", url)
    return m.group(1) if m else url


def linkify(html: str) -> str:
    """给正文里的裸 URL 补上链接。

    按标签切片，只处理文本片段，因此不会误改 href 属性。
    """
    out = []
    for i, part in enumerate(re.split(r"(<[^>]+>)", html)):
        if i % 2 == 1:  # 标签本体，原样保留
            out.append(part)
            continue

        def _sub(m: re.Match) -> str:
            url = m.group(0)
            tail = ""
            while url and url[-1] in TAIL_PUNCT:
                tail = url[-1] + tail
                url = url[:-1]
            return f'<a href="{url}">{_short_label(url)}</a>{tail}'

        out.append(URL_RE.sub(_sub, part))
    return "".join(out)


def build_html(key: str, title: str, md_text: str) -> str:
    """把一段 Markdown 渲染成站点正文 HTML（tables / fenced code / toc / 裸链接）。"""
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "toc"],
        extension_configs={"toc": {"permalink": False}},
    )
    return linkify(md.convert(md_text))


def main() -> None:
    SITE.mkdir(parents=True, exist_ok=True)

    missing = [str(p) for _, _, p in DOCS if not p.exists()]
    if missing:
        raise SystemExit("以下文档不存在：\n  " + "\n  ".join(missing))

    tabs, bodies = [], []
    for key, label, path in DOCS:
        body = build_html(key, label, path.read_text(encoding="utf-8"))
        tabs.append(
            f'<button class="tab-btn{" active" if not tabs else ""}" '
            f'data-target="{key}">{label}</button>'
        )
        bodies.append(
            f'<section class="doc{" active" if len(bodies) == 0 else ""}" '
            f'id="doc-{key}"><h1 class="doc-title">{label}</h1>{body}</section>'
        )

    updated = __import__("datetime").date.today().isoformat()
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>热点追踪助手 · 文档中心</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <div class="wrap">
    <h1 class="site">热点追踪助手 · 文档中心</h1>
    <div class="sub">三维重建与新视图合成 · 每周前沿论文速递 &nbsp;|&nbsp; 构建日期 {updated}</div>
    <div class="tabs">{''.join(tabs)}</div>
  </div>
</header>
<main><div class="wrap">{''.join(bodies)}</div></main>
<footer>
  <div class="wrap">
    由 <code>build_site.py</code> 从项目 Markdown 源文件自动生成 ·
    每次更新文档后重跑该脚本并重新部署即可同步线上内容。
  </div>
</footer>
<script>{JS}</script>
</body>
</html>
"""

    out = SITE / "index.html"
    out.write_text(page, encoding="utf-8")
    print(f"生成 {out}  ({len(page.encode('utf-8'))/1024:.1f} KB)")


if __name__ == "__main__":
    main()
