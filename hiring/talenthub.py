"""Reads the intern records from Talent Hub.

Talent Hub is a MongoDB database that belongs to another team, so this file only ever
READS from it. Nothing here writes to Talent Hub.

Two sources, chosen with TALENTHUB_SOURCE in config.py:
    "example"  reads shared/example_interns.json  (for testing without access)
    "mongodb"  reads the real Talent Hub with pymongo (a read-only user)

Both give the same 5 fields, so the rest of the system does not care which one is used:
    trainee_id, name, start_date, end_date, specialization

The field names inside a Talent Hub document are in config.TALENTHUB_FIELDS. When the
real names are known, only that mapping changes.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from . import config, db
from .checks import resolve_area

LOCAL_TZ = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET_HOURS))


class TalentHubUnavailable(RuntimeError):
    """Talent Hub could not be read. The system keeps working with the last copy."""


def as_local_date(value) -> str:
    """Talent Hub stores a date and time in UTC, for example 2026-05-19T00:00:00.000Z.
    We only need the day, read in local time, and we return it as YYYY-MM-DD."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):                     # pymongo gives a datetime
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).date().isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):        # a plain date: take it as written
        return text
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:                               # no zone given: take the date as written
        return dt.date().isoformat()
    return dt.astimezone(LOCAL_TZ).date().isoformat()


def _one(doc: dict) -> dict | None:
    """Turns one Talent Hub document into our 5 fields. Returns None if it has no ID."""
    f = config.TALENTHUB_FIELDS
    trainee_id = str(doc.get(f["trainee_id"], "") or "").strip()
    if not trainee_id:
        return None
    return {
        "trainee_id": trainee_id,
        "name": str(doc.get(f["name"], "") or "").strip()[:120],
        "start_date": as_local_date(doc.get(f["start_date"])),
        "end_date": as_local_date(doc.get(f["end_date"])),
        "specialization": str(doc.get(f["specialization"], "") or "").strip()[:80],
    }


# ---- the two sources ---------------------------------------------------------------
def _from_example() -> list[dict]:
    path = config.TALENTHUB_EXAMPLE_FILE
    if not path.exists():
        raise TalentHubUnavailable(f"The example intern file is missing: {path.name}")
    try:
        docs = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise TalentHubUnavailable(f"The example intern file is not valid JSON: {e}") from e
    return docs if isinstance(docs, list) else []


def _from_mongodb() -> list[dict]:
    try:
        from pymongo import MongoClient
    except ImportError as e:                            # pragma: no cover - needs the real database
        raise TalentHubUnavailable("pymongo is not installed. Run: pip install pymongo") from e
    try:                                                # pragma: no cover - needs the real database
        client = MongoClient(config.TALENTHUB_URL, serverSelectionTimeoutMS=8000)
        collection = client[config.TALENTHUB_DB][config.TALENTHUB_COLLECTION]
        wanted = {name: 1 for name in config.TALENTHUB_FIELDS.values()}
        docs = list(collection.find({}, wanted))
        client.close()
        return docs
    except Exception as e:                              # pragma: no cover - needs the real database
        raise TalentHubUnavailable(f"Talent Hub could not be read: {e}") from e


def read_interns() -> list[dict]:
    """Every intern record from the chosen source, as our 5 fields."""
    docs = _from_mongodb() if config.TALENTHUB_SOURCE == "mongodb" else _from_example()
    out, seen = [], set()
    for doc in docs:
        row = _one(doc) if isinstance(doc, dict) else None
        if row and row["trainee_id"] not in seen:       # the first record of an ID wins
            seen.add(row["trainee_id"])
            out.append(row)
    return out


# ---- copying the records into our own register --------------------------------------
def match_area(specialization: str, area_names: list[str]) -> str:
    """Matches a Talent Hub specialization (for example "AI/ML") to one of our areas.
    Returns "" when nothing fits, so the HR officer can see it and decide."""
    return resolve_area(specialization, area_names) or ""


def sync() -> dict:
    """Reads Talent Hub and copies the records into our interns table.
    New interns are added, changed ones (such as a longer end date) are updated."""
    rows = read_interns()
    area_names = [a.name for a in db.active_areas()]
    added = updated = unknown = 0
    at = db.now()
    with db.Session() as s:
        have = {i.trainee_id: i for i in s.scalars(db.select(db.Intern))}
        for row in rows:
            area = match_area(row["specialization"], area_names)
            if not area:
                unknown += 1
            intern = have.get(row["trainee_id"])
            if intern is None:
                s.add(db.Intern(**row, area=area, synced_at=at))
                added += 1
            else:
                changed = any(getattr(intern, k) != v for k, v in row.items()) or intern.area != area
                for k, v in row.items():
                    setattr(intern, k, v)
                intern.area = area
                intern.synced_at = at
                updated += changed
        s.commit()
    db.set_setting("interns_synced_at", at)
    return {"read": len(rows), "added": added, "updated": updated, "unknown_specialization": unknown}
