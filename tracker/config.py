"""配置加载：环境变量优先，本地调试可用 .env。

优先级（由高到低）：进程环境变量 > .env 文件 > 内置默认值。
之所以是一道基础性改动：所有敏感信息（SMTP 授权码、LLM API Key）必须
走环境变量或 CI Secrets，绝不能写进代码或提交到 Git。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# arXiv 检索式：命中「三维重建与新视图合成」及其主要子方向。
# 关键词取自 abs 字段而非全字段，避免把只是提到该词的论文捞进来。
DEFAULT_QUERY = (
    'cat:cs.CV AND ('
    'abs:"3D reconstruction" OR abs:"novel view synthesis" OR '
    'abs:"3D Gaussian splatting" OR abs:"neural radiance field" OR '
    'abs:"feed-forward reconstruction" OR abs:"structure from motion" OR '
    'abs:"image-to-3D" OR abs:"dynamic scene reconstruction")'
)


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """极简 .env 解析器，只为本地调试方便，避免引入 python-dotenv 依赖。

    已存在的环境变量不会被覆盖 —— CI 注入的真值优先级最高。
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Config:
    """一次运行所需的全部配置。frozen 防止运行中被意外改写。"""

    # ---- 邮件投递 ----
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_user: str = ""          # QQ 邮箱地址
    smtp_auth_code: str = ""     # 授权码，不是登录密码
    smtp_use_ssl: bool = True    # 465=SSL；改 587 请同时置 False
    mail_to: str = ""            # 收件人
    mail_from: str = ""          # 留空则取 smtp_user

    # ---- LLM ----
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-chat"
    llm_timeout: int = 120

    # ---- 检索 ----
    domain_name: str = "三维重建与新视图合成"
    arxiv_query: str = DEFAULT_QUERY
    lookback_days: int = 30      # 只看最近 N 天提交/更新的论文
    max_results: int = 60        # arXiv 候选池大小
    min_picks: int = 5
    max_picks: int = 10

    # ---- 运行 ----
    state_path: str = ".workbuddy/papers_seen.json"
    report_dir: str = "reports"  # 无论发送成功与否都会落盘，防止成果静默丢失
    dry_run: bool = False
    force: bool = False          # 忽略「本周已发」的幂等拦截

    def __post_init__(self) -> None:
        if not self.mail_from:
            object.__setattr__(self, "mail_from", self.smtp_user)

    @classmethod
    def from_env(cls, **overrides: object) -> "Config":
        base: dict[str, object] = dict(
            smtp_host=_env("SMTP_HOST", "smtp.qq.com"),
            smtp_port=_env_int("SMTP_PORT", 465),
            smtp_user=_env("SMTP_USER"),
            smtp_auth_code=_env("SMTP_AUTH_CODE"),
            smtp_use_ssl=_env_bool("SMTP_USE_SSL", True),
            mail_to=_env("MAIL_TO"),
            mail_from=_env("MAIL_FROM"),
            llm_api_key=_env("LLM_API_KEY") or _env("DEEPSEEK_API_KEY"),
            llm_base_url=_env("LLM_BASE_URL", "https://api.deepseek.com"),
            llm_model=_env("LLM_MODEL", "deepseek-chat"),
            llm_timeout=_env_int("LLM_TIMEOUT", 120),
            domain_name=_env("DOMAIN_NAME", "三维重建与新视图合成"),
            arxiv_query=_env("ARXIV_QUERY") or DEFAULT_QUERY,
            lookback_days=_env_int("LOOKBACK_DAYS", 30),
            max_results=_env_int("MAX_RESULTS", 60),
            min_picks=_env_int("MIN_PICKS", 5),
            max_picks=_env_int("MAX_PICKS", 10),
            state_path=_env("STATE_PATH", ".workbuddy/papers_seen.json"),
            report_dir=_env("REPORT_DIR", "reports"),
            dry_run=_env_bool("DRY_RUN", False),
            force=_env_bool("FORCE", False),
        )
        # 命令行显式传入的才覆盖；None 表示「没传」
        for k, v in overrides.items():
            if v is not None:
                base[k] = v
        return cls(**base)  # type: ignore[arg-type]

    def missing_required(self) -> list[str]:
        """返回缺失的必要配置。dry-run 时放宽 —— 只要能抓能生成即可。"""
        missing: list[str] = []
        if not self.llm_api_key:
            missing.append("LLM_API_KEY（或 DEEPSEEK_API_KEY）")
        if self.dry_run:
            return missing
        for name, value in (
            ("SMTP_USER", self.smtp_user),
            ("SMTP_AUTH_CODE", self.smtp_auth_code),
            ("MAIL_TO", self.mail_to),
        ):
            if not value:
                missing.append(name)
        return missing

    def masked(self) -> dict[str, object]:
        """用于日志打印的脱敏视图 —— 凭据绝不进日志。"""
        d: dict[str, object] = dict(self.__dict__)
        for k in ("smtp_auth_code", "llm_api_key"):
            v = str(d.get(k) or "")
            d[k] = ("已设置(%d字符)" % len(v)) if v else "未设置"
        return d
