"""SMTP 邮件发送 —— QQ 邮箱适用，同时输出纯文本备选体。

安全与健壮性设计：
- 授权码只从环境变量 / Config 注入，本模块不接触任何配置文件
- SSL(465) / STARTTLS(587) 两种模式都支持
- 失败自动重试（网络抖动很常见），重试间隔递增
- 同一封邮件带 Message-ID，邮件端天然幂等
"""

from __future__ import annotations

import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid


class MailError(RuntimeError):
    """邮件发送失败（含多次重试后仍失败）。"""


def build_message(
    subject: str, html: str, text: str, mail_from: str, mail_to: str
) -> MIMEMultipart:
    """构造 HTML + 纯文本双备选邮件。

    双备选的原因：纯文本兜底能绕开部分客户端对 HTML 的过滤/降级，
    也方便在垃圾箱里快速辨认内容。
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg


def send(
    host: str,
    port: int,
    user: str,
    auth_code: str,
    message: MIMEMultipart,
    use_ssl: bool = True,
    timeout: int = 30,
    attempts: int = 3,
) -> None:
    """发送邮件，带指数退避重试。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            if use_ssl:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(host, port, timeout=timeout,
                                      context=ctx) as smtp:
                    smtp.login(user, auth_code)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=timeout) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                    smtp.login(user, auth_code)
                    smtp.send_message(message)
            return
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            last = exc
            # 认证失败重试无意义，直接抛出
            if isinstance(exc, smtplib.SMTPAuthenticationError):
                raise MailError(f"SMTP 认证失败：{exc}") from exc
            if i < attempts - 1:
                time.sleep(min(2 ** i * 2, 30))
    raise MailError(f"邮件发送失败（重试 {attempts} 次）：{last}")
