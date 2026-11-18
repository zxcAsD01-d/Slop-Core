import json
import socket
from dataclasses import dataclass
from typing import Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .errors import ModelError


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str

    def endpoint(self) -> str:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ModelError("LLM 地址必须是 http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError("LLM 地址不能包含账号、查询参数或片段")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ModelError("远程 LLM 地址必须使用 HTTPS；本地服务可使用 HTTP")
        if not self.model.strip():
            raise ModelError("请填写分析模型名称")
        base = self.base_url.rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def complete(self, messages: List[Dict[str, str]]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = "Bearer " + self.config.api_key
        body = json.dumps({"model": self.config.model, "messages": messages}, ensure_ascii=False).encode()
        request = Request(self.config.endpoint(), data=body, headers=headers, method="POST")
        payload = self._request(request)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ModelError("LLM 响应缺少 choices[0].message.content") from error
        if not isinstance(content, str) or not content.strip():
            raise ModelError("LLM 返回了空文本或不支持的内容格式")
        return content

    @staticmethod
    def _request(request: Request) -> Dict[str, object]:
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ModelError("LLM 响应超过 1 MB 限制")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ModelError("LLM 响应必须是 JSON 对象")
            return payload
        except HTTPError as error:
            messages = {401: "API Key 无效", 403: "没有模型访问权限", 404: "接口地址或模型不存在",
                        429: "调用限流或余额不足"}
            raise ModelError("LLM 请求失败（HTTP " + str(error.code) + "）：" +
                             messages.get(error.code, "请检查模型服务状态"), 502) from error
        except (URLError, socket.timeout, TimeoutError) as error:
            raise ModelError("无法连接 LLM 或请求超时，请检查地址和网络", 502) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ModelError("LLM 服务未返回有效 JSON", 502) from error
