import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

from .analysis import analyze, validate_one
from .errors import AppError
from .llm import LLMClient, LLMConfig
from .models import Message, SourceConfig
from .prompts import build_prompt
from .source import SourceReader
from .store import MemoryStore


def history_digest(messages: List[Message]) -> str:
    raw = json.dumps([asdict(message) for message in messages], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


class PromptPlugin:
    def __init__(self, source: SourceConfig, memory_path: Path, limit: int = 120) -> None:
        self.source = source
        self.reader = SourceReader(source)
        self.store = MemoryStore(memory_path)
        self.limit = limit

    def set_consent(self, user_id: str, enabled: bool) -> None:
        self.store.set_consent(self.source.key, user_id, enabled)

    def profile(self, user_id: str) -> Dict[str, object]:
        messages = self.reader.history(user_id, self.limit)
        memories = self.store.list(self.source.key, user_id)
        digest = history_digest(messages)
        indexed = {message.id: message for message in messages}
        for memory in memories:
            memory["fresh"] = memory["source_digest"] == digest and validate_one(memory, indexed) is not None
            del memory["source_digest"]
        return {"user_id": user_id, "name": messages[-1].name if messages else user_id,
                "messages": [asdict(message) for message in messages], "memories": memories,
                "consent": self.store.consent(self.source.key, user_id)}

    def analyze(self, user_id: str, llm: Optional[LLMConfig] = None) -> Dict[str, object]:
        if not self.store.consent(self.source.key, user_id):
            raise AppError("请先确认用户已同意记录，并开启该用户的记忆")
        messages = self.reader.history(user_id, self.limit)
        if not messages:
            raise AppError("当前用户没有可分析的消息")
        result = analyze(messages, LLMClient(llm) if llm else None)
        digest = history_digest(messages)
        if digest != history_digest(self.reader.history(user_id, self.limit)):
            raise AppError("分析期间原始聊天发生变化，请重新分析；本次没有保存", 409)
        saved = self.store.save(self.source.key, user_id, result.candidates, digest)
        return {"saved": saved, "accepted": len(result.candidates), "excluded": result.excluded,
                "mode": result.mode, "message_count": len(messages)}

    def generate(self, user_id: str, intensity: int = 2, budget: int = 6000) -> Dict[str, object]:
        profile = self.profile(user_id)
        if not profile["consent"]:
            raise AppError("当前用户未开启记忆，无法生成个人提示词")
        memories = profile["memories"]
        fresh = [item for item in memories if item["fresh"]]
        safe = [item for item in fresh if item["severity"] <= 2 and item["risk_level"] <= 2]
        risks = [item for item in fresh if item["severity"] >= 3 or item["risk_level"] >= 3]
        result = build_prompt(user_id, str(profile["name"]), safe, intensity, budget, risks)
        result["stale"] = len(memories) - len(fresh)
        return result

    def _check_index(self, user_id: str) -> None:
        state = self.store.corpus_state(self.source.key, user_id)
        if not self.store.consent(self.source.key, user_id):
            raise AppError("该人物未授权索引查询")
        if state is None or state["status"] != "complete":
            raise AppError("该人物尚无完成的全文索引", 404)
        if state["source_token"] != self.reader.change_token():
            raise AppError("原始聊天库已变化，请增量更新索引后再查询", 409)

    def search_person_materials(self, user_id: str, query: str = "", topic: str = "",
                                limit: int = 10) -> Dict[str, object]:
        self._check_index(user_id)
        pending = self.store.pending_previews(self.source.key, user_id)
        materials = self.store.search_materials(self.source.key, user_id, query, topic, limit)
        safe = [item for item in materials if not item["risk_only"]]
        risks = [item for item in materials if item["risk_only"]]
        prompt = build_prompt(user_id, user_id, safe, 2, 12000, risks)
        for material in safe:
            material["use_receipt"] = self.store.preview_receipt(
                self.source.key, user_id, int(material["id"]),
            )
        return {"person_id": user_id, "materials": materials, "prompt_block": prompt["text"],
                "pending_confirmations": pending,
                "usage_note": "预览不计数。仅实际采用后用 use_receipt 作为 idempotency_key 确认；未采用可忽略提醒。",
                "ordering": "话题相关性、过去24小时使用后置、使用次数升序、最近使用时间升序"}

    def material_detail(self, user_id: str, material_id: int) -> Dict[str, object]:
        self._check_index(user_id)
        material = self.store.get_material(self.source.key, user_id, material_id)
        if not material["risk_only"]:
            material["use_receipt"] = self.store.preview_receipt(self.source.key, user_id, material_id)
        return material

    def confirm_material_use(self, user_id: str, material_id: int,
                            idempotency_key: str) -> Dict[str, object]:
        self._check_index(user_id)
        return self.store.confirm_material_use(self.source.key, user_id, material_id, idempotency_key)

    def delete_memory(self, user_id: str, memory_id: int) -> None:
        self.store.delete(self.source.key, user_id, memory_id)
