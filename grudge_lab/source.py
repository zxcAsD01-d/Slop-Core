import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .errors import SourceError
from .models import Message, MessageCursor, SourceConfig


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class SourceReader:
    def __init__(self, config: SourceConfig) -> None:
        self.config = config

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        path = Path(self.config.path).expanduser().resolve()
        if not path.is_file():
            raise SourceError("SQLite 数据库不存在：" + str(path))
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            yield connection
        except sqlite3.Error as error:
            raise SourceError("读取 SQLite 失败：" + str(error)) from error
        finally:
            if connection is not None:
                connection.close()

    def schema(self) -> List[Dict[str, object]]:
        with self.connect() as connection:
            names = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            return [{"name": row[0], "columns": [
                {"name": column[1], "type": column[2]}
                for column in connection.execute("PRAGMA table_info(" + identifier(row[0]) + ")")
            ]} for row in names]

    def validate(self) -> None:
        tables = {str(table["name"]): table["columns"] for table in self.schema()}
        if self.config.table not in tables:
            raise SourceError("找不到映射的数据表：" + self.config.table)
        columns = {column["name"] for column in tables[self.config.table]}
        fields = [self.config.id_column, self.config.user_column,
                  self.config.content_column, self.config.time_column]
        fields += [field for field in [self.config.name_column, self.config.role_column,
                                      self.config.conversation_column] if field]
        missing = [field for field in fields if field not in columns]
        if missing:
            raise SourceError("字段映射不存在：" + "、".join(missing))

    def users(self) -> List[Dict[str, object]]:
        self.validate()
        config = self.config
        user = "CAST(" + identifier(config.user_column) + " AS TEXT)"
        name = "MAX(" + identifier(config.name_column) + ")" if config.name_column else user
        where, parameters = self._role_filter()
        sql = ("SELECT " + user + " AS user_id, " + name + " AS name, COUNT(*) AS count "
               "FROM " + identifier(config.table) + where + " GROUP BY " + user +
               " ORDER BY count DESC, user_id LIMIT 500")
        with self.connect() as connection:
            return [{"id": str(row["user_id"]), "name": str(row["name"] or row["user_id"]),
                     "count": row["count"]} for row in connection.execute(sql, parameters)]

    def history(self, user_id: str, limit: int = 120) -> List[Message]:
        self.validate()
        if not 1 <= limit <= 1000:
            raise SourceError("读取条数必须在 1–1000 之间")
        config = self.config
        fields = [identifier(field) for field in [config.id_column, config.user_column,
                  config.content_column, config.time_column]]
        fields += [identifier(field) if field else "''" for field in
                   [config.name_column, config.conversation_column]]
        where, parameters = self._role_filter()
        where += (" AND " if where else " WHERE ") + "CAST(" + identifier(config.user_column) + " AS TEXT) = ?"
        sql = ("SELECT " + ", ".join(fields) + " FROM " + identifier(config.table) + where +
               " ORDER BY " + identifier(config.time_column) + " DESC, " +
               identifier(config.id_column) + " DESC LIMIT ?")
        with self.connect() as connection:
            rows = connection.execute(sql, parameters + [user_id, limit]).fetchall()
        return self._messages(rows, user_id)

    def message_page(self, user_id: str, cursor: Optional[MessageCursor] = None,
                     limit: int = 50) -> Tuple[List[Message], Optional[MessageCursor]]:
        self.validate()
        if not 1 <= limit <= 500:
            raise SourceError("分页条数必须在 1–500 之间")
        with self.connect() as connection:
            sql, parameters = self._page_query(user_id, cursor)
            rows = connection.execute(sql + " LIMIT ?", parameters + [limit]).fetchall()
        return self._page_result(rows, user_id)

    def iter_pages(self, user_id: str, cursor: Optional[MessageCursor] = None,
                   limit: int = 50) -> Iterator[Tuple[List[Message], MessageCursor]]:
        self.validate()
        if not 1 <= limit <= 500:
            raise SourceError("分页条数必须在 1–500 之间")
        with self.connect() as connection:
            sql, parameters = self._page_query(user_id, cursor)
            result = connection.execute(sql, parameters)
            while True:
                rows = result.fetchmany(limit)
                if not rows:
                    return
                messages, next_cursor = self._page_result(rows, user_id)
                if next_cursor is not None:
                    yield messages, next_cursor

    def _page_query(self, user_id: str, cursor: Optional[MessageCursor]):
        config = self.config
        id_column = identifier(config.id_column)
        user_column = identifier(config.user_column)
        time_column = identifier(config.time_column)
        fields = [id_column, user_column, identifier(config.content_column), time_column]
        fields += [identifier(field) if field else "''" for field in
                   [config.name_column, config.conversation_column]]
        role_where, parameters = self._role_filter()
        where = role_where + (" AND " if role_where else " WHERE ") + "CAST(" + user_column + " AS TEXT) = ?"
        parameters.append(user_id)
        if cursor is not None:
            time_order = "COALESCE(" + time_column + ", '')"
            id_order = "CAST(" + id_column + " AS TEXT) COLLATE BINARY"
            where += (" AND (" + time_order + " > ? OR (" + time_order +
                      " = ? AND " + id_order + " > ?))")
            cursor_time = cursor.timestamp if cursor.timestamp is not None else ""
            parameters.extend([cursor_time, cursor_time, str(cursor.message_id)])
        sql = ("SELECT " + ", ".join(fields) + " FROM " + identifier(config.table) + where +
               " ORDER BY COALESCE(" + time_column + ", '') ASC, CAST(" + id_column +
               " AS TEXT) COLLATE BINARY ASC")
        return sql, parameters

    def _page_result(self, rows: List[sqlite3.Row], user_id: str):
        if not rows:
            return [], None
        messages = self._messages(rows, user_id, reverse_rows=False)
        timestamp, message_id = rows[-1][3], rows[-1][0]
        if not isinstance(message_id, (str, int, float)):
            raise SourceError("分页游标中的消息 ID 必须是文本或数字")
        if timestamp is not None and not isinstance(timestamp, (str, int, float)):
            raise SourceError("分页游标中的时间必须是文本、数字或空值")
        return messages, MessageCursor(timestamp, message_id)

    def validate_user_ids(self, user_id: str) -> None:
        config = self.config
        column = "CAST(" + identifier(config.id_column) + " AS TEXT)"
        where, parameters = self._role_filter()
        where += (" AND " if where else " WHERE ") + "CAST(" + identifier(config.user_column) + " AS TEXT) = ?"
        sql = ("SELECT " + column + ", COUNT(*) FROM " + identifier(config.table) + where +
               " GROUP BY " + column + " HAVING COUNT(*) > 1 OR " + column +
               " IS NULL OR " + column + " = '' LIMIT 1")
        with self.connect() as connection:
            row = connection.execute(sql, parameters + [user_id]).fetchone()
        if row:
            raise SourceError("消息 ID 为空或重复，请映射对该用户唯一的字段")

    def change_token(self) -> str:
        path = Path(self.config.path).expanduser().resolve()
        parts = []
        for candidate in [path, Path(str(path) + "-wal")]:
            try:
                stat = candidate.stat()
                parts.append((stat.st_ino, stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                parts.append(None)
            except OSError as error:
                raise SourceError("无法检查源库状态：" + str(error)) from error
        return repr(parts)

    def _role_filter(self):
        if not self.config.role_column:
            return "", []
        return " WHERE " + identifier(self.config.role_column) + " = ?", [self.config.user_role]

    @staticmethod
    def _messages(rows: List[sqlite3.Row], user_id: str, reverse_rows: bool = True) -> List[Message]:
        messages = []
        seen = set()
        ordered_rows = reversed(rows) if reverse_rows else rows
        for row in ordered_rows:
            if row[0] is None or not str(row[0]):
                raise SourceError("消息 ID 为空，请映射一个非空且唯一的字段")
            message_id = str(row[0])
            if message_id in seen:
                raise SourceError("消息 ID 重复，请映射一个对该用户唯一的字段：" + message_id)
            seen.add(message_id)
            if row[2] is None or not str(row[2]).strip():
                continue
            messages.append(Message(message_id, user_id, str(row[4] or user_id),
                                    str(row[2]), str(row[3] or "时间未提供"), str(row[5] or "")))
        return messages
