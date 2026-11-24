@echo off
REM Double-click to start the MyAssays processor (opens in your browser).
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: setting up Python packages...
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m streamlit run app.py
pause
