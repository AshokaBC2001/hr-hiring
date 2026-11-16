"""SLT TalentHub integration: sample MongoDB repository and read-only API client.

In production, TalentHub encrypts ``Trainee_Name`` at rest in MongoDB (AES-256)
and exposes decrypted records through a Bearer-authenticated read-only HTTP API
(``GET /api/interns`` and ``GET /api/interns/:id``). This module owns both:

1. The local sample MongoDB ``interns`` repository (used by the seeder and the
   local mock TalentHub server).
2. The read-only HTTP client used by ``hr-hiring`` to query either the local
   mock server or the live TalentHub API.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from bson import ObjectId
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.errors import OperationFailure, PyMongoError

from . import checks, config

REQUIRED_FIELDS = (
    "Trainee_ID",
    "Trainee_Name",
    "Training_StartDate",
    "Training_EndDate",
    "field_of_spec_name",
)
OPTIONAL_FIELDS = ("Trainee_Email", "Institute")

INTERN_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": list(REQUIRED_FIELDS),
        "properties": {
            "Trainee_ID": {"bsonType": "string", "minLength": 1},
            "Trainee_Name": {"bsonType": "string", "minLength": 1},
            "Training_StartDate": {"bsonType": "date"},
            "Training_EndDate": {"bsonType": "date"},
            "field_of_spec_name": {"bsonType": "string", "minLength": 1},
            "Trainee_Email": {"bsonType": "string"},
            "Institute": {"bsonType": "string"},
        },
    }
}


class TalentHubUnavailable(Exception):
    """The TalentHub API or MongoDB server is offline or returned an error."""


class TalentHubAuthError(Exception):
    """The TalentHub API rejected the configured bearer token."""


_client_lock = threading.Lock()
_client: MongoClient | None = None
_client_key: tuple[str, int] | None = None

_mongo_collection_override: Any | None = None
_backend: Any | None = None
_http_transport: httpx.BaseTransport | None = None
_health_cache: tuple[float, dict[str, Any]] = (0.0, {})


def set_backend(backend: Any | None) -> None:
    """Inject a test backend implementing ``list_interns`` / ``get_intern``."""
    global _backend, _health_cache
    _backend = backend
    _health_cache = (0.0, {})


def set_http_transport(transport: httpx.BaseTransport | None) -> None:
    """Inject an ``httpx.BaseTransport`` (e.g. ``MockTransport``) for tests."""
    global _http_transport, _health_cache
    _http_transport = transport
    _health_cache = (0.0, {})


def set_mongo_collection(coll: Any | None) -> None:
    """Inject a fake/test MongoDB collection for isolated repository tests."""
    global _mongo_collection_override
    _mongo_collection_override = coll


def close() -> None:
    """Close the shared MongoClient and clear cached state."""
    global _client, _client_key, _health_cache
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None
        _client_key = None
    _health_cache = (0.0, {})


def _get_client() -> MongoClient:
    """Return the reused MongoClient configured for a single-process local app."""
    global _client, _client_key
    uri = config.TALENT_HUB_MONGO_URI
    timeout_ms = max(int(config.TALENT_HUB_TIMEOUT_MS), 100)
    key = (uri, timeout_ms)
    with _client_lock:
        if _client is not None and _client_key == key:
            return _client
        if _client is not None:
            _client.close()
        _client = MongoClient(
            uri,
            # Local single-process FastAPI + APScheduler has low concurrency; cap pool
            # at 10 to bound server memory (~1 MB/conn) while keeping burst headroom.
            maxPoolSize=10,
            # Avoid holding open idle connections when the sample DB sits unused.
            minPoolSize=0,
            # Reclaim idle sockets after 5 minutes to prevent churn on active runs.
            maxIdleTimeMS=300_000,
            # Fail promptly on unreachable host during local/CI runs.
            connectTimeoutMS=timeout_ms,
            # Bound server selection so /api/state degrades fast if mongod is stopped.
            serverSelectionTimeoutMS=timeout_ms,
            # Cap single read/seed socket wait at 10 seconds.
            socketTimeoutMS=10_000,
            # Preserve UTC tzinfo on BSON datetime values for .000Z serialization.
            tz_aware=True,
        )
        _client_key = key
        return _client


def _get_collection():
    if _mongo_collection_override is not None:
        return _mongo_collection_override
    client = _get_client()
    return client[config.TALENT_HUB_DB_NAME][config.TALENT_HUB_COLLECTION]


# ---- Date and document normalization -----------------------------------------------
def parse_utc_date(value: str | datetime) -> datetime:
    """Parse an ISO-8601 UTC string (e.g. '2025-01-01T00:00:00.000Z') or datetime to UTC."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Date value must be a non-empty ISO-8601 string or datetime.")
    raw = value.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 date: {value!r}") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc_date(value: str | datetime) -> str:
    """Format a datetime or ISO string as 'YYYY-MM-DDTHH:MM:SS.000Z'."""
    dt = parse_utc_date(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def normalize_intern(doc: dict[str, Any]) -> dict[str, Any]:
    """Normalize a MongoDB or TalentHub API intern record into canonical API JSON."""
    out: dict[str, Any] = {}
    if "_id" in doc and doc["_id"] is not None:
        out["_id"] = str(doc["_id"])
    out["Trainee_ID"] = str(doc.get("Trainee_ID", "")).strip()
    out["Trainee_Name"] = str(doc.get("Trainee_Name", "")).strip()
    out["Training_StartDate"] = format_utc_date(doc["Training_StartDate"])
    out["Training_EndDate"] = format_utc_date(doc["Training_EndDate"])
    out["field_of_spec_name"] = str(doc.get("field_of_spec_name", "")).strip()
    for opt in OPTIONAL_FIELDS:
        if doc.get(opt):
            out[opt] = str(doc[opt]).strip()
    return out


def to_storage_doc(record: dict[str, Any]) -> dict[str, Any]:
    """Validate and convert an intern input record into a MongoDB BSON document."""
    doc: dict[str, Any] = {}
    if "_id" in record and record["_id"] is not None:
        raw_id = record["_id"]
        if isinstance(raw_id, ObjectId):
            doc["_id"] = raw_id
        elif isinstance(raw_id, str) and ObjectId.is_valid(raw_id):
            doc["_id"] = ObjectId(raw_id)
        else:
            raise ValueError(f"Invalid ObjectId for _id: {raw_id!r}")

    for field in ("Trainee_ID", "Trainee_Name", "field_of_spec_name"):
        val = record.get(field)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{field} must be a non-empty string.")
        doc[field] = val.strip()

    start_dt = parse_utc_date(record["Training_StartDate"])
    end_dt = parse_utc_date(record["Training_EndDate"])
    if end_dt <= start_dt:
        raise ValueError("Training_EndDate must be after Training_StartDate.")
    doc["Training_StartDate"] = start_dt
    doc["Training_EndDate"] = end_dt

    for opt in OPTIONAL_FIELDS:
        val = record.get(opt)
        if isinstance(val, str) and val.strip():
            doc[opt] = val.strip()
    return doc


# ---- MongoDB sample repository -----------------------------------------------------
def ensure_collection() -> dict[str, Any]:
    """Ensure the interns collection, strict $jsonSchema validator, and indexes exist."""
    coll = _get_collection()
    if _mongo_collection_override is None:
        db = _get_client()[config.TALENT_HUB_DB_NAME]
        name = config.TALENT_HUB_COLLECTION
        try:
            if name not in db.list_collection_names():
                db.create_collection(
                    name,
                    validator=INTERN_VALIDATOR,
                    validationLevel="strict",
                    validationAction="error",
                )
            else:
                db.command({
                    "collMod": name,
                    "validator": INTERN_VALIDATOR,
                    "validationLevel": "strict",
                    "validationAction": "error",
                })
        except (PyMongoError, OperationFailure) as exc:
            raise TalentHubUnavailable(f"Failed to configure MongoDB collection {name}: {exc}") from exc

    coll.create_index([("Trainee_ID", ASCENDING)], unique=True, name="Trainee_ID_1")
    coll.create_index([("field_of_spec_name", ASCENDING)], name="field_of_spec_name_1")
    coll.create_index([("Training_EndDate", ASCENDING)], name="Training_EndDate_1")
    return {"db": config.TALENT_HUB_DB_NAME, "collection": config.TALENT_HUB_COLLECTION}


def mongo_health() -> dict[str, Any]:
    """Ping MongoDB directly for CLI/seeder diagnostics."""
    if _mongo_collection_override is not None:
        return {"ok": True, "db": config.TALENT_HUB_DB_NAME, "detail": "Test MongoDB collection in use."}
    try:
        _get_client().admin.command("ping")
        return {"ok": True, "db": config.TALENT_HUB_DB_NAME, "detail": "MongoDB is running."}
    except PyMongoError as exc:
        return {"ok": False, "db": config.TALENT_HUB_DB_NAME, "detail": f"MongoDB unreachable: {exc}"}


def upsert_interns(records: list[dict[str, Any]]) -> dict[str, int]:
    """Validate and idempotently upsert intern records keyed by Trainee_ID."""
    docs = [to_storage_doc(r) for r in records]
    coll = _get_collection()
    if not docs:
        return {"upserted": 0, "modified": 0, "total": coll.count_documents({})}
    ops = [UpdateOne({"Trainee_ID": d["Trainee_ID"]}, {"$set": d}, upsert=True) for d in docs]
    try:
        res = coll.bulk_write(ops, ordered=False)
        total = coll.count_documents({})
    except PyMongoError as exc:
        raise TalentHubUnavailable(f"MongoDB write failed: {exc}") from exc
    return {"upserted": res.upserted_count, "modified": res.modified_count, "total": total}


def _filter_rows(rows: list[dict[str, Any]], specialization: str = "", q: str = "", limit: int = 200) -> list[dict[str, Any]]:
    spec = (specialization or "").strip().lower()
    query = (q or "").strip().lower()
    out = []
    for row in rows:
        if spec and row["field_of_spec_name"].lower() != spec:
            continue
        if query:
            hay = " ".join([
                row.get("Trainee_ID", ""),
                row.get("Trainee_Name", ""),
                row.get("field_of_spec_name", ""),
                row.get("Trainee_Email", ""),
                row.get("Institute", ""),
            ]).lower()
            if query not in hay:
                continue
        out.append(row)
        if limit > 0 and len(out) >= limit:
            break
    return out


def mongo_list_interns(specialization: str = "", q: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Read interns directly from the MongoDB collection (used by mock API & CLI)."""
    coll = _get_collection()
    mongo_filter: dict[str, Any] = {}
    spec = (specialization or "").strip()
    query = (q or "").strip()
    if spec:
        mongo_filter["field_of_spec_name"] = {"$regex": f"^{re.escape(spec)}$", "$options": "i"}
    if query:
        rx = {"$regex": re.escape(query), "$options": "i"}
        mongo_filter["$or"] = [
            {"Trainee_ID": rx},
            {"Trainee_Name": rx},
            {"field_of_spec_name": rx},
            {"Trainee_Email": rx},
            {"Institute": rx},
        ]
    try:
        cursor = coll.find(mongo_filter).sort("Trainee_ID", ASCENDING)
        if limit > 0:
            cursor = cursor.limit(limit)
        return [normalize_intern(d) for d in cursor]
    except PyMongoError as exc:
        raise TalentHubUnavailable(f"MongoDB query failed: {exc}") from exc


def mongo_get_intern(identifier: str) -> dict[str, Any] | None:
    """Look up a single intern in MongoDB by Trainee_ID or MongoDB _id."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    coll = _get_collection()
    clauses: list[dict[str, Any]] = [{"Trainee_ID": ident}]
    if ObjectId.is_valid(ident):
        clauses.append({"_id": ObjectId(ident)})
    query: dict[str, Any] = clauses[0] if len(clauses) == 1 else {"$or": clauses}
    try:
        doc = coll.find_one(query)
    except PyMongoError as exc:
        raise TalentHubUnavailable(f"MongoDB lookup failed: {exc}") from exc
    return normalize_intern(doc) if doc else None


# ---- Read-only TalentHub HTTP client -----------------------------------------------
def _api_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    token = (config.TALENT_HUB_API_TOKEN or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_get(path: str) -> httpx.Response:
    url = f"{config.TALENT_HUB_API_URL.rstrip('/')}{path}"
    timeout = max(float(config.TALENT_HUB_TIMEOUT_MS) / 1000.0, 0.1)
    try:
        with httpx.Client(timeout=timeout, transport=_http_transport) as client:
            return client.get(url, headers=_api_headers())
    except httpx.HTTPError as exc:
        raise TalentHubUnavailable(f"TalentHub API at {config.TALENT_HUB_API_URL} is unreachable.") from exc


def _raise_for_api_error(resp: httpx.Response) -> None:
    if resp.status_code in (401, 403):
        raise TalentHubAuthError("TalentHub API rejected the authorization token.")
    if resp.status_code >= 500:
        raise TalentHubUnavailable(f"TalentHub API answered with HTTP {resp.status_code}.")
    if resp.status_code != 200:
        raise TalentHubUnavailable(f"TalentHub API answered unexpectedly with HTTP {resp.status_code}.")


def health(force: bool = False) -> dict[str, Any]:
    """Check whether the TalentHub read-only API is reachable (10-second cache)."""
    global _health_cache
    if _backend is not None:
        return {"ok": True, "url": "test", "detail": "Test TalentHub backend in use."}
    t, cached = _health_cache
    if not force and cached and time.time() - t < 10:
        return cached
    api_url = config.TALENT_HUB_API_URL.rstrip("/")
    result: dict[str, Any] = {"ok": False, "url": api_url, "detail": ""}
    try:
        resp = _http_get("/interns")
        if resp.status_code == 200:
            data = resp.json()
            count = len(data) if isinstance(data, list) else None
            result.update(ok=True, count=count, detail="TalentHub API is running.")
        elif resp.status_code in (401, 403):
            result["detail"] = "TalentHub API rejected the authorization token."
        else:
            result["detail"] = f"TalentHub API answered with HTTP {resp.status_code}."
    except (TalentHubUnavailable, ValueError) as exc:
        result["detail"] = f"{exc} Start the local mock server with ./start_talent_hub.sh"
    _health_cache = (time.time(), result)
    return result


def list_interns(specialization: str = "", q: str = "", limit: int = 200) -> list[dict[str, Any]]:
    """Retrieve active interns from TalentHub (or injected test backend)."""
    if _backend is not None:
        rows = [normalize_intern(r) for r in _backend.list_interns()]
        return _filter_rows(rows, specialization=specialization, q=q, limit=limit)
    resp = _http_get("/interns")
    _raise_for_api_error(resp)
    payload = resp.json()
    if not isinstance(payload, list):
        raise TalentHubUnavailable("TalentHub API returned an unexpected payload for /interns.")
    rows = [normalize_intern(item) for item in payload]
    return _filter_rows(rows, specialization=specialization, q=q, limit=limit)


def get_intern(identifier: str) -> dict[str, Any] | None:
    """Retrieve one intern by Trainee_ID or MongoDB _id from TalentHub."""
    ident = (identifier or "").strip()
    if not ident:
        return None
    if _backend is not None:
        doc = _backend.get_intern(ident)
        return normalize_intern(doc) if doc else None
    resp = _http_get(f"/interns/{quote(ident, safe='')}")
    if resp.status_code == 404:
        return None
    _raise_for_api_error(resp)
    return normalize_intern(resp.json())


# ---- Planner / analytics helpers (reads directly from TalentHub / MongoDB) ---------
LOCAL_TZ = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET_HOURS))


def as_local_date(value: Any) -> str:
    """Convert UTC ISO date strings or datetimes into YYYY-MM-DD in local Sri Lanka time."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(LOCAL_TZ).date().isoformat()
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        return dt.date().isoformat()
    return dt.astimezone(LOCAL_TZ).date().isoformat()


def match_area(specialization: str, area_names: list[str]) -> str:
    """Map a TalentHub specialization to an active HR Hiring area, or '' when unmapped."""
    return checks.resolve_area(specialization, area_names) or ""


def enrich_intern(
    doc: dict[str, Any],
    area_names: list[str],
    today: date | None = None,
    notice_days: int | None = None,
) -> dict[str, Any]:
    """Attach derived area mapping and cohort timing flags to a normalized intern record."""
    today = today or date.today()
    today_iso = today.isoformat()
    days = config.VACANCY_NOTICE_DAYS if notice_days is None else notice_days
    soon_iso = (today + timedelta(days=days)).isoformat()
    start_date = as_local_date(doc.get("Training_StartDate"))
    end_date = as_local_date(doc.get("Training_EndDate"))
    area = match_area(doc.get("field_of_spec_name", ""), area_names)
    finished = bool(end_date) and end_date < today_iso
    ending_soon = bool(end_date) and today_iso <= end_date <= soon_iso
    upcoming = bool(start_date) and start_date > today_iso
    active = not finished and not upcoming
    return {
        **doc,
        "start_date": start_date,
        "end_date": end_date,
        "area": area,
        "finished": finished,
        "ending_soon": ending_soon,
        "upcoming": upcoming,
        "active": active,
    }


def interns_with_area(
    area_names: list[str],
    specialization: str = "",
    q: str = "",
    limit: int = 0,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Read interns directly from TalentHub API and annotate area & lifecycle state."""
    raw = list_interns(specialization=specialization, q=q, limit=limit)
    return [enrich_intern(r, area_names, today=today) for r in raw]


def interns_ending_by(
    cutoff: str | date,
    area_names: list[str],
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Return active/mapped interns whose Training_EndDate is on or before ``cutoff``."""
    cutoff_iso = cutoff.isoformat() if isinstance(cutoff, date) else str(cutoff)[:10]
    rows = interns_with_area(area_names, limit=0, today=today)
    matched = [r for r in rows if r["end_date"] and r["end_date"] <= cutoff_iso and r["area"]]
    matched.sort(key=lambda r: (r["end_date"], r["Trainee_ID"]))
    return matched


def analytics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute summary counts and breakdowns across enriched intern rows."""
    by_spec: dict[str, int] = {}
    by_inst: dict[str, int] = {}
    by_area: dict[str, int] = {}
    for r in rows:
        sp = r.get("field_of_spec_name") or "Unknown"
        by_spec[sp] = by_spec.get(sp, 0) + 1
        inst = r.get("Institute") or "Unspecified"
        by_inst[inst] = by_inst.get(inst, 0) + 1
        if r.get("area"):
            by_area[r["area"]] = by_area.get(r["area"], 0) + 1
    return {
        "total": len(rows),
        "active": sum(1 for r in rows if r.get("active")),
        "ending_soon": sum(1 for r in rows if r.get("ending_soon")),
        "finished": sum(1 for r in rows if r.get("finished")),
        "upcoming": sum(1 for r in rows if r.get("upcoming")),
        "unknown_area": sum(1 for r in rows if not r.get("area")),
        "by_specialization": [
            {"name": k, "count": v}
            for k, v in sorted(by_spec.items(), key=lambda x: (-x[1], x[0]))
        ],
        "by_institute": [
            {"name": k, "count": v}
            for k, v in sorted(by_inst.items(), key=lambda x: (-x[1], x[0]))
        ],
        "by_area": by_area,
    }


def find_matching_interns(email: str = "", name: str = "") -> list[dict[str, Any]]:
    """Find matching TalentHub interns by email or full name; returns [] cleanly if offline."""
    clean_email = (email or "").strip().lower()
    clean_name = re.sub(r"\s+", " ", (name or "").strip().lower())
    if not clean_email and not clean_name:
        return []
    try:
        rows = list_interns(limit=0)
    except (TalentHubUnavailable, TalentHubAuthError):
        return []
    matches = []
    for r in rows:
        r_email = (r.get("Trainee_Email") or "").strip().lower()
        r_name = re.sub(r"\s+", " ", (r.get("Trainee_Name") or "").strip().lower())
        if (clean_email and r_email and clean_email == r_email) or (clean_name and r_name and clean_name == r_name):
            matches.append(r)
    return matches


def is_sample_mongo_target() -> bool:
    """True only when configured against a local sample MongoDB or injected fake collection."""
    if _mongo_collection_override is not None:
        return True
    parsed = urlparse(config.TALENT_HUB_MONGO_URI)
    host = (parsed.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def enroll_sample_intern(record: dict[str, Any]) -> dict[str, Any]:
    """Upsert a selected candidate into the LOCAL sample MongoDB interns collection.

    Refuses writes if ``TALENT_HUB_MONGO_URI`` points outside localhost so read-only
    external TalentHub boundaries can never be violated accidentally.
    """
    if not is_sample_mongo_target():
        raise PermissionError("Enrolling interns is restricted to the local sample MongoDB database.")
    ensure_collection()
    res = upsert_interns([record])
    saved = mongo_get_intern(record["Trainee_ID"])
    return {"intern": saved, **res}


# ---- Deterministic sample generator & seeder ---------------------------------------
_NAME_COMMUNITIES = (
    (
        (
            "Kamal", "Nimali", "Kasun", "Dilani", "Tharindu", "Sanduni",
            "Nuwan", "Chathuri", "Ravindu", "Ishara", "Dinuka", "Hashini",
            "Kavinda", "Nethmi", "Sahan", "Hasini",
        ),
        (
            "Perera", "Silva", "Fernando", "Jayawardena", "Bandara", "Rajapaksa",
            "Wickramasinghe", "Senanayake", "Gunasekara", "Dissanayake",
            "Ranasinghe", "Karunaratne", "Ekanayake", "Herath",
        ),
    ),
    (
        (
            "Kajanathan", "Aarani", "Mathushan", "Nirosha", "Senthuran", "Ashwini",
            "Praveen", "Divya", "Luxshan", "Keerthana", "Harish", "Pavithra",
        ),
        (
            "Sivakumar", "Subramaniam", "Rajaratnam", "Selvarajah", "Yogeswaran",
            "Balasubramaniam", "Nadarajah", "Shanmuganathan", "Krishnamoorthy",
            "Jegatheesan", "Vigneswaran",
        ),
    ),
    (
        ("Mohamed", "Fathima", "Aashiq", "Zainab", "Rizwan", "Amina", "Shafraz", "Nadia"),
        ("Farhan", "Ahamed", "Nazeer", "Hameed", "Ibrahim", "Ismail", "Marikar", "Cader"),
    ),
    (
        ("Shenal", "Natasha", "Dilan", "Tanya", "Rehan", "Melissa"),
        ("De Mel", "Jansen", "Van Dort", "Ludowyke", "Fonseka", "Rodrigo"),
    ),
)
_SAMPLE_START_DATES = (
    "2025-01-01T00:00:00.000Z",
    "2025-02-15T00:00:00.000Z",
    "2025-07-01T00:00:00.000Z",
    "2026-01-05T00:00:00.000Z",
    "2026-05-19T00:00:00.000Z",
    "2026-07-01T00:00:00.000Z",
    "2026-09-01T00:00:00.000Z",
    "2026-11-02T00:00:00.000Z",
)
_COHORT_DAYS = (90, 180, 180, 270, 365)
# Weighted towards IT & engineering specializations (~90% IT, ~10% IT-adjacent), interleaved for variety.
_WEIGHTED_SPECS = (
    "Software Engineering",
    "Network Engineering",
    "AI/ML",
    "Web Development",
    "Backend Development",
    "Cybersecurity",
    "DevOps",
    "Data Engineering",
    "Software Engineering",
    "Flutter",
    "Data Analytics",
    "UI/UX Design",
    "Software Quality Assurance",
    "MLOps",
    "Web Development",
    "Network Engineering",
    "Backend Development",
    "Networking",
    "Business Analysis",
    "Software Engineering",
    "Project Management",
)


_INSTITUTES = (
    "University of Moratuwa",
    "UCSC (University of Colombo)",
    "SLIIT",
    "Informatics Institute of Technology (IIT)",
    "NSBM Green University",
    "NIBM",
    "University of Peradeniya",
    "University of Kelaniya",
    "Sabaragamuwa University",
    "Wayamba University",
)
# First 12 indices (away from 1042/1043) are anchored relative to today so Planner always sees live vacancies.
_UPCOMING_ENDING_SLOTS = (
    ("MLOps", 2),
    ("MLOps", 4),
    ("MLOps", 6),
    ("AI/ML", 1),
    ("AI/ML", 5),
    ("Flutter", 2),
    ("Flutter", 6),
    ("DevOps", 3),
    ("Web Development", 4),
    ("Data Analytics", 5),
    ("Cybersecurity", 3),
    ("Civil Engineering", 3),  # intentionally unmapped specialization for Planner/UI edge testing
)


def _intern_email(name: str, trainee_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")
    return f"{slug}.{trainee_id}@interns.slt.lk"


def _sri_lankan_name_pool() -> list[str]:
    reserved = {"Kamal Perera", "Nimali Silva"}
    groups: list[list[str]] = []
    for firsts, lasts in _NAME_COMMUNITIES:
        # Place last names outer and step by a prime stride > len(firsts) so adjacent rows vary both names.
        raw = [f"{fn} {ln}" for ln in lasts for fn in firsts if f"{fn} {ln}" not in reserved]
        n = len(raw)
        stride = next(s for s in (19, 23, 29, 31, 17, 13, 11, 7, 5, 1) if s < n and math.gcd(s, n) == 1)
        ordered = [raw[(idx * stride) % n] for idx in range(n)]
        groups.append(ordered)

    # Interleave Sinhala, Tamil, Muslim, and Burgher names in weighted 3:2:1:1 turns.
    pattern = (0, 0, 1, 0, 2, 1, 3)
    cursors = [0] * len(groups)
    pool: list[str] = []
    while any(cursors[g] < len(groups[g]) for g in range(len(groups))):
        for g in pattern:
            if cursors[g] < len(groups[g]):
                pool.append(groups[g][cursors[g]])
                cursors[g] += 1
    return pool


def generate_sample_interns(count: int = 400, reference_date: date | None = None) -> list[dict[str, Any]]:
    """Build deterministic Sri Lankan, IT-focused sample intern records with institutes & emails."""
    if count < 1:
        raise ValueError("count must be at least 1.")
    ref_day = reference_date or date.today()
    names_pool = _sri_lankan_name_pool()
    rows: list[dict[str, Any]] = []
    pool_idx = 0
    for i in range(count):
        trainee_id = str(1001 + i)
        if trainee_id == "1042":
            # Match Sithuli's API spec example records for 1042 and 1043.
            name = "Kamal Perera"
            start_dt = parse_utc_date("2025-01-01T00:00:00.000Z")
            end_dt = parse_utc_date("2025-06-30T00:00:00.000Z")
            spec = "Software Engineering"
            institute = "University of Moratuwa"
        elif trainee_id == "1043":
            name = "Nimali Silva"
            start_dt = parse_utc_date("2025-02-15T00:00:00.000Z")
            end_dt = parse_utc_date("2025-08-15T00:00:00.000Z")
            spec = "Network Engineering"
            institute = "SLIIT"
        else:
            if pool_idx < len(names_pool):
                name = names_pool[pool_idx]
            else:
                name = f"{names_pool[pool_idx % len(names_pool)]} {i + 1}"
            pool_idx += 1
            institute = _INSTITUTES[i % len(_INSTITUTES)]
            if i < len(_UPCOMING_ENDING_SLOTS):
                spec, offset_days = _UPCOMING_ENDING_SLOTS[i]
                end_day = ref_day + timedelta(days=offset_days)
                end_dt = datetime(end_day.year, end_day.month, end_day.day, tzinfo=timezone.utc)
                start_dt = end_dt - timedelta(days=180)
            else:
                base_start = parse_utc_date(_SAMPLE_START_DATES[i % len(_SAMPLE_START_DATES)])
                start_dt = base_start + timedelta(days=(i // len(_SAMPLE_START_DATES)))
                end_dt = start_dt + timedelta(days=_COHORT_DAYS[i % len(_COHORT_DAYS)])
                spec = _WEIGHTED_SPECS[i % len(_WEIGHTED_SPECS)]

        rows.append({
            "Trainee_ID": trainee_id,
            "Trainee_Name": name,
            "Training_StartDate": format_utc_date(start_dt),
            "Training_EndDate": format_utc_date(end_dt),
            "field_of_spec_name": spec,
            "Trainee_Email": _intern_email(name, trainee_id),
            "Institute": institute,
        })
    return rows


def seed_sample_interns(count: int = 400, reset: bool = False) -> dict[str, Any]:
    """Ensure schema/indexes exist in MongoDB and idempotently seed sample interns."""
    rows = generate_sample_interns(count)
    info = ensure_collection()
    if reset:
        _get_collection().delete_many({})
    res = upsert_interns(rows)
    return {**info, **res}
