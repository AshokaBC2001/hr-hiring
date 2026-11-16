"""Seed the local sample SLT TalentHub MongoDB `interns` collection.

Usage:
    .venv/bin/python scripts/seed_talent_hub.py
    .venv/bin/python scripts/seed_talent_hub.py --count 100 --reset
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hiring import config, talent_hub  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed sample SLT TalentHub interns into MongoDB")
    ap.add_argument("--count", type=int, default=400, help="Number of sample intern records to create")
    ap.add_argument("--reset", action="store_true", help="Clear existing records before seeding")
    args = ap.parse_args()

    try:
        res = talent_hub.seed_sample_interns(count=args.count, reset=args.reset)
    except talent_hub.TalentHubUnavailable as exc:
        print(f"Error: {exc}")
        print(f"Check TALENT_HUB_MONGO_URI ({config.TALENT_HUB_MONGO_URI}) and ensure mongod is running.")
        return 1

    print(
        f"Seeded {res['db']}.{res['collection']}: "
        f"upserted={res['upserted']}, modified={res['modified']}, total={res['total']}"
    )
    sample = talent_hub.mongo_list_interns(limit=3)
    for row in sample:
        print(
            f"  {row['Trainee_ID']}  {row['Trainee_Name']:22} "
            f"{row['Training_StartDate']} -> {row['Training_EndDate']}  {row['field_of_spec_name']}"
        )
    talent_hub.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
