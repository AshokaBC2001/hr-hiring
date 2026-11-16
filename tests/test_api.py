"""Tests of the web API (roles, safety and the main actions), with the fake model."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel  # noqa: E402
from hiring import config, db, flows, llm  # noqa: E402
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
    assert client.post(f"/api/drafts/{draft['id']}/release", headers=HR).json()["status"] == "awaiting_result"
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
    assert scores == sorted(scores, reverse=True) and min(scores) >= 50          # ranked, 50 or more only
    assert [r["rank"] for r in dev] == list(range(1, len(dev) + 1))
    assert "Ravi Kumar" not in [r["name"] for r in client.get("/api/pool?area=Web Development").json()["rows"]]
    assert client.get("/api/candidates").json() == []                          # not in the hiring flow yet
    app_no = dev[0]["app_no"]
    assert client.post(f"/api/candidates/{app_no}/archive", headers=HR).status_code == 200
    assert app_no in [r["app_no"] for r in client.get("/api/pool?show=archived").json()["rows"]]
    assert app_no not in [r["app_no"] for r in client.get("/api/pool?area=DevOps").json()["rows"]]


def test_interns_and_vacancies_pages(client):
    client.post("/api/samples", headers=HR)
    flows.run_employee("recruiter", limit=30, wait=True)
    assert client.post("/api/interns/sync", headers=IN).status_code == 400     # HR officer only
    out = client.post("/api/interns/sync", headers=HR).json()
    assert out["read"] > 0 and out["created"] > 0
    interns = client.get("/api/interns").json()
    assert interns["stats"]["total"] == out["read"] and interns["source"] == "example"
    soon = client.get("/api/interns?show=soon").json()["rows"]
    assert soon and all(r["ending_soon"] for r in soon)
    assert client.get("/api/interns?show=unknown").json()["rows"]               # kept, not guessed
    v = client.get("/api/vacancies").json()
    assert v["counts"]["open"] == out["created"]
    one = v["rows"][0]
    assert one["waiting"] == len([s for s in one["suggestions"] if s["state"] == "suggested"])
    assert client.get("/api/state").json()["tiles"]["vacancies"] >= one["waiting"]
    app_no = one["suggestions"][0]["app_no"]
    assert client.post(f"/api/vacancies/{one['id']}/approve", headers=IN, json={"app_no": app_no}).status_code == 400
    assert client.post(f"/api/vacancies/{one['id']}/approve", headers=HR, json={"app_no": app_no}).status_code == 200
    assert client.post(f"/api/vacancies/{one['id']}/approve", headers=HR, json={"app_no": app_no}).status_code == 400
    assert client.post("/api/vacancies", headers=HR, json={"area": "Sales", "places": 1, "reason": "left early"}).status_code == 200
    assert client.post("/api/vacancies", headers=HR, json={"area": "Nope", "places": 1}).status_code == 400


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
    assert r["decisions"] == []                      # pool CVs do not need a decision
    assert any(s["name"] == "Human Resources" for s in r["suggested_areas"])
    no_area = client.get("/api/pool?show=no_area").json()["rows"]
    assert any(x["name"] == "Laura Bennett" for x in no_area)
    team = client.get("/api/team").json()
    assert len(team["employees"]) == 5
