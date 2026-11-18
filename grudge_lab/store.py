import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from uuid import uuid4

from .errors import AppError
from .models import Candidate, Message


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = None
        try:
            connection = sqlite3.connect(str(self.path), timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        except sqlite3.Error as error:
            if "fts5" in str(error) or "trigram" in str(error):
                raise AppError("当前 SQLite 缺少 FTS5 / trigram 能力，请使用支持它们的 Python 3.11+ 构建", 500) from error
            raise AppError("记忆数据库操作失败：" + str(error), 500) from error
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS consent (
                    source_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_key, user_id)
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
                    title TEXT NOT NULL, observation TEXT NOT NULL,
                    angle TEXT NOT NULL, evidence TEXT NOT NULL, topic TEXT NOT NULL DEFAULT '',
                    severity INTEGER NOT NULL DEFAULT 4, risk_level INTEGER NOT NULL DEFAULT 4,
                    risk_reason TEXT NOT NULL DEFAULT '旧版记忆待风险复核', safe_rewrite TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, source_digest TEXT NOT NULL,
                    UNIQUE (source_key, user_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS corpus_users (
                    source_key TEXT NOT NULL, user_id TEXT NOT NULL, status TEXT NOT NULL,
                    source_digest TEXT NOT NULL DEFAULT '', message_count INTEGER NOT NULL DEFAULT 0,
                    indexed_at TEXT NOT NULL DEFAULT '', active_generation TEXT NOT NULL DEFAULT '',
                    source_token TEXT NOT NULL DEFAULT '', model_key TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (source_key, user_id)
                );
                CREATE TABLE IF NOT EXISTS corpus_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL,
                    title TEXT NOT NULL, observation TEXT NOT NULL, angle TEXT NOT NULL, evidence TEXT NOT NULL,
                    topic TEXT NOT NULL, severity INTEGER NOT NULL, risk_level INTEGER NOT NULL,
                    risk_reason TEXT NOT NULL, safe_rewrite TEXT NOT NULL, use_count INTEGER NOT NULL DEFAULT 0,
                    last_used_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    UNIQUE(source_key, user_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS corpus_materials_owner
                    ON corpus_materials(source_key, user_id, generation_id);
                CREATE TABLE IF NOT EXISTS corpus_generation_materials (
                    generation_id TEXT NOT NULL, material_id INTEGER NOT NULL,
                    PRIMARY KEY(generation_id, material_id),
                    FOREIGN KEY(material_id) REFERENCES corpus_materials(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS analysis_batches (
                    source_key TEXT NOT NULL, user_id TEXT NOT NULL, digest TEXT NOT NULL,
                    model_key TEXT NOT NULL, candidates TEXT NOT NULL,
                    PRIMARY KEY(source_key, user_id, digest, model_key)
                );
                CREATE TABLE IF NOT EXISTS index_jobs (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, status TEXT NOT NULL,
                    source_token TEXT NOT NULL DEFAULT '', model_key TEXT NOT NULL DEFAULT '',
                    page_size INTEGER NOT NULL DEFAULT 30,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_job_users (
                    job_id TEXT NOT NULL, source_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'hashing', cursor TEXT NOT NULL DEFAULT '',
                    rolling_digest TEXT NOT NULL DEFAULT '', expected_digest TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0, generation_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, user_id), FOREIGN KEY(job_id) REFERENCES index_jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS index_messages (
                    job_id TEXT NOT NULL, user_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(job_id, user_id, message_id),
                    FOREIGN KEY(job_id) REFERENCES index_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS index_messages_sequence ON index_messages(job_id, user_id, sequence);
                CREATE TABLE IF NOT EXISTS material_uses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    material_id INTEGER NOT NULL, idempotency_key TEXT NOT NULL, used_at TEXT NOT NULL,
                    UNIQUE(source_key, user_id, idempotency_key),
                    FOREIGN KEY(material_id) REFERENCES corpus_materials(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS material_previews (
                    receipt TEXT PRIMARY KEY, source_key TEXT NOT NULL, user_id TEXT NOT NULL,
                    material_id INTEGER NOT NULL, created_at TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(material_id) REFERENCES corpus_materials(id) ON DELETE CASCADE
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS corpus_fts USING fts5(
                    title, topic, observation, angle, risk_reason, safe_rewrite,
                    content='corpus_materials', content_rowid='id', tokenize='trigram'
                );
                CREATE TRIGGER IF NOT EXISTS corpus_materials_ai AFTER INSERT ON corpus_materials BEGIN
                    INSERT INTO corpus_fts(rowid, title, topic, observation, angle, risk_reason, safe_rewrite)
                    VALUES (new.id, new.title, new.topic, new.observation, new.angle, new.risk_reason, new.safe_rewrite);
                END;
                CREATE TRIGGER IF NOT EXISTS corpus_materials_ad AFTER DELETE ON corpus_materials BEGIN
                    INSERT INTO corpus_fts(corpus_fts, rowid, title, topic, observation, angle, risk_reason, safe_rewrite)
                    VALUES ('delete', old.id, old.title, old.topic, old.observation, old.angle, old.risk_reason, old.safe_rewrite);
                END;
            """)
            self._ensure_column(connection, "memories", "topic", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "memories", "severity", "INTEGER NOT NULL DEFAULT 4")
            self._ensure_column(connection, "memories", "risk_level", "INTEGER NOT NULL DEFAULT 4")
            self._ensure_column(connection, "memories", "risk_reason",
                                "TEXT NOT NULL DEFAULT '旧版记忆待风险复核'")
            self._ensure_column(connection, "memories", "safe_rewrite", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "corpus_users", "source_token", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "corpus_users", "model_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "index_jobs", "source_token", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "index_jobs", "model_key", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "index_jobs", "page_size", "INTEGER NOT NULL DEFAULT 30")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(" + table + ")")}
        if name not in columns:
            connection.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)

    def consent(self, source_key: str, user_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM consent WHERE source_key = ? AND user_id = ?",
                (source_key, user_id),
            ).fetchone()
        return row is not None and bool(row[0])

    def set_consent(self, source_key: str, user_id: str, enabled: bool) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO consent VALUES (?, ?, ?, ?) ON CONFLICT(source_key, user_id) "
                "DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at",
                (source_key, user_id, int(enabled), now()),
            )
            if not enabled:
                connection.execute(
                    "DELETE FROM index_messages WHERE user_id = ? AND job_id IN "
                    "(SELECT id FROM index_jobs WHERE source_key = ?)", (user_id, source_key),
                )
                connection.execute("DELETE FROM material_previews WHERE source_key = ? AND user_id = ?",
                                   (source_key, user_id))
                connection.execute(
                    "DELETE FROM corpus_generation_materials WHERE material_id IN "
                    "(SELECT id FROM corpus_materials WHERE source_key = ? AND user_id = ?)",
                    (source_key, user_id),
                )
                connection.execute("DELETE FROM memories WHERE source_key = ? AND user_id = ?",
                                   (source_key, user_id))
                connection.execute("DELETE FROM corpus_materials WHERE source_key = ? AND user_id = ?",
                                   (source_key, user_id))
                connection.execute("DELETE FROM corpus_users WHERE source_key = ? AND user_id = ?",
                                   (source_key, user_id))
                connection.execute("DELETE FROM material_uses WHERE source_key = ? AND user_id = ?",
                                   (source_key, user_id))
                connection.execute("DELETE FROM analysis_batches WHERE source_key = ? AND user_id = ?",
                                   (source_key, user_id))
                connection.execute("UPDATE index_job_users SET status = 'revoked', phase = 'done', cursor = '', "
                                   "rolling_digest = '', expected_digest = '', message_count = 0, generation_id = '', "
                                   "error = ?, updated_at = ? WHERE source_key = ? AND user_id = ?",
                                   ("用户撤回授权", now(), source_key, user_id))
                self._refresh_jobs(connection, source_key)

    def save(self, source_key: str, user_id: str, candidates: List[Candidate], digest: str) -> int:
        inserted = 0
        with self.connect() as connection:
            consent = connection.execute(
                "SELECT enabled FROM consent WHERE source_key = ? AND user_id = ?",
                (source_key, user_id),
            ).fetchone()
            if consent is None or not consent[0]:
                raise AppError("请先确认该用户已同意记录并开启记忆")
            connection.execute(
                "UPDATE memories SET source_digest = '' WHERE source_key = ? AND user_id = ?",
                (source_key, user_id),
            )
            for candidate in candidates:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO memories (source_key, user_id, fingerprint, kind, "
                    "title, observation, angle, evidence, topic, severity, risk_level, risk_reason, "
                    "safe_rewrite, created_at, source_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_key, user_id, candidate.fingerprint, candidate.kind, candidate.title,
                     candidate.observation, candidate.angle,
                     json.dumps([asdict(item) for item in candidate.evidence], ensure_ascii=False),
                     candidate.topic, candidate.severity, candidate.risk_level, candidate.risk_reason,
                     candidate.safe_rewrite, now(), digest),
                )
                inserted += cursor.rowcount
                connection.execute(
                    "UPDATE memories SET source_digest = ? WHERE source_key = ? AND user_id = ? "
                    "AND fingerprint = ?", (digest, source_key, user_id, candidate.fingerprint),
                )
        return inserted

    def list(self, source_key: str, user_id: str) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, kind, title, observation, angle, evidence, topic, severity, risk_level, "
                "risk_reason, safe_rewrite, created_at, source_digest FROM memories "
                "WHERE source_key = ? AND user_id = ? ORDER BY id DESC LIMIT 200",
                (source_key, user_id),
            ).fetchall()
        result = [dict(row) for row in rows]
        for item in result:
            item["evidence"] = json.loads(str(item["evidence"]))
        return result

    def delete(self, source_key: str, user_id: str, memory_id: int) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memories WHERE source_key = ? AND user_id = ? AND id = ?",
                (source_key, user_id, memory_id),
            )
            if not cursor.rowcount:
                raise AppError("记忆不存在或不属于当前用户", 404)

    def consented_users(self, source_key: str) -> List[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT user_id FROM consent WHERE source_key = ? AND enabled = 1 ORDER BY user_id",
                (source_key,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def create_index_job(self, source_key: str, user_ids: List[str], source_token: str,
                         model_key: str = "", page_size: int = 30) -> str:
        if not user_ids:
            raise AppError("没有已授权的人物可建立索引")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT id FROM index_jobs WHERE source_key = ? AND status IN ('queued','running') "
                "ORDER BY created_at DESC LIMIT 1", (source_key,),
            ).fetchone()
            if active:
                return str(active[0])
            job_id, timestamp = uuid4().hex, now()
            connection.execute(
                "INSERT INTO index_jobs (id, source_key, status, source_token, model_key, page_size, created_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)",
                (job_id, source_key, source_token, model_key, page_size, timestamp, timestamp),
            )
            connection.executemany(
                "INSERT INTO index_job_users (job_id, source_key, user_id, status, phase, updated_at) "
                "VALUES (?, ?, ?, 'pending', 'hashing', ?)",
                [(job_id, source_key, user_id, timestamp) for user_id in user_ids],
            )
        return job_id

    def resumable_jobs(self, source_key: str) -> List[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM index_jobs WHERE source_key = ? AND status IN ('queued','running') "
                "ORDER BY created_at", (source_key,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def job_users(self, job_id: str) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM index_job_users WHERE job_id = ? AND status IN ('pending','running') "
                "ORDER BY rowid", (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def job_user(self, job_id: str, user_id: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM index_job_users WHERE job_id = ? AND user_id = ?",
                (job_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def job_source_token(self, job_id: str) -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT source_token FROM index_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise AppError("索引任务不存在", 404)
        return str(row[0])

    def job_options(self, job_id: str) -> Dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT model_key, page_size FROM index_jobs WHERE id = ?", (job_id,),
            ).fetchone()
        if row is None:
            raise AppError("索引任务不存在", 404)
        return dict(row)

    def checkpoint(self, job_id: str, user_id: str, phase: str, cursor: str,
                   rolling_digest: str, expected_digest: str, message_count: int,
                   generation_id: str, status: str = "running", error: str = "",
                   messages: Optional[List[Message]] = None, sequence_start: int = 0) -> None:
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT u.status FROM index_job_users u JOIN consent c ON c.source_key = u.source_key "
                "AND c.user_id = u.user_id WHERE u.job_id = ? AND u.user_id = ? AND c.enabled = 1",
                (job_id, user_id),
            ).fetchone()
            if row is None or row[0] not in {"pending", "running"}:
                raise AppError("索引任务已停止或授权已撤回")
            if messages:
                connection.executemany(
                    "INSERT OR REPLACE INTO index_messages VALUES (?, ?, ?, ?, ?)",
                    [(job_id, user_id, message.id, sequence_start + index,
                      json.dumps(asdict(message), ensure_ascii=False))
                     for index, message in enumerate(messages)],
                )
            connection.execute(
                "UPDATE index_job_users SET status = ?, phase = ?, cursor = ?, rolling_digest = ?, "
                "expected_digest = ?, message_count = ?, generation_id = ?, error = ?, updated_at = ? "
                "WHERE job_id = ? AND user_id = ? AND status IN ('pending','running')",
                (status, phase, cursor, rolling_digest, expected_digest, message_count,
                 generation_id, error[:1000], timestamp, job_id, user_id),
            )
            connection.execute("UPDATE index_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                               (timestamp, job_id))

    def mark_corpus_indexing(self, source_key: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO corpus_users (source_key, user_id, status) "
                "SELECT ?, ?, 'indexing' WHERE EXISTS "
                "(SELECT 1 FROM consent WHERE source_key = ? AND user_id = ? AND enabled = 1) "
                "ON CONFLICT(source_key, user_id) DO UPDATE SET status = 'indexing'",
                (source_key, user_id, source_key, user_id),
            )

    def keep_corpus_generation(self, source_key: str, user_id: str, source_token: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE corpus_users SET status = 'complete', source_token = ?, indexed_at = ? "
                "WHERE source_key = ? AND user_id = ? AND active_generation != ''",
                (source_token, now(), source_key, user_id),
            )

    def fail_corpus_index(self, source_key: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE corpus_users SET status = 'failed' WHERE source_key = ? AND user_id = ?",
                (source_key, user_id),
            )

    def corpus_state(self, source_key: str, user_id: str) -> Optional[Dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM corpus_users WHERE source_key = ? AND user_id = ?",
                (source_key, user_id),
            ).fetchone()
        return dict(row) if row else None

    def save_indexed_batch(self, source_key: str, user_id: str, generation_id: str,
                           candidates: List[Candidate]) -> int:
        if not candidates:
            return 0
        inserted = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            consent = connection.execute(
                "SELECT enabled FROM consent WHERE source_key = ? AND user_id = ?",
                (source_key, user_id),
            ).fetchone()
            if consent is None or not consent[0]:
                raise AppError("用户已撤回授权，停止写入索引")
            for candidate in candidates:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO corpus_materials (source_key, user_id, generation_id, "
                    "fingerprint, kind, title, observation, angle, evidence, topic, severity, risk_level, "
                    "risk_reason, safe_rewrite, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (source_key, user_id, generation_id, candidate.fingerprint, candidate.kind,
                     candidate.title, candidate.observation, candidate.angle,
                     json.dumps([asdict(item) for item in candidate.evidence], ensure_ascii=False),
                     candidate.topic, candidate.severity, candidate.risk_level, candidate.risk_reason,
                     candidate.safe_rewrite, now()),
                )
                inserted += cursor.rowcount
                material = connection.execute(
                    "SELECT id FROM corpus_materials WHERE source_key = ? AND user_id = ? AND fingerprint = ?",
                    (source_key, user_id, candidate.fingerprint),
                ).fetchone()
                connection.execute(
                    "INSERT OR IGNORE INTO corpus_generation_materials VALUES (?, ?)",
                    (generation_id, material[0]),
                )
        return inserted

    def activate_generation(self, source_key: str, user_id: str, generation_id: str,
                            digest: str, message_count: int, source_token: str = "", model_key: str = "") -> None:
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            consent = connection.execute(
                "SELECT enabled FROM consent WHERE source_key = ? AND user_id = ?",
                (source_key, user_id),
            ).fetchone()
            if consent is None or not consent[0]:
                raise AppError("用户已撤回授权，索引未启用")
            connection.execute(
                "INSERT INTO corpus_users (source_key, user_id, status, source_digest, message_count, "
                "indexed_at, active_generation, source_token, model_key) VALUES (?, ?, 'complete', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source_key, user_id) DO UPDATE SET status = 'complete', "
                "source_digest = excluded.source_digest, message_count = excluded.message_count, "
                "indexed_at = excluded.indexed_at, active_generation = excluded.active_generation, "
                "source_token = excluded.source_token, model_key = excluded.model_key",
                (source_key, user_id, digest, message_count, timestamp, generation_id, source_token, model_key),
            )
            connection.execute(
                "DELETE FROM corpus_generation_materials WHERE material_id IN "
                "(SELECT id FROM corpus_materials WHERE source_key = ? AND user_id = ?) AND generation_id != ?",
                (source_key, user_id, generation_id),
            )
            connection.execute(
                "DELETE FROM corpus_materials WHERE source_key = ? AND user_id = ? AND id NOT IN "
                "(SELECT material_id FROM corpus_generation_materials)", (source_key, user_id),
            )

    def discard_generation(self, source_key: str, user_id: str, generation_id: str) -> None:
        if not generation_id:
            return
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM corpus_generation_materials WHERE generation_id = ?", (generation_id,),
            )
            connection.execute(
                "DELETE FROM corpus_materials WHERE source_key = ? AND user_id = ? AND id NOT IN "
                "(SELECT material_id FROM corpus_generation_materials)", (source_key, user_id),
            )

    def finish_job_user(self, job_id: str, user_id: str, status: str,
                        error: str = "") -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM index_messages WHERE job_id = ? AND user_id = ?", (job_id, user_id))
            connection.execute(
                "UPDATE index_job_users SET status = ?, error = ?, updated_at = ? "
                "WHERE job_id = ? AND user_id = ?",
                (status, error[:1000], now(), job_id, user_id),
            )
            job = connection.execute(
                "SELECT source_key FROM index_jobs WHERE id = ?", (job_id,),
            ).fetchone()
            if job:
                self._refresh_jobs(connection, str(job[0]))

    def fail_job(self, job_id: str, error: str) -> None:
        with self.connect() as connection:
            unfinished = connection.execute(
                "SELECT source_key, user_id, generation_id FROM index_job_users "
                "WHERE job_id = ? AND status IN ('pending','running')", (job_id,),
            ).fetchall()
            for row in unfinished:
                connection.execute(
                    "DELETE FROM corpus_generation_materials WHERE generation_id = ?", (row["generation_id"],),
                )
                connection.execute(
                    "DELETE FROM corpus_materials WHERE source_key = ? AND user_id = ? AND id NOT IN "
                    "(SELECT material_id FROM corpus_generation_materials)",
                    (row["source_key"], row["user_id"]),
                )
                connection.execute(
                    "UPDATE corpus_users SET status = 'failed' WHERE source_key = ? AND user_id = ?",
                    (row["source_key"], row["user_id"]),
                )
            connection.execute("DELETE FROM index_messages WHERE job_id = ?", (job_id,))
            connection.execute(
                "UPDATE index_job_users SET status = 'failed', error = ?, updated_at = ? "
                "WHERE job_id = ? AND status IN ('pending','running')",
                (error[:1000], now(), job_id),
            )
            connection.execute(
                "UPDATE index_jobs SET status = 'completed_with_errors', updated_at = ? WHERE id = ?",
                (now(), job_id),
            )

    @staticmethod
    def _refresh_jobs(connection: sqlite3.Connection, source_key: str) -> None:
        rows = connection.execute(
            "SELECT j.id, COUNT(u.user_id) AS total, "
            "SUM(CASE WHEN u.status IN ('pending','running') THEN 1 ELSE 0 END) AS active, "
            "SUM(CASE WHEN u.status = 'failed' THEN 1 ELSE 0 END) AS failed "
            "FROM index_jobs j LEFT JOIN index_job_users u ON u.job_id = j.id "
            "WHERE j.source_key = ? AND j.status IN ('queued','running') GROUP BY j.id",
            (source_key,),
        ).fetchall()
        for row in rows:
            if row[1] == 0:
                status = "completed"
            elif row[2]:
                status = "running"
            else:
                status = "completed_with_errors" if row[3] else "completed"
            connection.execute("UPDATE index_jobs SET status = ?, updated_at = ? WHERE id = ?",
                               (status, now(), row[0]))

    def index_status(self, source_key: str, job_id: str = "") -> Dict[str, object]:
        with self.connect() as connection:
            if job_id:
                row = connection.execute(
                    "SELECT * FROM index_jobs WHERE source_key = ? AND id = ?",
                    (source_key, job_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM index_jobs WHERE source_key = ? ORDER BY created_at DESC LIMIT 1",
                    (source_key,),
                ).fetchone()
            if row is None:
                raise AppError("没有找到索引任务", 404)
            job = dict(row)
            counts = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status IN ('complete','unchanged','revoked','failed') THEN 1 ELSE 0 END) AS finished, "
                "SUM(message_count) AS messages, "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed "
                "FROM index_job_users WHERE job_id = ?", (job["id"],),
            ).fetchone()
            errors = connection.execute(
                "SELECT user_id, error FROM index_job_users WHERE job_id = ? AND status = 'failed'",
                (job["id"],),
            ).fetchall()
        return {**job, "total_users": counts["total"], "finished_users": counts["finished"] or 0,
                "messages_scanned": counts["messages"] or 0, "failed_users": counts["failed"] or 0,
                "errors": [dict(item) for item in errors]}

    def search_materials(self, source_key: str, user_id: str, query: str = "",
                         topic: str = "", limit: int = 10) -> List[Dict[str, object]]:
        if not 1 <= limit <= 50 or len(query) > 200 or len(topic) > 100:
            raise AppError("检索参数超出范围")
        if not self.consent(source_key, user_id):
            raise AppError("该人物未授权索引查询")
        state = self.corpus_state(source_key, user_id)
        if not state or state["status"] != "complete":
            raise AppError("该人物尚无完成的全文索引", 404)
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
        conditions = ["m.source_key = ?", "m.user_id = ?", "u.status = 'complete'"]
        parameters: List[object] = [source_key, user_id]
        join = ("JOIN corpus_users u ON u.source_key = m.source_key AND u.user_id = m.user_id "
                "JOIN corpus_generation_materials gm ON gm.material_id = m.id "
                "AND gm.generation_id = u.active_generation ")
        rank = ""
        if query:
            if len(query) >= 3:
                join += "JOIN corpus_fts ON corpus_fts.rowid = m.id "
                conditions.append("corpus_fts MATCH ?")
                parameters.append('"' + query.replace('"', '""') + '"')
                rank = "bm25(corpus_fts) ASC, "
            else:
                conditions.append("(instr(m.title, ?) > 0 OR instr(m.topic, ?) > 0 OR "
                                  "instr(m.observation, ?) > 0 OR instr(m.angle, ?) > 0 OR "
                                  "instr(m.risk_reason, ?) > 0 OR instr(m.safe_rewrite, ?) > 0)")
                parameters.extend([query] * 6)
        if topic:
            conditions.append("m.topic = ?")
            parameters.append(topic)
        sql = ("SELECT m.* FROM corpus_materials m " + join + " WHERE " + " AND ".join(conditions) +
               " ORDER BY " + rank + "CASE WHEN m.last_used_at >= ? THEN 1 ELSE 0 END ASC, "
               "m.use_count ASC, COALESCE(m.last_used_at, '') ASC, m.id DESC LIMIT ?")
        parameters.extend([cutoff, limit])
        with self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [self._material(row) for row in rows]

    def get_material(self, source_key: str, user_id: str, material_id: int) -> Dict[str, object]:
        if not self.consent(source_key, user_id):
            raise AppError("该人物未授权素材查询")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT m.* FROM corpus_materials m JOIN corpus_users u ON u.source_key = m.source_key "
                "AND u.user_id = m.user_id JOIN corpus_generation_materials gm ON gm.material_id = m.id "
                "AND gm.generation_id = u.active_generation "
                "WHERE m.source_key = ? AND m.user_id = ? AND m.id = ? AND u.status = 'complete'",
                (source_key, user_id, material_id),
            ).fetchone()
        if row is None:
            raise AppError("素材不存在或索引尚未完成", 404)
        return self._material(row)

    @staticmethod
    def _material(row: sqlite3.Row) -> Dict[str, object]:
        material = dict(row)
        material["evidence"] = json.loads(str(material["evidence"]))
        material["risk_only"] = material["severity"] >= 3 or material["risk_level"] >= 3
        material["scope_note"] = "局部批次观察；不证明全历史没有后续改口。使用前确认当前观点。"
        if material["risk_only"]:
            material["angle"] = ""
        material.pop("generation_id", None)
        material.pop("source_key", None)
        material.pop("fingerprint", None)
        return material

    def confirm_material_use(self, source_key: str, user_id: str, material_id: int,
                             idempotency_key: str) -> Dict[str, object]:
        if not 8 <= len(idempotency_key) <= 128:
            raise AppError("幂等凭证长度必须在 8–128 个字符之间")
        if not self.consent(source_key, user_id):
            raise AppError("该人物未授权使用记录")
        timestamp = now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            material = connection.execute(
                "SELECT m.use_count, m.last_used_at, m.severity, m.risk_level FROM corpus_materials m "
                "JOIN corpus_users u ON u.source_key = m.source_key AND u.user_id = m.user_id "
                "JOIN corpus_generation_materials gm ON gm.material_id = m.id "
                "AND gm.generation_id = u.active_generation WHERE m.source_key = ? AND m.user_id = ? "
                "AND m.id = ? AND u.status = 'complete'",
                (source_key, user_id, material_id),
            ).fetchone()
            if material is None:
                raise AppError("素材不存在或索引尚未完成", 404)
            if material["severity"] >= 3 or material["risk_level"] >= 3:
                raise AppError("风险说明不能作为回应素材确认使用")
            prior = connection.execute(
                "SELECT material_id, used_at FROM material_uses WHERE source_key = ? AND user_id = ? "
                "AND idempotency_key = ?", (source_key, user_id, idempotency_key),
            ).fetchone()
            if prior:
                if prior["material_id"] != material_id:
                    raise AppError("幂等凭证已用于另一条素材", 409)
                return {"counted": False, "use_count": material["use_count"],
                        "last_used_at": material["last_used_at"]}
            connection.execute(
                "INSERT INTO material_uses (source_key, user_id, material_id, idempotency_key, used_at) "
                "VALUES (?, ?, ?, ?, ?)", (source_key, user_id, material_id, idempotency_key, timestamp),
            )
            connection.execute(
                "UPDATE corpus_materials SET use_count = use_count + 1, last_used_at = ? "
                "WHERE source_key = ? AND user_id = ? AND id = ?",
                (timestamp, source_key, user_id, material_id),
            )
            connection.execute(
                "UPDATE material_previews SET confirmed = 1 WHERE source_key = ? AND user_id = ? "
                "AND material_id = ? AND receipt = ?", (source_key, user_id, material_id, idempotency_key),
            )
            count = int(material["use_count"]) + 1
        return {"counted": True, "use_count": count, "last_used_at": timestamp}

    def cached_batch(self, source_key: str, user_id: str, digest: str, model_key: str) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT candidates FROM analysis_batches WHERE source_key = ? AND user_id = ? "
                "AND digest = ? AND model_key = ?", (source_key, user_id, digest, model_key),
            ).fetchone()
        return str(row[0]) if row else None

    def cache_batch(self, source_key: str, user_id: str, digest: str, model_key: str,
                    candidates: List[Candidate]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO analysis_batches SELECT ?, ?, ?, ?, ? WHERE EXISTS "
                "(SELECT 1 FROM consent WHERE source_key = ? AND user_id = ? AND enabled = 1)",
                (source_key, user_id, digest, model_key,
                 json.dumps([asdict(candidate) for candidate in candidates], ensure_ascii=False),
                 source_key, user_id),
            )

    def preview_receipt(self, source_key: str, user_id: str, material_id: int) -> str:
        receipt = uuid4().hex
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT receipt FROM material_previews WHERE source_key = ? AND user_id = ? "
                "AND material_id = ? AND confirmed = 0 LIMIT 1", (source_key, user_id, material_id),
            ).fetchone()
            if prior:
                return str(prior[0])
            connection.execute(
                "INSERT INTO material_previews (receipt, source_key, user_id, material_id, created_at) "
                "SELECT ?, ?, ?, ?, ? WHERE EXISTS "
                "(SELECT 1 FROM consent WHERE source_key = ? AND user_id = ? AND enabled = 1)",
                (receipt, source_key, user_id, material_id, now(), source_key, user_id),
            )
        return receipt

    def pending_previews(self, source_key: str, user_id: str) -> List[Dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT receipt AS idempotency_key, material_id, created_at FROM material_previews "
                "WHERE source_key = ? AND user_id = ? AND confirmed = 0 ORDER BY created_at DESC LIMIT 20",
                (source_key, user_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def indexed_people(self, source_key: str, query: str = "", limit: int = 20) -> List[Dict[str, object]]:
        if not 1 <= limit <= 100 or len(query) > 100:
            raise AppError("人物查询参数超出范围")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.user_id AS person_id, COALESCE(u.status, 'not_indexed') AS status, "
                "COALESCE(u.message_count, 0) AS message_count, COALESCE(u.indexed_at, '') AS indexed_at "
                "FROM consent c LEFT JOIN corpus_users u ON u.source_key = c.source_key AND u.user_id = c.user_id "
                "WHERE c.source_key = ? AND c.enabled = 1 AND instr(c.user_id, ?) > 0 "
                "ORDER BY c.user_id LIMIT ?", (source_key, query, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def staged_message_page(self, job_id: str, user_id: str, after_sequence: int,
                            limit: int) -> Tuple[List[Message], int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload, sequence FROM index_messages WHERE job_id = ? AND user_id = ? AND sequence > ? "
                "ORDER BY sequence LIMIT ?", (job_id, user_id, after_sequence, limit),
            ).fetchall()
        return ([Message(**json.loads(row[0])) for row in rows],
                int(rows[-1]["sequence"]) if rows else after_sequence)
