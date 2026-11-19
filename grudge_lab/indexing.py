import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from .analysis import ANALYSIS_PROMPT, REVIEW_PROMPT, analyze, usable
from .errors import AppError
from .index_lock import index_lock
from .llm import LLMClient, LLMConfig
from .models import Candidate, Evidence, Message, MessageCursor, SourceConfig
from .source import SourceReader
from .store import MemoryStore


LOGGER = logging.getLogger(__name__)
PAGE_SIZE = 30


def encode_cursor(cursor: Optional[MessageCursor]) -> str:
    return json.dumps(asdict(cursor), ensure_ascii=False) if cursor else ""


def decode_cursor(value: str) -> Optional[MessageCursor]:
    if not value:
        return None
    try:
        data = json.loads(value)
    except json.JSONDecodeError as error:
        raise AppError("索引游标损坏，无法续跑", 500) from error
    if not isinstance(data, dict) or set(data) != {"timestamp", "message_id"}:
        raise AppError("索引游标格式无效，无法续跑", 500)
    timestamp, message_id = data["timestamp"], data["message_id"]
    if timestamp is not None and not isinstance(timestamp, (str, int, float)):
        raise AppError("索引游标时间类型无效", 500)
    if not isinstance(message_id, (str, int, float)):
        raise AppError("索引游标消息 ID 类型无效", 500)
    return MessageCursor(timestamp, message_id)


