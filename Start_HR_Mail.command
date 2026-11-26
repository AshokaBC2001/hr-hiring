#!/bin/bash
# Double-click on macOS to start the AI reader (if not running), launch HR Hiring (Mail Mode),
# and open http://localhost:8000 in your browser automatically.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First-time setup has not been run yet. Open Terminal and run: bash setup.sh"
  read -r -p "Press Enter to close..."
  exit 1
fi

mkdir -p data
MODEL_PID=""
cleanup() {
  if [ -n "$MODEL_PID" ] && kill -0 "$MODEL_PID" 2>/dev/null; then
    kill "$MODEL_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if ! curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
  echo "Starting AI CV reader engine (please wait a few seconds)..."
  ./start_model.sh > data/model.log 2>&1 &
  MODEL_PID=$!
  for _ in $(seq 1 50); do
    if curl -fsS http://127.0.0.1:8080/health >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi

(sleep 2 && open "http://localhost:8000") >/dev/null 2>&1 &
echo "Opening HR Hiring at http://localhost:8000 (keep this window open while using the app)..."
HR_MODE=mail HR_OUTLOOK_MODE="${HR_OUTLOOK_MODE:-auto}" .venv/bin/python app.py
