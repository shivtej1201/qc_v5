#!/bin/bash
echo "Installing dependencies..."
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
echo "Starting Link Auditor V2..."
python3 run.py
