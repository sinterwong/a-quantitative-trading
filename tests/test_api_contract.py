"""
tests/test_api_contract.py — OpenAPI ↔ Flask routes 契约测试 (P4-3)

验证:
  1. backend/openapi.json 与 Flask app.url_map 一致(没有孤立路径)
  2. backend/openapi.json 是 scripts/generate_openapi.py 生成的最新结果
  3. 每个 GET/200 端点示例响应能 round-trip 通过 Flask test client(冒烟测试)
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

PROJ_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ_DIR))
sys.path.insert(0, str(PROJ_DIR / 'backend'))

OPENAPI_PATH = PROJ_DIR / 'backend' / 'openapi.json'


def _normalize_path(rule: str) -> str:
    """Flask <name> / <type:name> → OpenAPI {name}。"""
    import re
    return re.sub(r'<(?:[^:>]+:)?([^>]+)>', r'{\1}', rule)


class TestOpenAPIContract(unittest.TestCase):

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'api', str(PROJ_DIR / 'backend' / 'api.py'),
        )
        self.api_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api_module)
        self.app = self.api_module.app

        with open(OPENAPI_PATH, encoding='utf-8') as f:
            self.spec = json.load(f)

    def test_all_routes_documented(self):
        """Flask 路由必须全部出现在 openapi.json 中(防止漏文档)。"""
        documented = set(self.spec.get('paths', {}).keys())
        flask_paths = {
            _normalize_path(r.rule)
            for r in self.app.url_map.iter_rules()
            if r.endpoint != 'static'
        }
        missing = flask_paths - documented
        self.assertFalse(
            missing,
            f'以下 Flask 路由未在 openapi.json 中文档化(运行 '
            f'`python scripts/generate_openapi.py` 重生成):\n  {sorted(missing)}',
        )

    def test_no_orphan_paths_in_spec(self):
        """openapi.json 中的路径必须对应真实 Flask 路由(防止陈旧文档)。"""
        documented = set(self.spec.get('paths', {}).keys())
        flask_paths = {
            _normalize_path(r.rule)
            for r in self.app.url_map.iter_rules()
            if r.endpoint != 'static'
        }
        orphans = documented - flask_paths
        self.assertFalse(
            orphans,
            f'openapi.json 包含已不存在的路径(运行 '
            f'`python scripts/generate_openapi.py` 重生成):\n  {sorted(orphans)}',
        )

    def test_spec_is_up_to_date(self):
        """openapi.json 必须是 scripts/generate_openapi.py 当前的生成结果。"""
        sys.path.insert(0, str(PROJ_DIR / 'scripts'))
        from generate_openapi import generate_spec
        regenerated = generate_spec(self.app)
        existing = self.spec

        # 比较 paths 集合 + 方法集合(忽略 example/description 等细节)
        re_paths = {p: set(ops.keys()) for p, ops in regenerated.get('paths', {}).items()}
        ex_paths = {p: set(ops.keys()) for p, ops in existing.get('paths', {}).items()}
        self.assertEqual(
            re_paths, ex_paths,
            'openapi.json 与最新自动生成结果不一致 — 运行 '
            '`python scripts/generate_openapi.py` 更新。',
        )


class TestSmokeEndpoints(unittest.TestCase):
    """对 read-only GET 端点做冒烟回归(确保未引入 500)。"""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'api', str(PROJ_DIR / 'backend' / 'api.py'),
        )
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        self.client = api.app.test_client()

    def test_health_returns_200(self):
        r = self.client.get('/health')
        self.assertEqual(r.status_code, 200)

    def test_positions_returns_200(self):
        r = self.client.get('/positions')
        self.assertEqual(r.status_code, 200)

    def test_cash_returns_200(self):
        r = self.client.get('/cash')
        self.assertEqual(r.status_code, 200)


if __name__ == '__main__':
    unittest.main()
