"""Unit and API tests for SLT TalentHub MongoDB repository and read-only HTTP client."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel  # noqa: E402
from fake_talent_hub import FakeInternCollection  # noqa: E402
from hiring import config, db, llm, talent_hub  # noqa: E402


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


def test_generate_sample_interns_builds_100_valid_sithuli_records():
    rows = talent_hub.generate_sample_interns(100)
    assert len(rows) == 100
    assert rows[0]["Trainee_ID"] == "1001"
    assert rows[-1]["Trainee_ID"] == "1100"
    assert len({r["Trainee_ID"] for r in rows}) == 100
    assert len({r["Trainee_Name"] for r in rows}) == 100

    expected_keys = {
        "Trainee_ID",
        "Trainee_Name",
        "Training_StartDate",
        "Training_EndDate",
        "field_of_spec_name",
    }
    for row in rows:
        assert set(row.keys()) == expected_keys
        assert row["Trainee_Name"].strip()
        assert row["field_of_spec_name"].strip()
        assert row["Training_StartDate"].endswith("T00:00:00.000Z")
        assert row["Training_EndDate"].endswith("T00:00:00.000Z")
        assert talent_hub.parse_utc_date(row["Training_EndDate"]) > talent_hub.parse_utc_date(row["Training_StartDate"])


def test_seed_sample_interns_is_idempotent(fake_mongo):
    first = talent_hub.seed_sample_interns(100)
    second = talent_hub.seed_sample_interns(100)
    assert first["upserted"] == 100 and first["total"] == 100
    assert second["upserted"] == 0 and second["modified"] == 0 and second["total"] == 100
    assert {idx["name"] for idx in fake_mongo.indexes} >= {"Trainee_ID_1", "field_of_spec_name_1", "Training_EndDate_1"}

    reset_res = talent_hub.seed_sample_interns(100, reset=True)
    assert reset_res["total"] == 100
    assert talent_hub.mongo_get_intern("1042") is not None


def test_api_mock_talenthub_auth_readonly_and_lookup(fake_mongo, monkeypatch):
    from hiring import talent_hub_mock

    monkeypatch.setattr(config, "TALENT_HUB_API_TOKEN", "test-token")
    talent_hub.seed_sample_interns(100)
    mock_client = TestClient(talent_hub_mock.app)

    assert mock_client.get("/api/interns").status_code == 401
    assert mock_client.get("/api/interns", headers={"Authorization": "Bearer wrong"}).status_code == 401

    auth = {"Authorization": "Bearer test-token"}
    r_all = mock_client.get("/api/interns", headers=auth)
    assert r_all.status_code == 200
    rows = r_all.json()
    assert len(rows) == 100
    assert rows[0]["Training_StartDate"].endswith("T00:00:00.000Z")

    r_one = mock_client.get("/api/interns/1042", headers=auth)
    assert r_one.status_code == 200
    kamal = r_one.json()
    assert kamal["Trainee_ID"] == "1042" and kamal["Trainee_Name"] == "Kamal Perera"
    assert kamal["Training_StartDate"] == "2025-01-01T00:00:00.000Z"
    assert kamal["field_of_spec_name"] == "Software Engineering"

    r_oid = mock_client.get(f"/api/interns/{kamal['_id']}", headers=auth)
    assert r_oid.status_code == 200 and r_oid.json()["Trainee_ID"] == "1042"
    assert mock_client.get("/api/interns/9999", headers=auth).status_code == 404

    assert mock_client.post("/api/interns", headers=auth, json={}).status_code == 405
    assert mock_client.put("/api/interns/1042", headers=auth, json={}).status_code == 405
    assert mock_client.delete("/api/interns/1042", headers=auth).status_code == 405


def test_api_hr_hiring_interns_and_state_integration(fake_mongo, tmp_path, monkeypatch):
    from hiring import talent_hub_mock
    from hiring.server import app

    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "TALENT_HUB_API_URL", "http://talenthub.local/api")
    monkeypatch.setattr(config, "TALENT_HUB_API_TOKEN", "test-token")
    db.setup(tmp_path / "data")
    llm.set_backend(FakeModel())
    talent_hub.seed_sample_interns(100)

    mock_client = TestClient(talent_hub_mock.app)

    def bridge(request: httpx.Request) -> httpx.Response:
        # Simulate a separate TalentHub server process expecting "test-token".
        saved = config.TALENT_HUB_API_TOKEN
        config.TALENT_HUB_API_TOKEN = "test-token"
        try:
            resp = mock_client.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                headers=dict(request.headers),
            )
        finally:
            config.TALENT_HUB_API_TOKEN = saved
        return httpx.Response(resp.status_code, content=resp.content, headers=dict(resp.headers))

    talent_hub.set_http_transport(httpx.MockTransport(bridge))
    try:
        with TestClient(app) as client:
            state = client.get("/api/state").json()
            assert state["talent_hub"]["ok"] is True and state["talent_hub"]["count"] == 100

            r_list = client.get("/api/interns?specialization=Software%20Engineering")
            assert r_list.status_code == 200
            data = r_list.json()
            assert data["total"] == len(data["interns"]) and data["total"] > 0
            assert all(x["field_of_spec_name"] == "Software Engineering" for x in data["interns"])

            r_q = client.get("/api/interns?q=Kamal&limit=5")
            assert r_q.status_code == 200
            assert any(x["Trainee_ID"] == "1042" for x in r_q.json()["interns"])

            r_one = client.get("/api/interns/1042")
            assert r_one.status_code == 200 and r_one.json()["Trainee_Name"] == "Kamal Perera"
            assert client.get("/api/interns/9999").status_code == 404

            # Bad bearer token surfaces as 502 without crashing /api/state.
            monkeypatch.setattr(config, "TALENT_HUB_API_TOKEN", "bad-token")
            assert client.get("/api/interns").status_code == 502
            assert client.get("/api/state").status_code == 200
    finally:
        talent_hub.set_http_transport(None)
        llm.set_backend(None)


def test_api_hr_hiring_interns_offline_returns_503(tmp_path, monkeypatch):
    from hiring.server import app

    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "TALENT_HUB_API_URL", "http://127.0.0.1:59999/api")
    monkeypatch.setattr(config, "TALENT_HUB_TIMEOUT_MS", 200)
    db.setup(tmp_path / "data")
    llm.set_backend(FakeModel())
    talent_hub.close()
    try:
        with TestClient(app) as client:
            st = client.get("/api/state")
            assert st.status_code == 200
            assert st.json()["talent_hub"]["ok"] is False

            res = client.get("/api/interns")
            assert res.status_code == 503 and "detail" in res.json()
            assert client.get("/api/interns/1042").status_code == 503
    finally:
        llm.set_backend(None)


def test_live_mongodb_schema_and_seeding_when_mongod_running(monkeypatch):
    talent_hub.close()
    if not talent_hub.mongo_health()["ok"]:
        pytest.skip("Local mongod is not running")

    from pymongo import MongoClient
    from pymongo.errors import WriteError

    temp_db = "slt_talent_hub_pytest"
    monkeypatch.setattr(config, "TALENT_HUB_DB_NAME", temp_db)
    talent_hub.close()
    client = MongoClient(config.TALENT_HUB_MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        client.drop_database(temp_db)
        res = talent_hub.seed_sample_interns(100)
        assert res["total"] == 100
        kamal = talent_hub.mongo_get_intern("1042")
        assert kamal is not None and kamal["Trainee_Name"] == "Kamal Perera"
        assert kamal["Training_StartDate"] == "2025-01-01T00:00:00.000Z"

        with pytest.raises(WriteError):
            client[temp_db][config.TALENT_HUB_COLLECTION].insert_one({"Trainee_ID": "BAD"})
    finally:
        talent_hub.close()
        client.drop_database(temp_db)
        client.close()


def test_cli_run_interns_and_seed_handle_unavailable_cleanly(monkeypatch, capsys):
    import run as cli_run

    monkeypatch.setattr(config, "TALENT_HUB_API_URL", "http://127.0.0.1:59999/api")
    monkeypatch.setattr(config, "TALENT_HUB_MONGO_URI", "mongodb://127.0.0.1:27999")
    monkeypatch.setattr(config, "TALENT_HUB_TIMEOUT_MS", 200)
    talent_hub.close()

    monkeypatch.setattr(sys, "argv", ["run.py", "interns"])
    rc_interns = cli_run.main()
    out_interns = capsys.readouterr().out
    assert rc_interns == 1
    assert "Error:" in out_interns and "start_talent_hub.sh" in out_interns

    monkeypatch.setattr(sys, "argv", ["run.py", "seed-talent-hub"])
    rc_seed = cli_run.main()
    out_seed = capsys.readouterr().out
    assert rc_seed == 1
    assert "Error:" in out_seed and "TALENT_HUB_MONGO_URI" in out_seed


def test_web_static_exposes_interns_page(tmp_path, monkeypatch):
    from hiring.server import app

    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    db.setup(tmp_path / "data")
    llm.set_backend(FakeModel())
    try:
        with TestClient(app) as client:
            js = client.get("/app.js")
            css = client.get("/style.css")
            assert js.status_code == 200
            assert css.status_code == 200
            assert '"/api/interns' in js.text and "pInterns" in js.text
            assert ".intern-stats" in css.text
    finally:
        llm.set_backend(None)
