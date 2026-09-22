"""命令行入口 —— 本地调试与云端调度共用同一个入口。

本地用法示例：
    # 干跑：只抓取 + 生成报告，不发信，不需要 SMTP 配置
    python run.py --dry-run

    # 完整跑：需要 .env 或环境变量提供 SMTP_USER / SMTP_AUTH_CODE /
    # MAIL_TO / LLM_API_KEY
    python run.py

    # 本周已发但想再发一次（忽略幂等拦截）
    python run.py --force

    # 只抓取，跳过 LLM 与发送（快速验证抓取链路）
    python run.py --fetch-only

    # 诊断 arXiv 连通性：逐组合试「直连/代理 × 请求特征」，不发信
    python run.py --probe-arxiv

参数会覆盖环境变量中的同名配置 —— 便于临时调整而不动配置文件。

退出码约定（云端工作流据此区分原因，不要再靠猜）：
    0 = 发送成功 / 幂等跳过 / dry-run 完成
    1 = 必要配置缺失（缺 Secret 或 .env 未填）—— 重试无意义，立即失败
    2 = arXiv 抓取失败（限流/拒绝）—— 属于可重试的瞬时故障
    3 = LLM 调用失败（鉴权/额度/网络）
    4 = 邮件发送失败（报告已落盘，下次调度自动补发）
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracker.arxiv_client import (  # noqa: E402
    ArxivError,
    fetch_recent,
    probe_profiles,
)
from tracker.config import Config, load_dotenv  # noqa: E402
from tracker.pipeline import main as pipeline_main  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="hotspot-tracker",
        description="热点追踪助手：抓取前沿论文并发送周报邮件",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="只生成报告不发送，无需 SMTP 配置")
    p.add_argument("--force", action="store_true",
                   help="忽略「本周已发送」幂等拦截")
    p.add_argument("--fetch-only", action="store_true",
                   help="只验证抓取链路，不调用 LLM 不发信")
    p.add_argument("--probe-arxiv", action="store_true",
                   help="只诊断 arXiv 连通性（逐组合打印状态），不发信")
    p.add_argument("--max-results", type=int, default=None,
                   help="arXiv 候选池大小（默认 60）")
    p.add_argument("--lookback-days", type=int, default=None,
                   help="只看最近 N 天（默认 30）")
    p.add_argument("--state", default=None,
                   help="状态文件路径（默认 .workbuddy/papers_seen.json）")
    p.add_argument("--report-dir", default=None,
                   help="报告落盘目录（默认 reports）")
    p.add_argument("--env-file", default=".env",
                   help="环境变量文件路径（默认 .env）")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="输出调试日志")
    return p.parse_args(argv)


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv(args.env_file)

    cfg = Config.from_env(
        dry_run=args.dry_run or None,
        force=args.force or None,
        max_results=args.max_results,
        lookback_days=args.lookback_days,
        state_path=args.state,
        report_dir=args.report_dir,
    )

    if args.probe_arxiv:
        print("arXiv 连通性诊断（通道/请求特征 → 结果）：")
        rows = probe_profiles()
        for label, code, detail in rows:
            print(f"  {label:22s} {code:9s} {detail}")
        ok = [r for r in rows if r[1] == "ok"]
        print(f"-- 可用组合 {len(ok)}/{len(rows)}")
        return 0 if ok else 2

    if args.fetch_only:
        try:
            papers = fetch_recent(
                query=cfg.arxiv_query,
                max_results=cfg.max_results,
                lookback_days=cfg.lookback_days,
            )
        except ArxivError as exc:
            logging.getLogger("tracker").error("抓取失败：%s", exc)
            return 2
        for p in papers[:10]:
            print(f"{p['published'][:10]}  {p['arxiv_id']:13s}  "
                  f"{(p['title'] or '')[:70]}")
        print(f"-- 共 {len(papers)} 篇候选")
        return 0

    missing = cfg.missing_required()
    if missing:
        log = logging.getLogger("tracker")
        log.error("缺少必要配置（退出码 1，重试无意义）：%s", "、".join(missing))
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        print("请通过环境变量或 .env 提供（参见 .env.example）；"
              "GitHub Actions 请在仓库 Settings → Secrets and variables → "
              "Actions 中补齐同名 Secret。", file=sys.stderr)
        # 脱敏快照：只显示「已设置(长度) / 未设置」，便于一眼看出是哪个 Secret 空
        snapshot = {
            "SMTP_USER": cfg.smtp_user,
            "SMTP_AUTH_CODE": cfg.smtp_auth_code,
            "MAIL_TO": cfg.mail_to,
            "LLM_API_KEY": cfg.llm_api_key,
            "LLM_BASE_URL": cfg.llm_base_url,
            "SMTP_HOST": cfg.smtp_host,
            "SMTP_PORT": str(cfg.smtp_port),
        }
        for key, value in snapshot.items():
            value = str(value or "")
            state = f"已设置({len(value)}字符)" if value else "未设置"
            print(f"  {key:16s} {state}", file=sys.stderr)
        return 1

    return pipeline_main(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
