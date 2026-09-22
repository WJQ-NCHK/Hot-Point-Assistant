"""流水线编排：抓取 → 去重 → LLM 精选 → 写作 → 渲染 → 发送 → 写状态。

幂等与防丢失的关键顺序（与本地 prompt 的「失败兜底」语义一致）：
    1. 报告先落盘（reports/ 目录），无论后面发不发的出去，成果都在
    2. SMTP 发送成功之后，才把本周键写进 papers_seen.json
    3. 发送失败不会写 week 键 → 下次调度自动补发，但 seen 列表也
       还没追加 → 不会漏推论文（最坏情况：补发时论文集合有小幅变化）
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import llm_client, mailer, prompts, report, state
from .arxiv_client import ArxivError, fetch_recent
from .config import Config
from .llm_client import LLMClient, LLMError
from .mailer import MailError

log = logging.getLogger("tracker")


@dataclass
class RunResult:
    status: str          # sent / skipped / dry_run / error
    detail: str
    picked: int = 0
    with_code: int = 0
    report_path: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _select_papers(cfg: Config, client: LLMClient,
                   candidates: list[dict]) -> tuple[list[dict], int]:
    """LLM 精选环节。返回（选中的论文列表, 被拒数量）。"""
    resp = client.chat_json(
        prompts.SELECT_SYSTEM.format(
            min_picks=cfg.min_picks,
            max_picks=cfg.max_picks,
            domain=cfg.domain_name,
            lookback_days=cfg.lookback_days,
        ),
        prompts.SELECT_USER.format(
            today=_now(), count=len(candidates),
            candidates=prompts.render_candidates(candidates),
        ),
        temperature=0.0,
    )

    by_id = {p["arxiv_id"]: p for p in candidates}
    picked: list[dict] = []
    for item in resp.get("picks", []):
        aid = (item or {}).get("arxiv_id")
        # 只接受候选池里真实存在的 ID，杜绝模型补进清单外的论文
        if aid not in by_id:
            log.warning("LLM 给出的 arxiv_id 不在候选池，忽略: %r", aid)
            continue
        paper = dict(by_id[aid])
        paper["why_frontier"] = (item or {}).get("why_frontier") or ""
        paper["risk_note"] = (item or {}).get("risk_note")
        picked.append(paper)

    # 会议归属：只认 comments 字段解析结果（这里直接取抓取到的 comment）
    for paper in picked:
        paper.setdefault("venue", paper.get("comment") or None)

    rejected = len(resp.get("rejected", []) or [])
    return picked, rejected


def _write_trends(cfg: Config, client: LLMClient,
                  papers: list[dict]) -> tuple[list[str], str]:
    """趋势判断与校验记录。"""
    resp = client.chat_json(
        prompts.WRITE_SYSTEM,
        prompts.WRITE_USER.format(
            today=_now(), domain=cfg.domain_name,
            picks=prompts.render_picks(papers),
        ),
        temperature=0.3,
    )
    trends = [str(t) for t in (resp.get("trends") or []) if str(t).strip()]
    note = str(resp.get("verification_note") or "")
    return trends, note


def run(cfg: Config) -> RunResult:
    """执行一次完整流水线。抛出的异常由调用方决定如何告警。"""
    # ---- 0. 幂等拦截：本周已成功发过 ----
    st = state.load(cfg.state_path)
    wk = state.week_key()
    if not cfg.force and state.week_already_sent(st, wk):
        log.info("幂等拦截：本周(%s)已发送过，跳过。", wk)
        return RunResult("skipped", f"本周 {wk} 已发送过（幂等跳过）")

    # ---- 1. 抓取 ----
    try:
        candidates = fetch_recent(
            query=cfg.arxiv_query,
            max_results=cfg.max_results,
            lookback_days=cfg.lookback_days,
        )
    except ArxivError as exc:
        raise ArxivError(f"抓取失败：{exc}") from exc
    log.info("arXiv 候选 %d 篇（近 %d 天）", len(candidates), cfg.lookback_days)

    # ---- 2. 去重 ----
    seen = state.seen_ids(st)
    fresh = [p for p in candidates if p["arxiv_id"] not in seen]
    dropped = len(candidates) - len(fresh)
    if dropped:
        log.info("去重：%d 篇已推送过，剩余 %d 篇", dropped, len(fresh))

    if not fresh:
        # 本周无新增也发一封信 —— 与本地语义一致：明确说"本周无新增"，
        # 不复述旧论文凑数。这样用户能确认任务确实活着。
        html_body = report.render_html(
            cfg.domain_name, [], [],
            "本周无新增候选论文（arXiv 检索近 %d 天无新结果），按约定不推送旧论文。"
            % cfg.lookback_days,
            candidates_count=0,
        )
        text_body = report.render_text(
            cfg.domain_name, [], [],
            "本周无新增候选论文，按约定不推送旧论文。",
        )
        return _finish_and_send(cfg, st, wk, [], html_body, text_body,
                                note="本周无新增")

    # ---- 3. LLM 精选 ----
    client = LLMClient(
        base_url=cfg.llm_base_url,
        api_key=cfg.llm_api_key,
        model=cfg.llm_model,
        timeout=cfg.llm_timeout,
    )
    picked, rejected = _select_papers(cfg, client, fresh)
    if not picked:
        raise LLMError("LLM 精选环节未返回任何有效论文（候选 %d 篇）" % len(fresh))
    log.info("LLM 精选 %d 篇（拒 %d）", len(picked), rejected)

    # ---- 4. 趋势写作 ----
    trends, vnote = _write_trends(cfg, client, picked)
    with_code = sum(1 for p in picked if report.has_official_link(p))
    vnote_full = (
        f"本轮抓取候选 {len(candidates)} 篇，去重后 {len(fresh)} 篇，"
        f"精选 {len(picked)} 篇（{with_code} 篇有官方代码或项目链接）。{vnote}"
    )

    # ---- 5. 渲染 ----
    html_body = report.render_html(
        cfg.domain_name, picked, trends, vnote_full,
        candidates_count=len(fresh),
    )
    text_body = report.render_text(
        cfg.domain_name, picked, trends, vnote_full,
        candidates_count=len(fresh),
    )

    return _finish_and_send(cfg, st, wk, picked, html_body, text_body,
                            note=vnote_full)


def _finish_and_send(
    cfg: Config,
    st: dict,
    wk: str,
    papers: list[dict],
    html_body: str,
    text_body: str,
    note: str = "",
) -> RunResult:
    """渲染之后：先落盘报告，再发信，最后写状态（顺序即幂等边界）。"""
    today = _now()
    report_dir = Path(cfg.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"weekly_{today}.html"
    report_path.write_text(html_body, encoding="utf-8")
    log.info("报告已落盘：%s", report_path)

    subject = f"【{cfg.domain_name}·周报】{today}"

    if cfg.dry_run:
        log.info("DRY-RUN：跳过 SMTP 发送（收件人 %s）",
                 cfg.mail_to or "<未配置>")
        return RunResult("dry_run", f"预览模式：报告已生成未发送。{note}",
                         picked=len(papers),
                         with_code=sum(1 for p in papers if p.get("code_url")),
                         report_path=str(report_path))

    if not (cfg.smtp_user and cfg.smtp_auth_code and cfg.mail_to):
        raise MailError(
            "SMTP 配置不完整（SMTP_USER / SMTP_AUTH_CODE / MAIL_TO），"
            "报告已落盘至 " + str(report_path)
        )

    msg = mailer.build_message(
        subject=subject, html=html_body, text=text_body,
        mail_from=cfg.mail_from, mail_to=cfg.mail_to,
    )
    try:
        mailer.send(
            host=cfg.smtp_host, port=cfg.smtp_port,
            user=cfg.smtp_user, auth_code=cfg.smtp_auth_code,
            message=msg, use_ssl=cfg.smtp_use_ssl,
        )
    except MailError:
        # 发送失败：报告已落盘；week 键不写 → 下次调度自动补发
        log.error("邮件发送失败，报告已保存在 %s，下次调度将自动补发",
                  report_path)
        raise

    # 发送成功才写状态：seen 追加 + week 键
    st = state.mark_sent(st, papers, key=wk)
    state.save(cfg.state_path, st)
    log.info("发送成功，状态已回写（%d 篇入 seen，周键 %s）", len(papers), wk)

    return RunResult(
        "sent", f"已发送 {len(papers)} 篇。{note}",
        picked=len(papers),
        with_code=sum(1 for p in papers if p.get("code_url")),
        report_path=str(report_path),
    )


def main(cfg: Config) -> int:
    """CLI 入口使用的顶层函数，负责日志与退出码。

    退出码约定（供 CI/调度判断；run.py --help 里有同一张表）：
        0 = sent / skipped / dry_run（成功）
        1 = 配置缺失（由 run.py 判定，重试无意义）
        2 = 抓取环节失败（arXiv 限流/拒绝，重试有效）
        3 = LLM 环节失败（鉴权/额度/网络）
        4 = 邮件发送失败（报告已落盘，下次自动补发）
    """
    result: RunResult | None = None
    try:
        result = run(cfg)
    except ArxivError as exc:
        log.error("抓取环节失败（退出码 2，可重试）：%s", exc)
        log.debug(traceback.format_exc())
        return 2
    except LLMError as exc:
        log.error("LLM 环节失败（退出码 3）：%s", exc)
        log.debug(traceback.format_exc())
        return 3
    except MailError as exc:
        log.error("邮件环节失败（退出码 4，报告已落盘）：%s", exc)
        log.debug(traceback.format_exc())
        return 4
    else:
        log.info("结束：%s —— %s", result.status, result.detail)
        return 0


__all__ = ["run", "main", "RunResult"]
