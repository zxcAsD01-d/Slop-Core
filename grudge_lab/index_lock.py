import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import AppError


@contextmanager
def index_lock(path: Path) -> Iterator[bool]:
    """进程退出时释放，避免 HTTP、CLI、MCP 重复执行同一索引。"""
    try:
        stream = path.open("a+b")
    except OSError as error:
        raise AppError("无法打开索引进程锁：" + str(error), 500) from error
    try:
        if os.name == "nt":
            import msvcrt
            stream.write(b"\0")
            stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise AppError("无法锁定索引进程：" + str(error), 500) from error
                acquired = False
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        stream.close()
