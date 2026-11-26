#!/bin/bash
# Starts the HR Hiring web app on http://localhost:8000
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Run ./setup.sh first."
  exit 1
fi
HR_OUTLOOK_MODE="${HR_OUTLOOK_MODE:-outlook_app}" exec .venv/bin/python app.py
