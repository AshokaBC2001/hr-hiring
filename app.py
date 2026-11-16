"""Starts the HR Hiring web app on http://localhost:8000 (or configured HR_APP_HOST:PORT).

    .venv/bin/python app.py
"""
import uvicorn

from hiring import config, llm, talent_hub

if __name__ == "__main__":
    h = llm.health(force=True)
    th = talent_hub.health(force=True)
    auth = bool((config.AUTH_USER and config.AUTH_PASS) or config.ACCESS_TOKEN)
    print("\nHR Hiring")
    print(f"  Open in your browser: http://{config.APP_HOST}:{config.APP_PORT}")
    print(f"  Model server: {config.MODEL_URL} -> {'running' if h['ok'] else 'NOT running'}"
          + ("" if h["ok"] else (" (rules-only fallback active)" if config.ALLOW_RULES_ONLY else " (start it with ./start_model.sh)")))
    print(f"  TalentHub ({th.get('mode', talent_hub.resolve_mode())}): {'connected' if th['ok'] else th.get('detail', 'offline')}")
    print(f"  Hosting auth: {'enabled' if auth else 'disabled (local dev)'}")
    print("  Stop with Control+C\n")
    uvicorn.run(
        "hiring.server:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="warning",
    )
