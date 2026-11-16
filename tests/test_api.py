"""Tests of the web API (roles, safety and the main actions), with the fake model."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel  # noqa: E402
from fake_talent_hub import FakeInternCollection  # noqa: E402
from hiring import config, db, flows, llm, mailer, talent_hub  # noqa: E402
from hiring.server import app  # noqa: E402

HR = {"x-role": "hr"}
IN = {"x-role": "interviewer"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    db.setup(tmp_path / "data")
    llm.set_backend(FakeModel())
    with TestClient(app) as c:
        yield c
    llm.set_backend(None)


def test_pages_and_state(client):
    assert client.get("/").status_code == 200
    assert "app.js" in client.get("/").text
    s = client.get("/api/state").json()
    assert s["model"]["ok"] and s["total"] == 0


def test_upload_and_flow(client):
    p = config.SAMPLES_DIR / "arjun_mehta.txt"
    r = client.post("/api/cvs", headers=HR, files=[("files", (p.name, p.read_bytes(), "text/plain"))],
                    data={"email": "k@example.com"}).json()
    assert r["results"][0]["ok"]
    app_no = r["results"][0]["app_no"]
    flows.run_all_once()
    d = client.get(f"/api/candidates/{app_no}").json()
    assert d["status"] == "in_pool" and d["email"] == "k@example.com"
    assert len(d["area_scores"]) == len(d["active_areas"])
    r = client.post(f"/api/candidates/{app_no}/invite", headers=HR, json={"area": d["area"]})
    assert r.status_code == 200 and r.json()["status"] == "shortlisted"
    flows.run_all_once()
    d = client.get(f"/api/candidates/{app_no}").json()
    assert d["status"] == "invite_ready"
    draft = d["drafts"][0]
    assert draft["can_release"]
    rel = client.post(f"/api/drafts/{draft['id']}/release", headers=HR).json()
    assert rel["status"] == "awaiting_result" and rel["delivery_status"] == "outbox_only"
    assert client.get("/api/interviews").json()["waiting"][0]["app_no"] == app_no
    assert client.post(f"/api/candidates/{app_no}/result", headers=IN, json={"result": "Pass"}).status_code == 200


def test_roles_are_enforced(client):
    client.post("/api/samples", headers=HR)
    flows.run_all_once()
    top = client.get("/api/pool?area=AI/ML").json()["rows"][0]["app_no"]
    assert client.post(f"/api/candidates/{top}/invite", headers=IN, json={"area": "AI/ML"}).status_code == 400
    client.post(f"/api/candidates/{top}/invite", headers=HR, json={"area": "AI/ML"})
    flows.run_all_once()
    rows = client.get("/api/candidates?status=release").json()
    app_no = rows[0]["app_no"]
    d = client.get(f"/api/candidates/{app_no}").json()["drafts"][0]
    r = client.post(f"/api/drafts/{d['id']}/release", headers=IN)
    assert r.status_code == 400 and "HR officer" in r.json()["detail"]
    client.post(f"/api/drafts/{d['id']}/release", headers=HR)
    r = client.post(f"/api/candidates/{app_no}/result", headers=HR, json={"result": "Pass"})
    assert r.status_code == 400 and "Interviewer" in r.json()["detail"]


def test_pool_ranking_and_lists(client):
    client.post("/api/samples", headers=HR)
    flows.run_employee("recruiter", limit=30, wait=True)
    p = client.get("/api/pool").json()
    assert p["stats"]["being_read"] == 0 and p["stats"]["no_area"] == 3
    assert p["stats"]["in_pool"] == len([r for r in p["rows"] if r["status"] in ("in_pool", "no_area")])
    assert all(len(r["other_areas"]) <= 3 for r in p["rows"])
    dev = client.get("/api/pool?area=DevOps").json()["rows"]
    scores = [r["area_score"] for r in dev]
    assert scores == sorted(scores, reverse=True) and min(scores) >= 50
    assert [r["rank"] for r in dev] == list(range(1, len(dev) + 1))
    assert "Ravi Kumar" not in [r["name"] for r in client.get("/api/pool?area=Web Development").json()["rows"]]
    assert client.get("/api/candidates").json() == []
    app_no = dev[0]["app_no"]
    assert client.post(f"/api/candidates/{app_no}/archive", headers=HR).status_code == 200
    assert app_no in [r["app_no"] for r in client.get("/api/pool?show=archived").json()["rows"]]
    assert app_no not in [r["app_no"] for r in client.get("/api/pool?area=DevOps").json()["rows"]]


def test_interns_and_vacancies_pages_backed_by_talenthub_mongodb(client):
    coll = FakeInternCollection()
    talent_hub.set_mongo_collection(coll)
    talent_hub.seed_sample_interns(400)

    class _DirectMongoBackend:
        def list_interns(self):
            return talent_hub.mongo_list_interns(limit=0)

        def get_intern(self, identifier: str):
            return talent_hub.mongo_get_intern(identifier)

    talent_hub.set_backend(_DirectMongoBackend())
    try:
        client.post("/api/samples", headers=HR)
        flows.run_employee("recruiter", limit=30, wait=True)
        assert client.post("/api/vacancies/plan", headers=IN).status_code == 400
        out = client.post("/api/vacancies/plan", headers=HR).json()
        assert out["read"] == 400 and out["created"] > 0

        # No duplicate 'interns' table was added to SQLite
        with db.Session() as s:
            tables = {r[0] for r in s.connection().exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        assert "interns" not in tables

        interns = client.get("/api/interns").json()
        assert interns["total"] == 400 and interns["stats"]["ending_soon"] >= 10
        assert client.get("/api/interns?show=unknown").json()["total"] >= 1

        v = client.get("/api/vacancies").json()
        assert v["counts"]["open"] == out["created"]
        one = next(row for row in v["rows"] if row["suggestions"])
        app_no = one["suggestions"][0]["app_no"]
        assert client.post(f"/api/vacancies/{one['id']}/approve", headers=IN, json={"app_no": app_no}).status_code == 400
        assert client.post(f"/api/vacancies/{one['id']}/approve", headers=HR, json={"app_no": app_no}).status_code == 200
        assert client.post(f"/api/vacancies/{one['id']}/approve", headers=HR, json={"app_no": app_no}).status_code == 400
        assert client.post("/api/vacancies", headers=HR, json={"area": "Sales", "places": 1, "reason": "early exit"}).status_code == 200
    finally:
        talent_hub.set_backend(None)
        talent_hub.set_mongo_collection(None)


def test_email_sender_checklist_notes_returning_intern_and_exports(client):
    sent = []

    def fake_smtp(msg, cfg):
        sent.append((msg["To"], msg["Subject"], msg["Message-ID"]))
        return msg["Message-ID"]

    mailer.set_backend(fake_smtp)
    coll = FakeInternCollection()
    talent_hub.set_mongo_collection(coll)
    talent_hub.seed_sample_interns(50)

    class _DirectMongoBackend:
        def list_interns(self):
            return talent_hub.mongo_list_interns(limit=0)

        def get_intern(self, identifier: str):
            return talent_hub.mongo_get_intern(identifier)

    talent_hub.set_backend(_DirectMongoBackend())
    try:
        # Configure live SMTP with Demo Override via API + send test email
        assert client.post("/api/email/settings", headers=HR, json={
            "enabled": True,
            "host": "smtp.gmail.com",
            "port": 587,
            "user": "hr@slt.lk",
            "password": "secret",
            "test_recipient": "demo@slt.lk",
        }).status_code == 200
        assert client.post("/api/email/test", headers=HR, json={"to": "demo@slt.lk"}).json()["ok"] is True

        # Upload CV -> Invite -> Interview Pass -> Onboarding -> Release Acceptance Email via SMTP
        p = config.SAMPLES_DIR / "arjun_mehta.txt"
        r = client.post("/api/cvs", headers=HR, files=[("files", (p.name, p.read_bytes(), "text/plain"))]).json()
        app_no = r["results"][0]["app_no"]
        flows.run_all_once()
        client.post(f"/api/candidates/{app_no}/invite", headers=HR, json={"area": "AI/ML"})
        flows.run_all_once()
        d1 = client.get(f"/api/candidates/{app_no}").json()["drafts"][0]
        rel1 = client.post(f"/api/drafts/{d1['id']}/release", headers=HR).json()
        assert rel1["delivery_status"] == "sent" and rel1["sent_to"] == "demo@slt.lk"

        assert client.post(f"/api/candidates/{app_no}/result", headers=IN, json={"result": "Pass"}).status_code == 200
        flows.run_all_once()
        cand_detail = client.get(f"/api/candidates/{app_no}").json()
        assert cand_detail["work_id"] == 1001 and len(cand_detail["checklist"]) >= 6

        # Toggle interactive checklist + add HR note
        chk_res = client.post(f"/api/candidates/{app_no}/checklist/0", headers=HR, json={"done": True}).json()
        assert chk_res["checklist"][0]["done"] is True
        notes_res = client.post(f"/api/candidates/{app_no}/notes", headers=HR, json={"text": "Top ML candidate"}).json()
        assert notes_res["hr_notes"][0]["text"] == "Top ML candidate"

        # Enroll into local sample MongoDB interns & verify Returning-Intern detection picks them up!
        enroll_res = client.post(f"/api/candidates/{app_no}/enroll-intern", headers=HR, json={"months": 6})
        assert enroll_res.status_code == 200 and enroll_res.json()["intern"]["Trainee_ID"] == "1001"
        updated_detail = client.get(f"/api/candidates/{app_no}").json()
        assert any(m["Trainee_ID"] == "1001" for m in updated_detail["returning_interns"])

        # Release acceptance draft & test manual resend
        offer = [d for d in updated_detail["drafts"] if d["kind"] == "acceptance"][0]
        rel2 = client.post(f"/api/drafts/{offer['id']}/release", headers=HR).json()
        assert rel2["delivery_status"] == "sent"
        assert client.post(f"/api/drafts/{offer['id']}/resend", headers=HR).json()["ok"] is True

        # CSV & JSON database exports
        csv_resp = client.get("/api/export/candidates.csv")
        assert csv_resp.status_code == 200 and "Arjun Mehta" in csv_resp.text
        json_resp = client.get("/api/export/database.json")
        assert json_resp.status_code == 200 and json_resp.json()["candidates"][0]["work_id"] == 1001
    finally:
        mailer.set_backend(None)
        talent_hub.set_backend(None)
        talent_hub.set_mongo_collection(None)


def test_other_websites_and_hosts_are_refused(client):
    assert client.post("/api/samples", headers={**HR, "origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 400


def test_bad_input_gives_a_clear_message(client):
    r = client.post("/api/cvs", headers=HR, files=[("files", ("x.exe", b"abc", "application/octet-stream"))]).json()
    assert not r["results"][0]["ok"]
    assert client.get("/api/candidates/APP-9999").status_code == 400
    assert client.get("/api/candidates/..%2F..%2Fetc/cv").status_code in (400, 404)
    r = client.post("/api/areas", headers=HR, json={"name": "Empty area", "required_skills": ""})
    assert r.status_code == 400 and "required skill" in r.json()["detail"]


def test_review_lists_decisions_and_suggested_areas(client):
    for a in client.get("/api/areas").json()["areas"]:   # keep only the IT areas, as the HR officer could
        if a["name"] not in {"AI/ML", "DevOps", "MLOps", "Flutter"}:
            assert client.put(f"/api/areas/{a['id']}", headers=HR, json={**a, "active": False}).status_code == 200
    client.post("/api/samples", headers=HR)
    flows.run_employee("recruiter", limit=30, wait=True)
    r = client.get("/api/review").json()
    assert r["decisions"] == []
    assert any(s["name"] == "Human Resources" for s in r["suggested_areas"])
    no_area = client.get("/api/pool?show=no_area").json()["rows"]
    assert any(x["name"] == "Laura Bennett" for x in no_area)
    team = client.get("/api/team").json()
    assert len(team["employees"]) == 5
