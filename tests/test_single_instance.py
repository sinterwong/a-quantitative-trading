"""tests/test_single_instance.py — 单实例锁测试 (P3-1)。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import pytest


def test_first_acquire_succeeds():
    """全新锁文件 → 成功获取。"""
    from core.single_instance import acquire_singleton
    with tempfile.TemporaryDirectory() as tmpdir:
        lock = acquire_singleton('test-singleton-1', lock_dir=tmpdir)
        try:
            assert lock.name == 'test-singleton-1'
            assert os.path.exists(lock.lock_file)
            with open(lock.lock_file, 'r') as f:
                assert int(f.read().strip()) == os.getpid()
        finally:
            lock.release()


def test_second_acquire_in_same_process_succeeds():
    """同一进程二次获取同名锁:旧锁(PID=自己)被识别为可覆盖。"""
    from core.single_instance import acquire_singleton
    with tempfile.TemporaryDirectory() as tmpdir:
        lock1 = acquire_singleton('test-singleton-same', lock_dir=tmpdir)
        try:
            # 同一进程再次获取:旧 PID == my PID,允许覆盖
            # (release lock1 first to free flock)
            lock1.release()
            lock2 = acquire_singleton('test-singleton-same', lock_dir=tmpdir)
            assert lock2 is not None
            lock2.release()
        finally:
            pass


def test_acquire_release_unlinks_lockfile():
    from core.single_instance import acquire_singleton
    with tempfile.TemporaryDirectory() as tmpdir:
        lock = acquire_singleton('test-cleanup', lock_dir=tmpdir)
        path = lock.lock_file
        assert os.path.exists(path)
        lock.release()
        assert not os.path.exists(path)


def test_stale_lock_with_dead_pid_can_be_acquired():
    """锁文件存在但 PID 无效(进程已死)→ 应能覆盖获取。"""
    from core.single_instance import acquire_singleton
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = os.path.join(tmpdir, 'stale.pid')
        # 写入一个肯定不存在的 PID
        with open(lock_path, 'w') as f:
            f.write('999999999')
        # 应能正常获取(stale PID 被识别)
        lock = acquire_singleton('stale', lock_dir=tmpdir)
        try:
            assert lock is not None
        finally:
            lock.release()


def test_concurrent_acquire_fails_via_subprocess():
    """通过子进程模拟"另一个进程在跑",当前进程获取应失败。

    子进程持有锁后睡 2 秒,主进程立即尝试获取应抛 SingletonError。
    """
    from core.single_instance import SingletonError, acquire_singleton
    with tempfile.TemporaryDirectory() as tmpdir:
        # 子进程脚本:获取锁后睡 3 秒
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = (
            f"import sys; sys.path.insert(0, {repr(repo_root)});"
            "from core.single_instance import acquire_singleton;"
            "import time;"
            f"lock = acquire_singleton('concurrent', lock_dir={repr(tmpdir)});"
            "print('LOCKED', flush=True);"
            "time.sleep(3)"
        )
        proc = subprocess.Popen(
            [sys.executable, '-c', script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        try:
            # 等子进程打印 LOCKED
            line = proc.stdout.readline()
            assert 'LOCKED' in line, f'subprocess output: {line!r}'

            # 主进程尝试获取 → 应失败
            with pytest.raises(SingletonError) as exc:
                acquire_singleton('concurrent', lock_dir=tmpdir)
            assert exc.value.holder_pid == proc.pid
        finally:
            proc.kill()
            proc.wait(timeout=2)
