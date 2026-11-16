"""Unit and API tests for SLT TalentHub MongoDB repository and read-only HTTP client."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from bson import ObjectId

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_talent_hub import FakeInternCollection  # noqa: E402
from hiring import config, talent_hub  # noqa: E402


@pytest.fixture()
def fake_mongo(monkeypatch):
    coll = FakeInternCollection()
    talent_hub.set_mongo_collection(coll)
    yield coll
    talent_hub.set_mongo_collection(None)


def test_parse_and_format_utc_date_round_trip():
    dt = talent_hub.parse_utc_date("2025-01-01T00:00:00.000Z")
    assert dt == datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert talent_hub.format_utc_date(dt) == "2025-01-01T00:00:00.000Z"
    assert talent_hub.format_utc_date("2026-05-19T00:00:00Z") == "2026-05-19T00:00:00.000Z"
    assert talent_hub.format_utc_date(datetime(2026, 5, 19, 0, 0, 0)) == "2026-05-19T00:00:00.000Z"
    for bad in ("not-a-date", "", None):
        with pytest.raises(ValueError):
            talent_hub.parse_utc_date(bad)  # type: ignore[arg-type]


def test_normalize_intern_preserves_sithuli_fields_and_optional_metadata():
    oid = ObjectId("6735c911b619622d10c28ea1")
    raw = {
        "_id": oid,
        "Trainee_ID": "1042",
        "Trainee_Name": "Kamal Perera",
        "Training_StartDate": datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        "Training_EndDate": datetime(2025, 6, 30, 0, 0, tzinfo=timezone.utc),
        "field_of_spec_name": "Software Engineering",
        "Trainee_Email": "kamal.p@slt.lk",
        "Institute": "University of Moratuwa",
        "internal_extra": "ignored",
    }
    norm = talent_hub.normalize_intern(raw)
    assert norm == {
        "_id": "6735c911b619622d10c28ea1",
        "Trainee_ID": "1042",
        "Trainee_Name": "Kamal Perera",
        "Training_StartDate": "2025-01-01T00:00:00.000Z",
        "Training_EndDate": "2025-06-30T00:00:00.000Z",
        "field_of_spec_name": "Software Engineering",
        "Trainee_Email": "kamal.p@slt.lk",
        "Institute": "University of Moratuwa",
    }


def test_to_storage_doc_validates_required_fields_and_date_order():
    doc = talent_hub.to_storage_doc({
        "Trainee_ID": " 1042 ",
        "Trainee_Name": "Kamal Perera",
        "Training_StartDate": "2025-01-01T00:00:00.000Z",
        "Training_EndDate": "2025-06-30T00:00:00.000Z",
        "field_of_spec_name": "Software Engineering",
    })
    assert doc["Trainee_ID"] == "1042"
    assert doc["Training_StartDate"] == datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert doc["Training_EndDate"] == datetime(2025, 6, 30, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        talent_hub.to_storage_doc({
            "Trainee_ID": "1042",
            "Trainee_Name": "Kamal Perera",
            "Training_StartDate": "2025-06-30T00:00:00.000Z",
            "Training_EndDate": "2025-06-30T00:00:00.000Z",
            "field_of_spec_name": "Software Engineering",
        })
    with pytest.raises(ValueError):
        talent_hub.to_storage_doc({
            "Trainee_ID": "",
            "Trainee_Name": "Kamal Perera",
            "Training_StartDate": "2025-01-01T00:00:00.000Z",
            "Training_EndDate": "2025-06-30T00:00:00.000Z",
            "field_of_spec_name": "Software Engineering",
        })


def test_mongo_repository_upsert_filter_and_get_by_id_or_object_id(fake_mongo):
    oid = ObjectId("6735c911b619622d10c28ea1")
    res1 = talent_hub.upsert_interns([
        {
            "_id": oid,
            "Trainee_ID": "1042",
            "Trainee_Name": "Kamal Perera",
            "Training_StartDate": "2025-01-01T00:00:00.000Z",
            "Training_EndDate": "2025-06-30T00:00:00.000Z",
            "field_of_spec_name": "Software Engineering",
        },
        {
            "Trainee_ID": "1043",
            "Trainee_Name": "Nimali Silva",
            "Training_StartDate": "2025-02-15T00:00:00.000Z",
            "Training_EndDate": "2025-08-15T00:00:00.000Z",
            "field_of_spec_name": "Network Engineering",
        },
    ])
    assert res1["upserted"] == 2 and res1["total"] == 2

    res2 = talent_hub.upsert_interns([
        {
            "Trainee_ID": "1043",
            "Trainee_Name": "Nimali S. Silva",
            "Training_StartDate": "2025-02-15T00:00:00.000Z",
            "Training_EndDate": "2025-08-15T00:00:00.000Z",
            "field_of_spec_name": "Network Engineering",
        },
    ])
    assert res2["modified"] == 1 and res2["total"] == 2

    all_rows = talent_hub.mongo_list_interns()
    assert [r["Trainee_ID"] for r in all_rows] == ["1042", "1043"]
    assert len(talent_hub.mongo_list_interns(specialization="software engineering")) == 1
    assert [r["Trainee_ID"] for r in talent_hub.mongo_list_interns(q="Nimali")] == ["1043"]

    by_tid = talent_hub.mongo_get_intern("1042")
    by_oid = talent_hub.mongo_get_intern("6735c911b619622d10c28ea1")
    assert by_tid is not None and by_tid["Trainee_Name"] == "Kamal Perera"
    assert by_oid is not None and by_oid["Trainee_ID"] == "1042"
    assert talent_hub.mongo_get_intern("not-an-id") is None


def test_http_client_reads_with_bearer_auth_and_handles_errors(monkeypatch):
    seen_headers = []
    sample_list = [
        {
            "_id": "6735c911b619622d10c28ea1",
            "Trainee_ID": "1042",
            "Trainee_Name": "Kamal Perera",
            "Training_StartDate": "2025-01-01T00:00:00.000Z",
            "Training_EndDate": "2025-06-30T00:00:00.000Z",
            "field_of_spec_name": "Software Engineering",
        },
        {
            "_id": "6735c911b619622d10c28ea2",
            "Trainee_ID": "1043",
            "Trainee_Name": "Nimali Silva",
            "Training_StartDate": "2025-02-15T00:00:00.000Z",
            "Training_EndDate": "2025-08-15T00:00:00.000Z",
            "field_of_spec_name": "Network Engineering",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != "Bearer secret-tok":
            return httpx.Response(401, json={"detail": "Unauthorized"})
        if request.url.path == "/api/interns":
            return httpx.Response(200, json=sample_list)
        if request.url.path == "/api/interns/1042":
            return httpx.Response(200, json=sample_list[0])
        if request.url.path == "/api/interns/9999":
            return httpx.Response(404, json={"detail": "Not found"})
        if request.url.path == "/api/interns/boom":
            return httpx.Response(500, json={"detail": "Server error"})
        return httpx.Response(404)

    monkeypatch.setattr(config, "TALENT_HUB_API_URL", "https://talenthub.test/api")
    monkeypatch.setattr(config, "TALENT_HUB_API_TOKEN", "secret-tok")
    talent_hub.set_http_transport(httpx.MockTransport(handler))
    try:
        rows = talent_hub.list_interns(specialization="Software Engineering")
        assert len(rows) == 1 and rows[0]["Trainee_ID"] == "1042"
        assert seen_headers[-1] == "Bearer secret-tok"

        one = talent_hub.get_intern("1042")
        assert one is not None and one["Trainee_Name"] == "Kamal Perera"
        assert talent_hub.get_intern("9999") is None

        with pytest.raises(talent_hub.TalentHubUnavailable):
            talent_hub.get_intern("boom")

        monkeypatch.setattr(config, "TALENT_HUB_API_TOKEN", "wrong-tok")
        with pytest.raises(talent_hub.TalentHubAuthError):
            talent_hub.list_interns()
    finally:
        talent_hub.set_http_transport(None)


def test_health_fails_gracefully_when_api_unreachable(monkeypatch):
    monkeypatch.setattr(config, "TALENT_HUB_API_URL", "http://127.0.0.1:59999/api")
    monkeypatch.setattr(config, "TALENT_HUB_TIMEOUT_MS", 200)
    talent_hub.close()
    h = talent_hub.health(force=True)
    assert h["ok"] is False
    assert h["detail"]
