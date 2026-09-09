# -*- coding: utf-8 -*-
"""端到端 mock 测试：真实抓取 + mock LLM + dry-run。

验证点：
1. 幻觉 ID（不在候选池）被剔除
2. 报告落盘且含全部选中论文的真实 arxiv_id
3. dry-run 不写状态文件（幂等边界正确）
4. 趋势判断与校验记录渲染进报告
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, ".")

from tracker import pipeline
from tracker.arxiv_client import fetch_recent
from tracker.config import Config

QUERY = 'cat:cs.CV AND abs:"3D Gaussian splatting"'
real = fetch_recent(QUERY, max_results=12, lookback_days=30)
assert len(real) >= 8, f"抓取候选不足: {len(real)}"


class FakeLLM:
    def __init__(self, *a, **k):
        pass

    def chat_json(self, system, user, temperature=0.0):
        if "挑" in system:
            ids = [p["arxiv_id"] for p in real[:8]]
            picks = [
                {"arxiv_id": i, "why_frontier": "测试理由：近一周新增且方法有实质推进",
                 "risk_note": None}
                for i in ids
            ]
            picks.append({"arxiv_id": "9999.99999", "why_frontier": "幻觉测试：不在候选池"})
            return {"picks": picks, "rejected": [{"arxiv_id": "x", "reason": "测试"}]}
        return {
            "trends": ["测试趋势 1：mock 管道验证", "测试趋势 2：字段原样传递"],
            "verification_note": "mock 校验记录",
        }


pipeline.LLMClient = FakeLLM

tmp = pathlib.Path(tempfile.mkdtemp())
cfg = Config.from_env(
    state_path=str(tmp / "papers_seen.json"),
    report_dir=str(tmp / "reports"),
    dry_run=True,
    force=True,
    max_picks=10,
)

r = pipeline.run(cfg)
print("status      :", r.status)
print("picked      :", r.picked, "(期望 8，幻觉 ID 应被剔除)")
print("with_code   :", r.with_code)
print("report 落盘 :", r.report_path, "存在:", pathlib.Path(r.report_path).exists())

state_file = tmp / "papers_seen.json"
print("state 文件  :", "未写(正确，dry-run 不写状态)" if not state_file.exists() else "被写了(错误)")

html = pathlib.Path(r.report_path).read_text(encoding="utf-8")
ids = [p["arxiv_id"] for p in real[:8]]
print("8 个 arxiv_id 均出现在报告:", all(i in html for i in ids))
print("报告含趋势判断:", "测试趋势 1" in html)
print("报告含校验记录:", "mock 校验记录" in html)
print("报告大小:", len(html) // 1024, "KB")

assert r.picked == 8, "幻觉 ID 未被剔除"
assert not state_file.exists(), "dry-run 不应写状态"
assert all(i in html for i in ids), "报告缺少真实 arxiv_id"
print("\n全部断言通过")
