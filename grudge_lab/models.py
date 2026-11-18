import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

from .errors import AppError


@dataclass(frozen=True)
class SourceConfig:
    path: str
    table: str = "chat_messages"
    id_column: str = "message_id"
    user_column: str = "author_id"
    content_column: str = "body"
    time_column: str = "sent_at"
    name_column: str = "author_name"
    role_column: str = "role"
    user_role: str = "user"
    conversation_column: str = "room_id"

    @property
    def key(self) -> str:
        payload = asdict(self)
        payload["path"] = str(Path(self.path).expanduser().resolve())
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class Message:
    id: str
    user_id: str
    name: str
    content: str
    timestamp: str
    conversation_id: str


@dataclass(frozen=True)
class MessageCursor:
    timestamp: Union[str, int, float, None]
    message_id: Union[str, int, float]


@dataclass(frozen=True)
class Evidence:
    message_id: str
    quote: str
    timestamp: str


@dataclass(frozen=True)
class Candidate:
    kind: str
    title: str
    observation: str
    angle: str
    evidence: List[Evidence]
    topic: str = ""
    severity: int = 1
    risk_level: int = 1
    risk_reason: str = ""
    safe_rewrite: str = ""

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class AnalysisResult:
    candidates: List[Candidate]
    excluded: int
    mode: str
    warning: Optional[str] = None


def required_text(data: Dict[str, object], key: str, maximum: int = 300) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AppError("缺少有效字段：" + key)
    if len(value.strip()) > maximum:
        raise AppError("字段过长：" + key)
    return value.strip()
