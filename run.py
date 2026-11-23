"""Command line helpers.

    .venv/bin/python run.py check          is the model server running, and does it answer in JSON?
    .venv/bin/python run.py samples        add the sample CVs
    .venv/bin/python run.py once           let the Recruiter, Coordinator and Onboarding work once
    .venv/bin/python run.py brief          let the Chief of staff write the morning brief
    .venv/bin/python run.py status         list the candidates and their status
    .venv/bin/python run.py outlook-status check Microsoft Outlook integration & auth status
    .venv/bin/python run.py outlook-login  sign in to Microsoft Outlook using Device Code Flow
    .venv/bin/python run.py outlook-test   test active Outlook delivery to --target email
"""
import argparse
import json
import sys
import time

from hiring import actions, config, db, flows, llm, outlook
from hiring.checks import parse_json_object


def check() -> int:
    h = llm.health(force=True)
    print(f"Model server {config.MODEL_URL}: {h['detail']}")
    if not h["ok"]:
        return 1
    print(f"Model: {h['model']}. Asking a test question...")
    t = time.time()
    try:
        text = llm.chat([{"role": "user", "content": 'Reply with this JSON object only: {"ready": true, "colour": "green"}'}],
                        json_mode=True, max_tokens=50)
    except llm.ModelUnavailable as e:
        print("Failed:", e)
        return 1
    print(f"Answer in {time.time() - t:.1f} s: {text.strip()[:200]}")
    try:
        parse_json_object(text)
        print("OK: the model answers in JSON. You can start the app.")
        return 0
    except ValueError:
        print("The answer was not JSON. The app will still work, but expect more fixes and fallbacks.")
        return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="HR Hiring helpers")
    ap.add_argument(
        "command",
        choices=["check", "samples", "once", "brief", "status", "outlook-status", "outlook-login", "outlook-test"],
    )
    ap.add_argument("--target", default="", help="Recipient email for outlook-test")
    a = ap.parse_args()
    if a.command == "check":
        return check()
    db.setup()
    actions.upgrade_scoring()
    if a.command == "samples":
        for r in actions.add_samples():
            print(("Added " + r["app_no"]) if r["ok"] else "Skipped", "-", r["file"], "" if r["ok"] else f"({r['reason']})")
    elif a.command == "once":
        for r in flows.run_all_once():
            print(r)
        if flows.NOTICE["text"]:
            print("Stopped:", flows.NOTICE["text"])
    elif a.command == "brief":
        print(flows.run_chief())
        with db.Session() as s:
            from sqlalchemy import select
            b = s.scalar(select(db.Brief).order_by(db.Brief.id.desc()).limit(1))
            if b:
                print("\n" + b.text)
    elif a.command == "status":
        for c in db.candidates_in(set(config.STATUSES)):
            print(f"{c.app_no}  {c.name or '-':28} {config.STATUSES[c.status][0]:32} {c.area or '-':10} "
                  f"{c.score if c.score is not None else '-'}")
    elif a.command == "outlook-status":
        print(json.dumps(outlook.public_settings(), indent=2))
    elif a.command == "outlook-login":
        try:
            st = outlook.start_device_flow()
        except outlook.OutlookError as exc:
            print(f"Error: {exc}")
            return 1
        print(st.get("message") or f"Go to {st['verification_uri']} and enter code {st['user_code']}")
        while True:
            cur = outlook.auth_status()
            if cur.get("signed_in"):
                acct = cur.get("account") or {}
                print(f"Signed in as {acct.get('username') or acct.get('name')}")
                return 0
            if cur.get("status") == "error":
                print(f"Sign-in failed: {cur.get('error')}")
                return 1
            time.sleep(2)
    elif a.command == "outlook-test":
        cfg = outlook.raw_settings()
        acct = outlook.get_signed_in_account(cfg)
        target = a.target or cfg.get("test_recipient") or (acct.get("username") if acct else "") or cfg.get("smtp_user") or ""
        try:
            res = outlook.send_test(target)
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
