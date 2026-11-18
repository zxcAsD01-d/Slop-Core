import sqlite3
from pathlib import Path

from .errors import AppError


PEOPLE = [
    ("lin", "林澈", [
        ("09:02", "群里讨论方案时，我偏好先给结论，再说理由。"),
        ("09:05", "任何产品只要加上 AI，效率至少翻倍。"),
        ("09:08", "我说翻倍，指的是我手里这两个整理表格的任务，别扩大到所有产品。"),
        ("09:12", "做决定必须看数据，凭感觉拍板最不靠谱。"),
        ("09:15", "这次配色就用绿色吧，别问数据了，我看着顺眼。"),
        ("09:19", "我做的方案从来不用改第二遍。"),
        ("09:21", "刚才那版方案我又改了三处，主要是标题、排序和预算。"),
        ("09:24", "用词可以尖锐，别把我的玩笑当事实，讨论就针对我说过的话。"),
        ("09:28", "补充：方案不用改第二遍是我吹牛，刚刚这条我收回。"),
        ("09:31", "讨论别只甩一个结论，拿出具体例子来。"),
        ("09:35", "我允许这个测试程序记录这些虚构聊天，拿观点互相吐槽就行。"),
        ("09:38", "绿色那个方案我依然喜欢；但如果转化测试差，就换。"),
    ]),
    ("zhou", "周野", [
        ("10:02", "我喜欢短句，能三句话讲完就别写三页。"),
        ("10:05", "开会绝不能超过十分钟，超过就是浪费时间。"),
        ("10:08", "关于为什么开会不能超过十分钟，我准备了四十页的分享。"),
        ("10:12", "我选工具只有一个标准：能不能省时间。"),
        ("10:15", "为了把这个快捷键改得顺手，我研究了整个下午。"),
        ("10:19", "没有测过的方案我不会随便下结论。"),
        ("10:21", "这个工具我还没用，但界面看着就慢。"),
        ("10:24", "修正一下，界面看着慢是第一印象，我还需要实测。"),
        ("10:28", "笑死，刚刚我讲效率讲了半小时。这个梗可以留着。"),
        ("10:32", "测试里可以吐槽我的发言，记得带原话。"),
    ]),
    ("xu", "许知夏", [
        ("11:02", "我喜欢结构清楚的论证，最好分成事实、假设和结论。"),
        ("11:05", "任何新功能都必须先写文档。"),
        ("11:08", "这个按钮先做出来再说，文档下周补。"),
        ("11:12", "补充：紧急修复可以先改后补文档，刚才的任何说得太满。"),
        ("11:15", "简单说就是，这个简单方案有十二个前置条件。"),
        ("11:19", "我不反对被质疑，拿原话和例子来就行。"),
        ("11:24", "我偏好少用形容词，多给可以验证的描述。"),
        ("11:28", "这只是虚构测试数据，同意保留观点和吐槽偏好。"),
    ]),
]


def create_sample(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.exists():
        raise AppError("文件已存在，未覆盖：" + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        with connection:
            _create_tables(connection)
            _insert_people(connection)
    finally:
        connection.close()
    return path


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE chat_messages (
            message_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            author_id TEXT NOT NULL,
            author_name TEXT NOT NULL,
            role TEXT NOT NULL,
            body TEXT NOT NULL,
            sent_at TEXT NOT NULL
        );
        CREATE INDEX messages_by_author ON chat_messages(author_id, sent_at);
        CREATE TABLE sample_info (description TEXT NOT NULL);
        INSERT INTO sample_info VALUES ('全部人物和消息均为虚构；用于验证归属、改口与语境。');
    """)


def _insert_people(connection: sqlite3.Connection) -> None:
    for user_id, name, messages in PEOPLE:
        for index, (time, body) in enumerate(messages, 1):
            connection.execute(
                "INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id + "-" + str(index), "demo-room", user_id, name,
                 "user", body, "2026-11-17T" + time + ":00+08:00"),
            )
        connection.execute(
            "INSERT INTO chat_messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id + "-bot", "demo-room", user_id, "测试机器人", "assistant",
             "这是机器人生成的内容，不能作为该用户的发言证据。", "2026-11-17T12:00:00+08:00"),
        )
