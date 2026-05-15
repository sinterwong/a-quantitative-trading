"""
core/single_instance.py — OS 级单实例锁 (P3-1)

产品定位:本系统约束"单 OS 单进程,不可多开"。本模块提供统一的 PID 文件锁,
所有进程入口(backend/main.py / 未来的 worker / cli)调用 acquire_singleton()
以确保同时只有一个实例在跑。

实现:
- O_CREAT | O_EXCL 原子创建锁文件
- fcntl.LOCK_EX 文件锁防竞态
- 验证旧 PID 是否仍存活(`os.kill(pid, 0)`)
- 退出时 atexit 自动释放

用法:

    from core.single_instance import acquire_singleton, SingletonError

    try:
        lock = acquire_singleton('quant-system')   # 阻塞,成功后持有锁直到进程退出
    except SingletonError as e:
        print(f'另一个实例在跑: PID={e.holder_pid}')
        sys.exit(1)
"""

from __future__ import annotations

import atexit
import errno
import fcntl
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger('core.single_instance')


# 默认锁文件目录(可被 acquire_singleton 的 lock_dir 覆盖)
_DEFAULT_LOCK_DIR = '/tmp'


class SingletonError(RuntimeError):
    """单实例锁获取失败。"""

    def __init__(self, message: str, holder_pid: int = 0, lock_file: str = ''):
        super().__init__(message)
        self.holder_pid = holder_pid
        self.lock_file = lock_file


class SingletonLock:
    """单实例锁句柄。

    持有锁的 fd 保留为类属性,进程退出时 atexit 自动 unlink。
    """

    def __init__(self, name: str, lock_file: str, fd: int):
        self.name = name
        self.lock_file = lock_file
        self._fd = fd

    def release(self) -> None:
        """主动释放(测试用)。生产中由 atexit 自动调用。"""
        if self._fd is None:
            return
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None
        try:
            os.unlink(self.lock_file)
        except OSError:
            pass


def _is_pid_alive(pid: int) -> bool:
    """判断给定 PID 是否仍存活。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM   # 存在但无权限


def acquire_singleton(
    name: str,
    lock_dir: str = _DEFAULT_LOCK_DIR,
) -> SingletonLock:
    """获取单实例锁。成功返回 SingletonLock,失败抛 SingletonError。

    Parameters
    ----------
    name : str
        实例名,用作锁文件名(如 'quant-system')
    lock_dir : str
        锁文件目录,默认 /tmp

    Returns
    -------
    SingletonLock

    Raises
    ------
    SingletonError
        已有同名实例在跑(返回的 holder_pid 是持有者)
    """
    Path(lock_dir).mkdir(parents=True, exist_ok=True)
    lock_file = os.path.join(lock_dir, f'{name}.pid')

    my_pid = os.getpid()

    # ── 第一关:O_CREAT|O_EXCL 原子创建 ─────────────────────────
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise SingletonError(
                f'failed to create lock file: {exc}',
                lock_file=lock_file,
            ) from exc
        # 锁文件存在,进入验证流程
        fd = None

    if fd is not None:
        # 第一个创建者:加 fcntl 锁 + 写 PID
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise SingletonError(
                f'flock failed on new lock file: {exc}',
                lock_file=lock_file,
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f'{my_pid}'.encode())
        os.fsync(fd)
        lock = SingletonLock(name, lock_file, fd)
        atexit.register(lock.release)
        logger.info('Singleton lock acquired: name=%s pid=%d file=%s',
                    name, my_pid, lock_file)
        return lock

    # ── 第二关:锁文件已存在,验证持有者是否还活着 ─────────────
    fd = os.open(lock_file, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 另一进程持有 flock → 仍在运行
        os.close(fd)
        try:
            with open(lock_file, 'r') as f:
                holder = int(f.read().strip() or '0')
        except Exception:
            holder = 0
        raise SingletonError(
            f'another instance is running (pid={holder})',
            holder_pid=holder, lock_file=lock_file,
        )

    # 拿到 flock,但锁文件存在 → 旧进程异常退出未清理。验证 PID。
    try:
        raw = os.read(fd, 64).decode().strip()
        old_pid = int(raw) if raw else 0
    except (ValueError, UnicodeDecodeError):
        old_pid = 0

    if old_pid > 0 and old_pid != my_pid and _is_pid_alive(old_pid):
        os.close(fd)
        raise SingletonError(
            f'another instance is running (pid={old_pid})',
            holder_pid=old_pid, lock_file=lock_file,
        )

    # 旧 PID 无效 / 已死 → 覆盖
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f'{my_pid}'.encode())
    os.fsync(fd)
    lock = SingletonLock(name, lock_file, fd)
    atexit.register(lock.release)
    logger.info(
        'Singleton lock acquired (stale lock cleaned): name=%s pid=%d file=%s',
        name, my_pid, lock_file,
    )
    return lock


__all__ = [
    'SingletonError',
    'SingletonLock',
    'acquire_singleton',
]
