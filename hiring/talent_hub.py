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

import re
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from bson import ObjectId
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.errors import OperationFailure, PyMongoError

from . import config

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
