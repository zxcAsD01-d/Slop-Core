import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from grudge_lab.analysis import validate_candidates
from grudge_lab.errors import AppError
from grudge_lab.indexing import IndexManager, roll_digest
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
        self.assertEqual(self.plugin.search_person_materials("zhou", '" OR x')["materials"], [])
        self.assertEqual(self.plugin.search_person_materials("zhou", topic="不存在")["materials"], [])
        with self.assertRaises(AppError):
            self.plugin.search_person_materials("lin")

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
        with self.assertRaises(AppError):
            build_prompt("zhou", "周野", [], intensity=3)

    def test_missing_fts5_is_explicit_error(self):
        from grudge_lab.store import MemoryStore
        with patch("grudge_lab.store.sqlite3.connect", side_effect=sqlite3.OperationalError("no such module: fts5")):
            with self.assertRaises((AppError, sqlite3.OperationalError)):
                MemoryStore(self.root / "no-fts.db")


if __name__ == "__main__":
    unittest.main()
