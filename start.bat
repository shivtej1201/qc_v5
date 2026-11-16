@echo off
echo Installing dependencies...
python -m venv venv
call venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
echo Starting Link Auditor V2...
python run.py