def roll_digest(previous: str, messages: List[Message]) -> str:
    for message in messages:
        digest = hashlib.sha256(previous.encode("ascii"))
        digest.update(json.dumps(asdict(message), ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")
        previous = digest.hexdigest()
    return previous


def batch_digest(messages: List[Message]) -> str:
    return roll_digest("", messages)


def candidate_cache_key(config: Optional[LLMConfig]) -> str:
    payload = {"mode": "demo", "model": ""} if config is None else {
        "mode": "llm", "base_url": config.base_url, "model": config.model,
    }
    payload["pipeline"] = ANALYSIS_PROMPT + REVIEW_PROMPT
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def candidates_from_cache(value: str) -> List[Candidate]:
    try:
        payload = json.loads(value)
        if not isinstance(payload, list):
            raise ValueError("缓存不是列表")
        result = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("evidence"), list):
                raise ValueError("候选卡格式无效")
            evidence = [Evidence(**entry) for entry in item["evidence"] if isinstance(entry, dict)]
            if len(evidence) != len(item["evidence"]):
                raise ValueError("候选证据格式无效")
            result.append(Candidate(**{**item, "evidence": evidence}))
        return result
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as error:
        raise AppError("索引分析缓存损坏：" + str(error), 500) from error


class IndexManager:
    def __init__(self, source: SourceConfig, store: MemoryStore,
                 llm: Optional[LLMConfig] = None, page_size: int = PAGE_SIZE) -> None:
        if not 1 <= page_size <= 500:
            raise AppError("索引分页条数必须在 1–500 之间")
        self.source = source
        self.reader = SourceReader(source)
        self.store = store
        self.llm = llm
        self.client = LLMClient(llm, local_only=True) if llm else None
        self.page_size = page_size
        self.model_key = candidate_cache_key(llm)
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.RLock()

    def start(self, user_id: str = "") -> Dict[str, object]:
        users = [user_id] if user_id else self.store.consented_users(self.source.key)
        if user_id and not self.store.consent(self.source.key, user_id):
            raise AppError("该人物尚未授权索引")
        if not users:
            raise AppError("没有已授权的人物可建立索引")
        if self.llm:
            self.llm.endpoint(local_only=True)
        self.reader.validate()
        token = self.reader.change_token()
        job_id = self.store.create_index_job(self.source.key, users, token, self.model_key, self.page_size)
        self._launch(job_id)
        return self.store.index_status(self.source.key, job_id)

    def resume_pending(self) -> None:
        for job_id in self.store.resumable_jobs(self.source.key):
            self._launch(job_id)

    def status(self, job_id: str = "") -> Dict[str, object]:
        return self.store.index_status(self.source.key, job_id)

    def _launch(self, job_id: str) -> None:
        with self._lock:
            current = self._threads.get(job_id)
            if current and current.is_alive():
                return
            thread = threading.Thread(target=self._run, args=(job_id,), daemon=True,
                                      name="grudge-index-" + job_id[:8])
            self._threads[job_id] = thread
            thread.start()

    def _run(self, job_id: str) -> None:
        lock_path = self.store.path.with_name(self.store.path.name + "." + self.source.key + ".index.lock")
        try:
            while self.store.job_options(job_id)["status"] in {"queued", "running"}:
                with index_lock(lock_path) as acquired:
                    if acquired:
                        self._run_locked(job_id)
                        return
                time.sleep(0.1)
        except AppError as error:
            LOGGER.error("索引锁失败：%s", error)
            self.store.fail_job(job_id, str(error))

    def _run_locked(self, job_id: str) -> None:
        try:
            options = self.store.job_options(job_id)
            if options["status"] not in {"queued", "running"}:
                return
            if options["model_key"] and options["model_key"] != self.model_key:
                raise AppError("分析引擎配置已变化，旧任务已停止；请重新建立索引")
            if self.llm:
                self.llm.endpoint(local_only=True)
            page_size = int(options["page_size"])
            self.reader.validate()
            while True:
                pending = self.store.job_users(job_id)
                if not pending:
                    self.store.finish_job_user(job_id, "", "complete")
                    return
                item = pending[0]
                user_id = str(item["user_id"])
                try:
                    self._process_user(job_id, item, page_size)
                except AppError as error:
                    if not self.store.consent(self.source.key, user_id):
                        latest = self.store.job_user(job_id, user_id) or item
                        self.store.discard_generation(self.source.key, user_id, str(latest["generation_id"]))
                        self.store.finish_job_user(job_id, user_id, "revoked", "用户撤回授权")
                        continue
                    self._fail_user(job_id, user_id, item, str(error))
                except Exception as error:
                    LOGGER.exception("索引用户失败：%s", user_id)
                    self._fail_user(job_id, user_id, item, "索引失败：" + str(error))
        except Exception as error:
            LOGGER.exception("索引任务失败：%s", job_id)
            self.store.fail_job(job_id, str(error))

    def _process_user(self, job_id: str, item: Dict[str, object], page_size: int) -> None:
        user_id = str(item["user_id"])
        if not self.store.consent(self.source.key, user_id):
            self.store.finish_job_user(job_id, user_id, "revoked", "用户撤回授权")
            return
        job_token = self.store.job_source_token(job_id)
        if self.reader.change_token() != job_token:
            raise AppError("索引期间原始聊天库已变化，请重新建立索引")
        state = self.store.corpus_state(self.source.key, user_id)
        if item["status"] == "pending" and state and state["status"] == "complete" and \
                state["source_token"] == job_token and state["model_key"] == self.model_key:
            self.store.finish_job_user(job_id, user_id, "unchanged")
            return
        self.store.mark_corpus_indexing(self.source.key, user_id)
        phase = str(item["phase"])
        cursor_text = str(item["cursor"])
        rolling = str(item["rolling_digest"])
        count = int(item["message_count"])
        expected = str(item["expected_digest"])
        generation = str(item["generation_id"])
        if phase == "hashing":
            self.reader.validate_user_ids(user_id)
            phase, cursor_text, rolling, expected, count, generation = self._hash_user(
                job_id, user_id, cursor_text, rolling, expected, count, generation,
            )
        if phase == "analyzing":
            phase, cursor_text = self._analyze_user(
                job_id, user_id, cursor_text, expected, count, generation, page_size,
            )
        if phase == "verifying":
            self._verify_user(job_id, user_id, cursor_text, rolling, expected, count, generation, job_token)

    def _hash_user(self, job_id: str, user_id: str, cursor_text: str, rolling: str,
                   expected: str, count: int, generation: str) -> Tuple[str, str, str, str, int, str]:
        cursor = decode_cursor(cursor_text)
        for messages, next_cursor in self.reader.iter_pages(user_id, cursor, 500):
            if not self.store.consent(self.source.key, user_id):
                raise AppError("用户已撤回授权")
            rolling = roll_digest(rolling, messages)
            sequence_start = count + 1
            count += len(messages)
            cursor = next_cursor
            self.store.checkpoint(job_id, user_id, "hashing", encode_cursor(cursor), rolling,
                                  expected, count, generation,
                                  messages=[message for message in messages if usable(message)],
                                  sequence_start=sequence_start)
        if self.reader.change_token() != self.store.job_source_token(job_id):
            raise AppError("索引期间原始聊天发生变化，请重新运行")
        expected = rolling
        state = self.store.corpus_state(self.source.key, user_id)
        if state and state["source_digest"] == expected and state["active_generation"] and \
                state["model_key"] == self.model_key:
            self.store.keep_corpus_generation(self.source.key, user_id, self.reader.change_token())
            self.store.finish_job_user(job_id, user_id, "unchanged")
            return "done", "", rolling, expected, count, generation
        generation = generation or uuid4().hex
        self.store.checkpoint(job_id, user_id, "analyzing", "", "", expected, count, generation)
        return "analyzing", "", "", expected, count, generation

    def _analyze_user(self, job_id: str, user_id: str, cursor_text: str, expected: str,
                      count: int, generation: str, page_size: int) -> Tuple[str, str]:
        sequence = int(cursor_text) if cursor_text else 0
        while True:
            if not self.store.consent(self.source.key, user_id):
                raise AppError("用户已撤回授权")
            messages, next_sequence = self.store.staged_message_page(job_id, user_id, sequence, page_size)
            if not messages:
                break
            digest = batch_digest(messages)
            cached = self.store.cached_batch(self.source.key, user_id, digest, self.model_key)
            candidates = candidates_from_cache(cached) if cached is not None else self._analyze_batch(messages)
            if cached is None:
                self.store.cache_batch(self.source.key, user_id, digest, self.model_key, candidates)
            self.store.save_indexed_batch(self.source.key, user_id, generation, candidates)
            sequence = next_sequence
            self.store.checkpoint(job_id, user_id, "analyzing", str(sequence), "",
                                  expected, count, generation)
        self.store.checkpoint(job_id, user_id, "verifying", "", "", expected, count, generation)
        return "verifying", ""

    def _analyze_batch(self, messages: List[Message]) -> List[Candidate]:
        return analyze(messages, self.client).candidates

    def _verify_user(self, job_id: str, user_id: str, cursor_text: str, rolling: str,
                     expected: str, count: int, generation: str, source_token: str) -> None:
        cursor = decode_cursor(cursor_text)
        for messages, next_cursor in self.reader.iter_pages(user_id, cursor, 500):
            if not self.store.consent(self.source.key, user_id):
                raise AppError("用户已撤回授权")
            rolling = roll_digest(rolling, messages)
            cursor = next_cursor
            self.store.checkpoint(job_id, user_id, "verifying", encode_cursor(cursor), rolling,
                                  expected, count, generation)
        if rolling != expected or self.reader.change_token() != source_token:
            raise AppError("索引期间原始聊天发生变化；本次索引未启用，请重新运行")
        self.store.activate_generation(self.source.key, user_id, generation, expected, count,
                                       source_token, self.model_key)
        self.store.finish_job_user(job_id, user_id, "complete")

    def _fail_user(self, job_id: str, user_id: str, item: Dict[str, object], error: str) -> None:
        latest = self.store.job_user(job_id, user_id) or item
        generation = str(latest["generation_id"])
        self.store.discard_generation(self.source.key, user_id, generation)
        self.store.fail_corpus_index(self.source.key, user_id)
        self.store.finish_job_user(job_id, user_id, "failed", error)
