"""
scripts/generate_openapi.py — 由 Flask app.url_map 自动生成 OpenAPI 3.0 spec (P4-3)

从 backend/api.py 加载 Flask app,扫描所有注册路由及其 docstring/参数,
输出 backend/openapi.json。

用法:
    python scripts/generate_openapi.py
    python scripts/generate_openapi.py --check     # 仅校验是否需要重新生成
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
BACKEND_DIR = os.path.join(PROJ_DIR, 'backend')

sys.path.insert(0, PROJ_DIR)
sys.path.insert(0, BACKEND_DIR)


# 忽略路径: Flask static、内部健康检查
_IGNORE_RULES = {'/static/<path:filename>'}
# 路径参数转换: <name> 或 <type:name> → {name}
_PATH_PARAM_RE = re.compile(r'<(?:[^:>]+:)?([^>]+)>')


def _load_flask_app():
    """加载 backend/api.py 的 Flask app。

    必须用 ``backend.api`` 的标准包名导入: R2-4 把 11 个 Blueprint
    拆出去后, 每个 ``backend/api_routes/*.py`` 都会 ``from backend.api
    import ...`` 共享 helper。若改用 ``spec_from_file_location('api', …)``
    把模块塞进 ``sys.modules['api']``, Blueprint 模块再 import
    ``backend.api`` 时 sys.modules 里没有匹配项, Python 会重头再执行一遍
    ``backend/api.py`` —— 在第二次执行的底部 import 撞上半初始化的
    ``backend.api_routes.analysis`` 就抛 ImportError (circular)。
    """
    from backend.api import app
    return app


def _convert_path(flask_rule: str) -> str:
    """Flask <type:name> → OpenAPI {name}。"""
    return _PATH_PARAM_RE.sub(r'{\1}', flask_rule)


def _extract_path_params(flask_rule: str) -> List[Dict[str, Any]]:
    """从 Flask rule 提取 path 参数。"""
    params = []
    for m in _PATH_PARAM_RE.finditer(flask_rule):
        params.append({
            'name': m.group(1),
            'in': 'path',
            'required': True,
            'schema': {'type': 'string'},
        })
    return params


def _first_doc_line(doc: str) -> str:
    """取 docstring 第一行作为 summary。"""
    if not doc:
        return ''
    for line in doc.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ''


# 全局响应信封 schema:所有 ok(data, ...) 响应包含的字段。
_ENVELOPE_SUCCESS_SCHEMA = {
    'type': 'object',
    'required': ['status', 'timestamp'],
    'properties': {
        'status':    {'type': 'string', 'enum': ['ok']},
        'timestamp': {'type': 'string'},
    },
    'additionalProperties': True,
}

_ENVELOPE_ERROR_SCHEMA = {
    'type': 'object',
    'required': ['status', 'error', 'timestamp'],
    'properties': {
        'status':    {'type': 'string', 'enum': ['error']},
        'error':     {'type': 'string'},
        'timestamp': {'type': 'string'},
    },
    'additionalProperties': True,
}


def generate_spec(app) -> Dict[str, Any]:
    """扫描 Flask app.url_map,生成 OpenAPI 3.0 spec dict。"""
    paths: Dict[str, Dict[str, Any]] = {}

    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if rule.rule in _IGNORE_RULES or rule.endpoint == 'static':
            continue
        view = app.view_functions.get(rule.endpoint)
        if view is None:
            continue
        oapi_path = _convert_path(rule.rule)
        path_params = _extract_path_params(rule.rule)
        summary = _first_doc_line(view.__doc__ or '')

        methods = sorted(m for m in rule.methods if m not in ('HEAD', 'OPTIONS'))
        if not methods:
            continue

        path_obj = paths.setdefault(oapi_path, {})
        for method in methods:
            op: Dict[str, Any] = {
                'summary': summary or rule.endpoint,
                'operationId': f'{method.lower()}_{rule.endpoint}',
                'responses': {
                    '200': {
                        'description': 'Success (ok envelope)',
                        'content': {
                            'application/json': {'schema': _ENVELOPE_SUCCESS_SCHEMA},
                        },
                    },
                    '4XX': {
                        'description': 'Client error (error envelope)',
                        'content': {
                            'application/json': {'schema': _ENVELOPE_ERROR_SCHEMA},
                        },
                    },
                    '5XX': {'description': 'Server error'},
                },
            }
            if path_params:
                op['parameters'] = path_params
            path_obj[method.lower()] = op

    return {
        'openapi': '3.0.0',
        'info': {
            'title': 'A-Share Quant Trading API',
            'version': '2.0.0',
            'description': 'Auto-generated from Flask app.url_map (P4-3 + P2-2 schema).',
        },
        'servers': [{'url': 'http://127.0.0.1:5555'}],
        'paths': paths,
        'components': {
            'schemas': {
                'EnvelopeSuccess': _ENVELOPE_SUCCESS_SCHEMA,
                'EnvelopeError':   _ENVELOPE_ERROR_SCHEMA,
            },
        },
    }


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true',
                        help='仅校验生成结果是否与磁盘一致(用于 CI)')
    parser.add_argument('--out', default=os.path.join(BACKEND_DIR, 'openapi.json'),
                        help='输出路径(默认 backend/openapi.json)')
    args = parser.parse_args()

    app = _load_flask_app()
    spec = generate_spec(app)
    new_json = json.dumps(spec, ensure_ascii=False, indent=2) + '\n'

    if args.check:
        existing = ''
        if os.path.exists(args.out):
            with open(args.out, encoding='utf-8') as f:
                existing = f.read()
        if existing.strip() != new_json.strip():
            print(f'[generate_openapi] {args.out} is OUTDATED — run without --check to regenerate.')
            sys.exit(1)
        print(f'[generate_openapi] {args.out} is up-to-date.')
        return

    with open(args.out, 'w', encoding='utf-8') as f:
        f.write(new_json)
    n_paths = len(spec['paths'])
    print(f'[generate_openapi] wrote {n_paths} paths to {args.out}')


if __name__ == '__main__':
    main()
