@echo off
setlocal
cd /d %~dp0
if not exist .venv (
  py -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if not exist .env copy .env.example .env
python scripts\check_config.py
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
