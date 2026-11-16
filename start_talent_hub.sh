#!/bin/bash
# Starts the local read-only mock SLT TalentHub API on http://127.0.0.1:5000/api
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Run ./setup.sh first."
  exit 1
fi
exec .venv/bin/python -m hiring.talent_hub_mock
