@echo off
cd /d "%~dp0"
echo Starting 小黑量化 Web UI...
pip install streamlit plotly --quiet
streamlit run streamlit_app.py --server.port 8501 --browser.gatherUsageStats false
