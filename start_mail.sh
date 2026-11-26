#!/bin/bash
# Starts the HR Hiring web app in mail-only mode on http://localhost:8000
# (only Add CV, Register and Mail settings are shown).
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Run ./setup.sh first."
  exit 1
fi
HR_MODE=mail HR_OUTLOOK_MODE="${HR_OUTLOOK_MODE:-auto}" exec .venv/bin/python app.py
