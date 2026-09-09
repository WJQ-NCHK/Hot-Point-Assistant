"""papers_seen.json 的读写与幂等控制。

两层防重发：
1. seen 列表 —— 某个 arXiv ID 推过一次，之后永远不再推（与本地版本语义一致）。
2. _runs 里的 ISO 周键（如 2026-W37）—— 本周已经成功发过，哪怕任务被
   调度器重跑、Action 重试，也直接跳过，不会给你的邮箱再塞一封。

写入采用「临时文件 + 原子替换」，避免进程中途被杀导致状态文件损坏。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path


def week_key(d: date | None = None) -> str:
    """ISO 周键，形如 2026-W37 —— 幂等粒度是「每周一封」。"""
    d = d or datetime.now(timezone.utc).date()
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def load(path: str | os.PathLike[str]) -> dict:
    p = Path(path)
    if not p.exists():
        return {"_comment": "已推送论文的 arXiv ID 清单", "_updated": None,
                "seen": [], "_runs": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 状态文件损坏时宁可当作空状态重建，也不要让一次坏文件中断推送
        return {"_comment": "已推送论文的 arXiv ID 清单", "_updated": None,
                "seen": [], "_runs": []}
    if not isinstance(data, dict):
        return {"_comment": "已推送论文的 arXiv ID 清单", "_updated": None,
                "seen": [], "_runs": []}
    data.setdefault("seen", [])
    data.setdefault("_runs", [])
    data.setdefault("_updated", None)
    return data


def save(path: str | os.PathLike[str], data: dict) -> None:
    """原子写入：先写临时文件再替换，进程中途被杀不会留下半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data["_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fd, tmp = tempfile.mkstemp(
        dir=str(p.parent), prefix=p.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def seen_ids(data: dict) -> set[str]:
    return {item.get("arxiv_id") for item in data.get("seen", [])
            if isinstance(item, dict) and item.get("arxiv_id")}


def week_already_sent(data: dict, key: str | None = None) -> bool:
    return (key or week_key()) in data.get("_runs", [])


def mark_sent(data: dict, papers: list[dict], key: str | None = None) -> dict:
    """发送成功后调用：追加 seen 条目 + 记录本周已发。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = seen_ids(data)
    for p in papers:
        aid = p.get("arxiv_id")
        if aid and aid not in existing:
            data["seen"].append({
                "arxiv_id": aid,
                "title": p.get("title") or "",
                "pushed_at": today,
            })
            existing.add(aid)
    k = key or week_key()
    if k not in data.get("_runs", []):
        data.setdefault("_runs", []).append(k)
    return data
