"""
test_state_db_migration.py — legacy → canonical 一次性迁移测试
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestLegacyMigration(unittest.TestCase):

    def setUp(self):
        self._prev = os.environ.pop('QUANT_STATE_DB', None)
        self._prev_no = os.environ.pop('QUANT_STATE_DB_NO_MIGRATE', None)

    def tearDown(self):
        if self._prev is not None:
            os.environ['QUANT_STATE_DB'] = self._prev
        else:
            os.environ.pop('QUANT_STATE_DB', None)
        if self._prev_no is not None:
            os.environ['QUANT_STATE_DB_NO_MIGRATE'] = self._prev_no
        else:
            os.environ.pop('QUANT_STATE_DB_NO_MIGRATE', None)

    def test_legacy_copied_and_renamed(self):
        # 注:conftest.py 把 sqlite3.connect 重定向到共享 temp DB,所以
        # 这里用文件系统写入(bytes) 而不是 sqlite3 来构造 legacy 文件。
        # 我们关心的是迁移的"复制 + 重命名"行为,不是数据正确性。
        from core import state_db as sd
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / 'data' / 'state.db'
            legacy_dir = root / 'backend' / 'services'
            legacy_dir.mkdir(parents=True)
            legacy = legacy_dir / 'portfolio.db'
            legacy.write_bytes(b'fake-sqlite-bytes')

            with patch.object(sd, '_CANONICAL_DB', canonical), \
                 patch.object(sd, '_LEGACY_DB', legacy):
                sd.reset_migration_flag_for_tests()
                path = sd.state_db_path()
                self.assertEqual(Path(path), canonical)
                self.assertTrue(canonical.exists())
                self.assertFalse(legacy.exists(), 'legacy 应已被重命名')
                # 字节内容保留 → 证明 shutil.copy2 真的发生了
                self.assertEqual(canonical.read_bytes(), b'fake-sqlite-bytes')
                # 备份文件存在
                backups = list(legacy_dir.glob('portfolio.migrated-*.bak'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), b'fake-sqlite-bytes')

    def test_no_migrate_env_skips(self):
        from core import state_db as sd
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / 'data' / 'state.db'
            legacy_dir = root / 'backend' / 'services'
            legacy_dir.mkdir(parents=True)
            legacy = legacy_dir / 'portfolio.db'
            legacy.write_bytes(b'x')

            os.environ['QUANT_STATE_DB_NO_MIGRATE'] = '1'
            with patch.object(sd, '_CANONICAL_DB', canonical), \
                 patch.object(sd, '_LEGACY_DB', legacy):
                sd.reset_migration_flag_for_tests()
                path = sd.state_db_path()
                # 关掉迁移时,canonical 不存在 → 仍返回 canonical 路径,
                # 但 legacy 文件未被改名
                self.assertEqual(Path(path), canonical)
                self.assertTrue(legacy.exists())

    def test_fresh_install_no_legacy(self):
        from core import state_db as sd
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / 'data' / 'state.db'
            legacy = root / 'backend' / 'services' / 'portfolio.db'
            with patch.object(sd, '_CANONICAL_DB', canonical), \
                 patch.object(sd, '_LEGACY_DB', legacy):
                sd.reset_migration_flag_for_tests()
                path = sd.state_db_path()
                self.assertEqual(Path(path), canonical)

    def test_canonical_exists_skips_migration(self):
        from core import state_db as sd
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical_dir = root / 'data'
            canonical_dir.mkdir()
            canonical = canonical_dir / 'state.db'
            canonical.write_bytes(b'')  # canonical 已存在(空文件够了)
            legacy_dir = root / 'backend' / 'services'
            legacy_dir.mkdir(parents=True)
            legacy = legacy_dir / 'portfolio.db'
            legacy.write_bytes(b'legacy-bytes')

            with patch.object(sd, '_CANONICAL_DB', canonical), \
                 patch.object(sd, '_LEGACY_DB', legacy):
                sd.reset_migration_flag_for_tests()
                path = sd.state_db_path()
                self.assertEqual(Path(path), canonical)
                # canonical 已经在了 → legacy 不应被动
                self.assertTrue(legacy.exists())


if __name__ == '__main__':
    unittest.main()
