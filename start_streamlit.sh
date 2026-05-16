#!/usr/bin/env bash
# 启动量化交易控制台(streamlit UI)。
#
# env(全部可选):
#   QUANT_UI_BACKEND_URL   后端 base URL,默认 http://127.0.0.1:5555
#   TRADING_API_KEY        后端要求时填,UI 会带 X-API-Key
#   QUANT_UI_TIMEOUT       单请求超时秒,默认 8
#
# 后端没起的话先 `python backend/main.py --mode api --port 5555`。

set -euo pipefail
cd "$(dirname "$0")"

: "${QUANT_UI_BACKEND_URL:=http://127.0.0.1:5555}"
export QUANT_UI_BACKEND_URL

exec streamlit run streamlit_app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    "$@"
