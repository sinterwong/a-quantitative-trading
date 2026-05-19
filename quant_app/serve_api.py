"""
quant_app/serve_api.py — Flask HTTP server 启动 (P3-2)

仅负责加载 backend/api.py 中的 Flask app 并绑定 werkzeug make_server。
本模块不包含任何调度/Scheduler/IntradayMonitor 逻辑,可单独启动。
"""

from __future__ import annotations

import os
import sys


def start_api_server(host: str, port: int, logger) -> None:
    """阻塞式启动 Flask app(werkzeug make_server,threaded=True)。

    必须用 ``backend.api`` 标准包名导入: R2-4 把 Blueprint 拆出去后,
    ``backend/api_routes/*.py`` 都会 ``from backend.api import ...``。
    若改用 ``spec_from_file_location('api', …)`` 把模块塞进
    ``sys.modules['api']``, Blueprint 再 import ``backend.api`` 时找不到,
    Python 会重头执行一遍 ``backend/api.py`` —— 撞上半初始化的
    ``backend.api_routes.analysis`` 触发 ImportError。
    """
    BACKEND_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'backend',
    )
    PROJ_DIR = os.path.dirname(BACKEND_DIR)
    sys.path.insert(0, BACKEND_DIR)
    sys.path.insert(0, PROJ_DIR)
    os.environ['FLASK_ENV'] = 'production'

    from werkzeug.serving import make_server

    from backend.api import app as flask_app

    server = make_server(host, port, flask_app, threaded=True, passthrough_errors=False)
    logger.info('API running on http://%s:%s', host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('API shutting down...')
        server.shutdown()
        server.server_close()
