import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from grudge_lab.analysis import validate_candidates
from grudge_lab.errors import AppError
from grudge_lab.index_lock import index_lock
from grudge_lab.indexing import IndexManager, roll_digest
from grudge_lab.llm import LLMConfig
from grudge_lab.models import Candidate, Evidence, SourceConfig
from grudge_lab.plugin import PromptPlugin
from grudge_lab.prompts import build_prompt
from grudge_lab.sample import create_sample


class IndexingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = create_sample(self.root / "data" / "demo.db")
        self.config = SourceConfig(str(self.source))
        self.plugin = PromptPlugin(self.config, self.root / "memory.db")
        self.manager = IndexManager(self.config, self.plugin.store)

    def tearDown(self):
        for thread in self.manager._threads.values():
            thread.join(timeout=5)
        self.temp.cleanup()

    def index(self, user_id="zhou"):
        self.plugin.set_consent(user_id, True)
        job = self.manager.start(user_id)
        return self.wait_job(str(job["id"]))

    def wait_job(self, job_id):
        deadline = time.monotonic() + 10
        result = self.manager.status(job_id)
        while result["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.01)
            result = self.manager.status(job_id)
        self.assertEqual(result["status"], "completed", result)
        return result

    def test_full_index_search_chinese_and_read_only(self):
        original = self.source.read_bytes()
        result = self.index()
        self.assertEqual(result["messages_scanned"], 10)
        matches = self.plugin.search_person_materials("zhou", "四十页")
        self.assertGreater(len(matches["materials"]), 0)
        self.assertIn("四十页", matches["prompt_block"])
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue(self.plugin.search_person_materials("zhou", "省时")["materials"])
        for query in ["我喜欢短", "喜欢"]:
            self.assertTrue(self.plugin.search_person_materials("zhou", query)["materials"])
        self.assertEqual(self.plugin.search_person_materials("zhou", '" OR x')["materials"], [])
        self.assertEqual(self.plugin.search_person_materials("zhou", topic="不存在")["materials"], [])
        with self.assertRaises(AppError):
            self.plugin.search_person_materials("lin")

    def test_legacy_fts_migration_indexes_quotes_without_unsafe_rewrites(self):
        self.index()
        store = self.plugin.store
        with store.connect() as connection:
            for suffix in ("ai", "ad", "au"):
                connection.execute(f"DROP TRIGGER corpus_materials_{suffix}")
            connection.execute("DROP TABLE corpus_fts")
            connection.execute("CREATE VIRTUAL TABLE corpus_fts USING fts5(title, tokenize='trigram')")
            connection.execute("UPDATE corpus_materials SET risk_level = 4, safe_rewrite = 'UNSAFE_ATTACK_MARKER'")
        upgraded = PromptPlugin(self.config, store.path)
        result = upgraded.search_person_materials("zhou", "我喜欢短")
        self.assertTrue(result["materials"])
        self.assertTrue(all(not item["safe_rewrite"] for item in result["materials"]))
        self.assertEqual(upgraded.search_person_materials("zhou", "UNSAFE_ATTACK_MARKER")["materials"], [])
        upgraded.set_consent("zhou", False)
        with store.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM corpus_fts WHERE corpus_fts MATCH ?",
                                       ('"我喜欢短"',)).fetchone()[0]
        self.assertEqual(total, 0)

    def test_active_job_reuse_requires_same_people_and_settings(self):
        create = self.plugin.store.create_index_job
        key, token = self.config.key, self.plugin.reader.change_token()
        job = create(key, ["zhou", "lin"], token, "model-a", 30)
        self.assertEqual(job, create(key, ["lin", "zhou"], token, "model-a", 30))
        for users, source_token, model, page_size in (
            (["lin"], token, "model-a", 30), (["zhou", "lin"], "changed", "model-a", 30),
            (["zhou", "lin"], token, "model-b", 30), (["zhou", "lin"], token, "model-a", 31),
        ):
            with self.subTest(users=users, model=model, page_size=page_size), self.assertRaises(AppError) as error:
                create(key, users, source_token, model, page_size)
            self.assertEqual(error.exception.status, 409)

    def test_waiting_worker_takes_over_after_index_lock_release(self):
        self.plugin.set_consent("zhou", True)
        store = self.plugin.store
        lock_path = store.path.with_name(f"{store.path.name}.{self.config.key}.index.lock")
        with index_lock(lock_path) as acquired:
            self.assertTrue(acquired)
            job = self.manager.start("zhou")
            worker = self.manager._threads[str(job["id"])]
            worker.join(timeout=0.2)
            self.assertTrue(worker.is_alive())
        self.wait_job(str(job["id"]))

    def test_full_index_refuses_remote_llm_before_creating_job(self):
        self.plugin.set_consent("zhou", True)
        manager = IndexManager(self.config, self.plugin.store, LLMConfig("https://model.invalid/v1", "test", ""))
        with self.assertRaisesRegex(AppError, "只允许使用本机"):
            manager.start("zhou")
        self.assertFalse(self.plugin.store.resumable_jobs(self.config.key))

    def test_preview_idempotency_conflict_and_recent_sorting(self):
        self.index()
        first = self.plugin.search_person_materials("zhou")
        material = first["materials"][0]
        self.assertEqual(material["use_count"], 0)
        again = self.plugin.search_person_materials("zhou")
        self.assertEqual(again["materials"][0]["use_count"], 0)
        self.assertTrue(again["pending_confirmations"])
        receipt = material["use_receipt"]
        result = self.plugin.confirm_material_use("zhou", material["id"], receipt)
        self.assertTrue(result["counted"])
        repeated = self.plugin.confirm_material_use("zhou", material["id"], receipt)
        self.assertFalse(repeated["counted"])
        self.assertEqual(repeated["use_count"], 1)
        sorted_result = self.plugin.search_person_materials("zhou")["materials"]
        self.assertEqual(sorted_result[-1]["id"], material["id"])
        with self.assertRaises(AppError) as context:
            self.plugin.confirm_material_use("zhou", sorted_result[0]["id"], receipt)
        self.assertEqual(context.exception.status, 409)

    def test_usage_sort_count_and_oldest_then_recent(self):
        self.index()
        materials = self.plugin.search_person_materials("zhou")["materials"]
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(timespec="seconds")
        older = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="seconds")
        with self.plugin.store.connect() as connection:
            connection.execute("UPDATE corpus_materials SET use_count = 8, last_used_at = ?", (old,))
            connection.execute("UPDATE corpus_materials SET use_count = 1, last_used_at = ? WHERE id = ?",
                               (older, materials[0]["id"]))
            connection.execute("UPDATE corpus_materials SET use_count = 1, last_used_at = ? WHERE id = ?",
                               (old, materials[1]["id"]))
        ordered = self.plugin.search_person_materials("zhou")["materials"]
        self.assertEqual([item["id"] for item in ordered[:2]],
                         [materials[0]["id"], materials[1]["id"]])

    def test_preview_batch_reuses_receipts_without_writing_and_rechecks_consent(self):
        self.index()
        store = self.plugin.store
        key = self.config.key
        ids = [int(item["id"]) for item in store.search_materials(key, "zhou") if not item["risk_only"]]
        self.assertGreaterEqual(len(ids), 2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(store.preview_receipts, key, "zhou", ids) for _ in range(2)]
            first, second = (future.result(timeout=5) for future in futures)
        self.assertEqual(first, second)

        statements = []
        connect = store.connect

        @contextmanager
        def traced_connection():
            with connect() as connection:
                connection.set_trace_callback(statements.append)
                yield connection

        with patch.object(store, "connect", traced_connection):
            self.assertEqual(store.preview_receipts(key, "zhou", ids), first)
        self.assertEqual(len(statements), 1)
        self.assertTrue(statements[0].startswith("SELECT"))

        self.plugin.set_consent("zhou", False)
        with self.assertRaises(AppError) as context:
            store.preview_receipts(key, "zhou", ids)
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(store.pending_previews(key, "zhou"), [])

    def test_unchanged_skips_model_and_incremental_preserves_usage(self):
        self.index()
        material = self.plugin.search_person_materials("zhou", "四十页")["materials"][0]
        self.plugin.confirm_material_use("zhou", material["id"], material["use_receipt"])
        with patch.object(self.manager, "_analyze_batch", side_effect=AssertionError("不得重新调用模型")):
            self.wait_job(str(self.manager.start("zhou")["id"]))
        with sqlite3.connect(self.source) as connection:
            connection.execute("INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                               ("zhou-new", "r", "zhou", "周野", "user",
                                "我喜欢明确的测试结果。", "2026-12-01T00:00:00Z"))
        with self.assertRaises(AppError) as context:
            self.plugin.search_person_materials("zhou")
        self.assertEqual(context.exception.status, 409)
        self.wait_job(str(self.manager.start("zhou")["id"]))
        restored = self.plugin.material_detail("zhou", material["id"])
        self.assertEqual(restored["use_count"], 1)

    def test_revoke_clears_all_derived_data_and_fts(self):
        self.index()
        material = self.plugin.search_person_materials("zhou")["materials"][0]
        self.plugin.confirm_material_use("zhou", material["id"], material["use_receipt"])
        self.plugin.set_consent("zhou", False)
        with self.plugin.store.connect() as connection:
            for table in ["corpus_users", "corpus_materials", "material_uses", "material_previews", "analysis_batches"]:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM " + table + " WHERE source_key = ? AND user_id = ?",
                    (self.config.key, "zhou"),
                ).fetchone()[0], 0, table)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM corpus_fts WHERE corpus_fts MATCH ?", ('"四十页"',),
            ).fetchone()[0], 0)
        with self.assertRaises(AppError):
            self.plugin.material_detail("zhou", material["id"])

    def test_changed_source_during_analysis_never_activates(self):
        self.plugin.set_consent("zhou", True)
        original = self.manager._analyze_batch

        def changing(messages):
            with sqlite3.connect(self.source) as connection:
                connection.execute("UPDATE chat_messages SET body = body || '更新' WHERE message_id = 'zhou-1'")
            return original(messages)

        with patch.object(self.manager, "_analyze_batch", side_effect=changing):
            job = self.manager.start("zhou")
            thread = self.manager._threads[str(job["id"])]
            thread.join(timeout=5)
        status = self.manager.status(str(job["id"]))
        self.assertEqual(status["status"], "completed_with_errors")
        with self.assertRaises(AppError):
            self.plugin.search_person_materials("zhou")

    def test_resume_from_hash_checkpoint(self):
        self.plugin.set_consent("zhou", True)
        token = self.plugin.reader.change_token()
        job_id = self.plugin.store.create_index_job(self.config.key, ["zhou"], token)
        messages, cursor = self.plugin.reader.message_page("zhou", limit=3)
        self.plugin.store.checkpoint(job_id, "zhou", "hashing", json.dumps(asdict(cursor)),
                                     roll_digest("", messages), "", len(messages), "")
        self.manager.page_size = 3
        self.manager.resume_pending()
        status = self.wait_job(job_id)
        self.assertEqual(status["messages_scanned"], 10)

    def test_resume_analysis_reuses_committed_cache(self):
        self.plugin.set_consent("zhou", True)
        messages = self.plugin.reader.history("zhou")
        digest = roll_digest("", messages)
        job_id = self.plugin.store.create_index_job(self.config.key, ["zhou"], self.plugin.reader.change_token())
        self.plugin.store.mark_corpus_indexing(self.config.key, "zhou")
        self.plugin.store.checkpoint(job_id, "zhou", "analyzing", "", "", digest,
                                     len(messages), "resume-generation", messages=messages, sequence_start=1)
        candidates = self.manager._analyze_batch(messages)
        self.plugin.store.cache_batch(self.config.key, "zhou", digest, self.manager.model_key, candidates)
        with patch.object(self.manager, "_analyze_batch", side_effect=AssertionError("缓存应命中")):
            self.manager.resume_pending()
            self.wait_job(job_id)
        self.assertTrue(self.plugin.search_person_materials("zhou")["materials"])

    def test_duplicate_ids_across_pages_are_rejected(self):
        with sqlite3.connect(self.source) as connection:
            connection.execute("CREATE TABLE repeated AS SELECT * FROM chat_messages")
            connection.execute("INSERT INTO repeated SELECT * FROM chat_messages WHERE message_id = 'zhou-1'")
        self.config = replace(self.config, table="repeated")
        self.plugin = PromptPlugin(self.config, self.root / "memory.db")
        self.manager = IndexManager(self.config, self.plugin.store, page_size=3)
        self.plugin.set_consent("zhou", True)
        job = self.manager.start("zhou")
        self.manager._threads[str(job["id"])].join(timeout=5)
        result = self.manager.status(str(job["id"]))
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertIn("重复", result["errors"][0]["error"])

    def test_risk_fields_and_prompts_never_include_unsafe_angle(self):
        messages = self.plugin.reader.history("zhou")
        raw = {"kind": "habit", "title": "措辞风险", "observation": "存在升级冲突的风险。",
               "angle": "UNSAFE_ATTACK_MARKER", "topic": "会议", "severity": 3, "risk_level": 4,
               "risk_reason": "由论点转向贬低个人会升级冲突。", "safe_rewrite": "请说明这次会议的时间安排。",
               "evidence": [{"message_id": messages[0].id, "quote": messages[0].content}]}
        candidates, rejected = validate_candidates([raw], messages)
        self.assertEqual(rejected, 0)
        self.assertEqual(candidates[0].angle, "")
        result = build_prompt("zhou", "周野", [], risk_records=[asdict(candidates[0])])
        self.assertNotIn("UNSAFE_ATTACK_MARKER", result["text"])
        self.assertIn("请说明这次会议的时间安排", result["text"])
        for rewrite in ("", "  ", raw["angle"]):
            with self.subTest(rewrite=rewrite):
                self.assertEqual(validate_candidates([{**raw, "safe_rewrite": rewrite}], messages), ([], 1))
        with self.assertRaises(AppError):
            build_prompt("zhou", "周野", [], intensity=3)

    def test_missing_fts5_is_explicit_error(self):
        from grudge_lab.store import MemoryStore
        with patch("grudge_lab.store.sqlite3.connect", side_effect=sqlite3.OperationalError("no such module: fts5")):
            with self.assertRaises((AppError, sqlite3.OperationalError)):
                MemoryStore(self.root / "no-fts.db")


if __name__ == "__main__":
    unittest.main()
