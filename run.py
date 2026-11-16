"""Command line helpers.

    .venv/bin/python run.py check        is the model server running, and does it answer in JSON?
    .venv/bin/python run.py samples      add the sample CVs
    .venv/bin/python run.py once         let the Recruiter, Coordinator and Onboarding work once
    .venv/bin/python run.py plan         let the Planner read MongoDB interns and open vacancies
    .venv/bin/python run.py brief        let the Chief of staff write the morning brief
    .venv/bin/python run.py status       list the candidates and their status
"""
import argparse
import json
import sys
import time

from hiring import actions, config, db, flows, llm, supervisor, talent_hub
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
        choices=[
            "check", "samples", "once", "plan", "brief", "status",
            "seed-talent-hub", "interns", "doctor", "report", "feedback",
        ],
    )
    ap.add_argument("--reviewer", default="Supervisor", help="Feedback author (for feedback --comment)")
    ap.add_argument("--category", default="general", help="Feedback category")
    ap.add_argument("--rating", type=int, default=None, help="Optional rating 1-5")
    ap.add_argument("--target", default="", help="Optional target reference (e.g. APP-0001)")
    ap.add_argument("--comment", default="", help="Submit supervisor feedback text")
    ap.add_argument("--out", default="", help="Optional output file path for report")
    a = ap.parse_args()
    if a.command == "check":
        return check()
    if a.command == "seed-talent-hub":
        try:
            res = talent_hub.seed_sample_interns(400)
        except talent_hub.TalentHubUnavailable as exc:
            print(f"Error: {exc}")
            print(f"Check TALENT_HUB_MONGO_URI ({config.TALENT_HUB_MONGO_URI}) and ensure mongod is running.")
            return 1
        finally:
            talent_hub.close()
        print(f"Seeded {res['db']}.{res['collection']}: total={res['total']} (upserted={res['upserted']}, modified={res['modified']})")
        return 0
    if a.command == "interns":
        try:
            rows = talent_hub.list_interns(limit=1000)
        except (talent_hub.TalentHubUnavailable, talent_hub.TalentHubAuthError) as exc:
            print(f"Error: {exc}")
            print("Start the local mock server with ./start_talent_hub.sh or check TALENT_HUB_API_URL / TALENT_HUB_API_TOKEN.")
            return 1
        for r in rows:
            print(f"{r['Trainee_ID']:6}  {r['Trainee_Name']:24} {r['Training_StartDate']} -> {r['Training_EndDate']}  {r['field_of_spec_name']}")
        print(f"Total shown: {len(rows)}")
        return 0
    db.setup()
    if a.command == "samples":
        for r in actions.add_samples():
            print(("Added " + r["app_no"]) if r["ok"] else "Skipped", "-", r["file"], "" if r["ok"] else f"({r['reason']})")
    elif a.command == "once":
        for r in flows.run_all_once():
            print(r)
        if flows.NOTICE["text"]:
            print("Stopped:", flows.NOTICE["text"])
    elif a.command == "plan":
        print(flows.run_planner())
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
    elif a.command == "doctor":
        diag = supervisor.diagnostics(force=True)
        print(json.dumps(diag, indent=2))
        return 0 if diag["ready"] else 1
    elif a.command == "report":
        md = supervisor.supervisor_report_markdown()
        if a.out:
            from pathlib import Path
            Path(a.out).write_text(md, encoding="utf-8")
            print(f"Wrote supervisor report to {a.out}")
        else:
            print(md)
    elif a.command == "feedback":
        if a.comment:
            try:
                saved = supervisor.submit_feedback(
                    reviewer=a.reviewer,
                    category=a.category,
                    rating=a.rating,
                    target=a.target,
                    comment=a.comment,
                )
            except actions.ActionError as exc:
                print(f"Error: {exc}")
                return 1
            print(json.dumps(saved, indent=2))
        else:
            print(json.dumps(supervisor.list_feedback(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
