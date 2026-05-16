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

    `backend/api.py` 通过 importlib 加载,避免在 sys.modules 内出现两份 api 实例。
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
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        'api', os.path.join(BACKEND_DIR, 'api.py'),
    )
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    server = make_server(host, port, api.app, threaded=True, passthrough_errors=False)
    logger.info('API running on http://%s:%s', host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('API shutting down...')
        server.shutdown()
        server.server_close()
