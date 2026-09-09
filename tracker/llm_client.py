"""LLM 客户端 —— OpenAI 兼容协议，零第三方依赖实现。

设计原则：模型只做「挑选」和「写作」，不做「提供事实」。
候选论文的字段（标题/链接/会议）全部由代码注入并在提示词里
明确禁止改写，因此模型即使幻觉也没有污染数据的出口。

健壮性：
- JSON 输出容错解析（剥掉 ```json 围栏、截取首个完整对象）
- 网络错误指数退避重试
- 温度分环节：挑选 0（确定性判断），写作 0.3（保留表达多样性）
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    """LLM 调用失败或输出无法解析。"""


def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象 —— 容忍围栏、前后缀说明文字。"""
    cleaned = JSON_FENCE_RE.sub("", text.strip())
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 兜底：截取第一个 { 到与之配平的 } 之间的内容
    depth = 0
    for i, ch in enumerate(cleaned):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[: i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    break
    raise LLMError("LLM 输出中未找到可解析的 JSON 对象")


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 120, attempts: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.attempts = attempts

    def chat(self, system: str, user: str, temperature: float = 0.0) -> str:
        """单轮对话。挑选环节 temperature=0，写作环节 0.3。"""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        last: Exception | None = None
        for i in range(self.attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                data = json.loads(raw)
                content = data["choices"][0]["message"]["content"]
                if not content:
                    raise LLMError("LLM 返回空 content")
                return content
            except Exception as exc:  # noqa: BLE001
                last = exc
                # 4xx 属于请求本身的问题（鉴权/参数），重试无意义
                if isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                    detail = ""
                    try:
                        detail = exc.read().decode("utf-8", errors="replace")[:200]
                    except Exception:  # noqa: BLE001
                        pass
                    raise LLMError(f"LLM HTTP {exc.code}: {detail}") from exc
                if i < self.attempts - 1:
                    time.sleep(min(2 ** i * 2, 30))

        raise LLMError(f"LLM 调用失败（重试 {self.attempts} 次）：{last}")

    def chat_json(self, system: str, user: str,
                  temperature: float = 0.0) -> dict:
        """调用并要求解析为 JSON。"""
        return extract_json(self.chat(system, user, temperature))
