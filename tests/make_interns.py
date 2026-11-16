"""Builds shared/example_interns.json: made-up intern records for testing.

Talent Hub is the real source, but we do not have access yet. These documents have the
same five fields and the same date format (UTC, for example 2026-05-19T00:00:00.000Z),
so switching to the real Talent Hub later changes nothing else.

The dates are written relative to the day this file is built, so run it again to refresh
the demo:
    python tests/make_interns.py

All people here are made up.
"""
import json
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "shared" / "example_interns.json"
TODAY = date.today()
random.seed(7)                      # the same list every time it is built

FIRST = ["Aarav", "Ananya", "Daniel", "Elena", "Emily", "Fatima", "Grace", "Hassan", "Isabel", "James",
         "Kenji", "Laura", "Leo", "Maya", "Mateo", "Nadia", "Noah", "Omar", "Priya", "Rahul", "Sofia",
         "Tomas", "Vikram", "Yuki", "Zara", "Chloe", "Diego", "Farah", "Hugo", "Ivy", "Jonas", "Keira",
         "Liam", "Mina", "Nikhil", "Olivia", "Pedro", "Rania", "Samir", "Tara"]
LAST = ["Ahmed", "Bennett", "Carter", "Dubois", "Fernandes", "Garcia", "Haddad", "Iyer", "Johansson",
        "Kim", "Larsen", "Mehta", "Nair", "Oliveira", "Pillai", "Quinn", "Rossi", "Sharma", "Tanaka",
        "Ueda", "Vargas", "Weber", "Yilmaz", "Zhang", "Novak", "Silva", "Okafor", "Mensah"]

# How many interns are working in each area, and how their specialization is written in
# Talent Hub. A few are written differently on purpose, to test the matching.
AREAS = [
    ("AI/ML", "AI/ML", 9), ("DevOps", "DevOps", 7), ("MLOps", "MLOps", 8), ("Flutter", "Flutter", 6),
    ("Web Development", "Web Development", 7), ("Backend Development", "Backend Development", 5),
    ("Data Analytics", "Data Analytics", 6), ("Data Engineering", "Data Engineering", 4),
    ("Cybersecurity", "Cybersecurity", 5), ("Software Quality Assurance", "Software Quality Assurance", 4),
    ("UI/UX Design", "UI/UX Design", 4), ("Networking", "Networking", 3),
    ("Project Management", "Project Management", 3), ("Business Analysis", "Business Analysis", 3),
    ("Digital Marketing", "Digital Marketing", 3), ("Human Resources", "Human Resources", 3),
    ("Accounting", "Accounting", 3), ("Sales", "Sales", 3),
    ("AI/ML", "AI and ML", 1),            # written differently; the matching still finds AI/ML
    ("Flutter", "Mobile Development", 1),  # the same
]

# Areas where places are coming free in the next few days, and how many.
LEAVING_SOON = {"MLOps": 3, "AI/ML": 2, "Flutter": 2, "DevOps": 1, "Data Analytics": 1,
                "Digital Marketing": 1, "Cybersecurity": 1}
# Areas where someone finished in the last few weeks (a first sync usually finds a few).
LEFT_RECENTLY = {"MLOps": 1, "Human Resources": 1, "Web Development": 1}


def utc(day: date) -> str:
    """The format Talent Hub uses: midnight UTC on that day."""
    return day.isoformat() + "T00:00:00.000Z"


def main() -> None:
    names = [f"{f} {l}" for f in FIRST for l in LAST]
    random.shuffle(names)
    docs, n, used_names = [], 4000, iter(names)

    def add(spec: str, end: date | None, months: int = 6) -> None:
        nonlocal n
        n += 1
        start = (end - timedelta(days=months * 30)) if end else TODAY - timedelta(days=60)
        docs.append({
            "traineeId": f"T{n}",
            "name": next(used_names),
            "startDate": utc(start),
            "endDate": utc(end) if end else None,
            "specialization": spec,
        })

    for area, spec, count in AREAS:
        soon, recent = LEAVING_SOON.pop(area, 0) if spec == area else 0, LEFT_RECENTLY.pop(area, 0) if spec == area else 0
        for i in range(count):
            if i < soon:                                   # finishing within the next 7 days
                end = TODAY + timedelta(days=random.randint(1, 7))
            elif i < soon + recent:                        # finished in the last few weeks
                end = TODAY - timedelta(days=random.randint(5, 30))
            else:                                          # still working for months
                end = TODAY + timedelta(days=random.randint(45, 300))
            add(spec, end)

    # ---- a few awkward records, to make sure the system copes ----
    add("Civil Engineering", TODAY + timedelta(days=3))     # no area of ours fits
    add("AI/ML", None)                                      # no end date yet
    docs.append(dict(docs[0]))                              # the same record twice
    docs.append({"name": "No Id Here", "startDate": utc(TODAY), "endDate": utc(TODAY),
                 "specialization": "AI/ML"})                # no trainee ID

    OUT.write_text(json.dumps(docs, indent=1) + "\n", encoding="utf-8")
    print(f"{len(docs)} example intern records written to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
