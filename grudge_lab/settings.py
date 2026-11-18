import json
import os
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional

from .errors import AppError
from .llm import LLMConfig
from .models import SourceConfig


SOURCE_FIELDS = set(SourceConfig.__dataclass_fields__)


class Settings:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / ".state" / "settings.json"
        self.lock = threading.RLock()
        self.api_key = os.environ.get("GRUDGE_LLM_API_KEY", "")
        self.data = self._load()

    def _load(self) -> Dict[str, object]:
        defaults = {"source": asdict(SourceConfig(str(self.root / "data" / "demo.db"))),
                    "llm": {"base_url": "", "model": "", "mode": "demo"}, "limit": 120}
        if not self.path.exists():
            return defaults
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("设置必须是 JSON 对象")
            self._validate(data)
            return data
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise AppError("配置文件无效：" + str(error), 500) from error

    def public(self) -> Dict[str, object]:
        with self.lock:
            return {**self.data, "key_configured": bool(self.api_key)}

    def source(self) -> SourceConfig:
        with self.lock:
            return SourceConfig(**self.data["source"])

    def llm(self) -> Optional[LLMConfig]:
        with self.lock:
            config = self.data["llm"]
            if config["mode"] == "demo":
                return None
            result = LLMConfig(config["base_url"], config["model"], self.api_key)
            result.endpoint()
            return result

    def update(self, data: Dict[str, object]) -> None:
        self._validate(data)
        key = data.get("api_key")
        if key is not None and not isinstance(key, str):
            raise AppError("API Key 必须是字符串")
        clean = {"source": data["source"], "llm": data["llm"], "limit": data["limit"]}
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            try:
                temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.chmod(0o600)
                temporary.replace(self.path)
            except OSError as error:
                raise AppError("保存设置失败：" + str(error), 500) from error
            self.data = clean
            if key is not None:
                self.api_key = key

    @staticmethod
    def _validate(data: Dict[str, object]) -> None:
        source, llm = data.get("source"), data.get("llm")
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
            raise AppError("字段映射配置不完整")
        for key, value in source.items():
            if not isinstance(value, str) or len(value) > 4096:
                raise AppError("字段配置无效：" + key)
            if key not in {"name_column", "role_column", "conversation_column"} and not value.strip():
                raise AppError("字段不可为空：" + key)
        if not isinstance(llm, dict) or set(llm) != {"base_url", "model", "mode"}:
            raise AppError("LLM 配置不完整")
        if not all(isinstance(value, str) for value in llm.values()) or llm["mode"] not in {"demo", "llm"}:
            raise AppError("LLM 配置无效")
        limit = data.get("limit")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AppError("读取条数必须在 1–1000 之间")
        if llm["mode"] == "llm":
            LLMConfig(llm["base_url"], llm["model"], "").endpoint()
