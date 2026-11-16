"""Comprehensive tests for logic fixes, efficiency, AI upgrades, MongoDB direct mode,
hosting security/proxy guards, supervisor feedback, diagnostics, and CLI helpers."""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel  # noqa: E402
from fake_talent_hub import FakeInternCollection  # noqa: E402
from hiring import actions, checks, config, db, employees, flows, llm, mailer, supervisor, talent_hub, tools, vacancies  # noqa: E402
from hiring.server import app  # noqa: E402

HR = {"x-role": "hr"}
IN = {"x-role": "interviewer"}


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    db.setup(tmp_path / "data")
    model = FakeModel()
    llm.set_backend(model)
    flows.NOTICE.update(text="", at="")
    yield model, tmp_path
    llm.set_backend(None)
    mailer.set_backend(None)
    talent_hub.set_backend(None)
    talent_hub.set_mongo_collection(None)
    talent_hub.close()
    llm.close()


def _utc(day: date) -> str:
    return day.isoformat() + "T00:00:00.000Z"


class _FakeTH:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def list_interns(self):
        return self.docs

    def get_intern(self, identifier: str):
        return next((d for d in self.docs if d["Trainee_ID"] == identifier), None)


# ---- 1. Logic bug fixes ------------------------------------------------------------
def test_extended_end_date_auto_closed_vacancy_reopens_when_new_date_is_due(isolated):
    today = date.today()
    soon = today + timedelta(days=2)
    extended = today + timedelta(days=45)
    docs = [
        {
            "Trainee_ID": "T-REOPEN",
            "Trainee_Name": "Dilani Perera",
            "Training_StartDate": _utc(today - timedelta(days=120)),
            "Training_EndDate": _utc(soon),
            "field_of_spec_name": "Flutter",
        }
    ]
    talent_hub.set_backend(_FakeTH(docs))

    out1 = vacancies.find_and_update(today=today)
    assert out1["created"] == 1 and len(db.vacancies_in("open")) == 1

    # Extend end date far into future -> auto-closes
    docs[0]["Training_EndDate"] = _utc(extended)
    out2 = vacancies.find_and_update(today=today)
    assert out2["closed"] == 1 and db.vacancies_in("open") == []

    # When today advances into extended end date notice period, vacancy MUST reopen!
    out3 = vacancies.find_and_update(today=extended - timedelta(days=3))
    assert out3["created"] == 1 and len(db.vacancies_in("open")) == 1

    # By contrast, manual HR close must NOT reopen even if intern still finishes in window
    v = db.vacancies_in("open")[0]
    actions.close_vacancy(v.id)
    out4 = vacancies.find_and_update(today=extended - timedelta(days=2))
    assert out4["created"] == 0 and db.vacancies_in("open") == []


def test_vacancy_partial_places_shrink_marks_filled_and_validates_opens_on(isolated):
    assert db.setup is not None
    with pytest.raises(actions.ActionError, match="YYYY-MM-DD"):
        actions.add_vacancy("AI/ML", 1, "test", "not-a-date")

    today = date.today()
    soon = today + timedelta(days=2)
    docs = [
        {"Trainee_ID": "T1", "Trainee_Name": "One", "Training_StartDate": _utc(soon - timedelta(days=90)),
         "Training_EndDate": _utc(soon), "field_of_spec_name": "AI/ML"},
        {"Trainee_ID": "T2", "Trainee_Name": "Two", "Training_StartDate": _utc(soon - timedelta(days=90)),
         "Training_EndDate": _utc(soon), "field_of_spec_name": "AI/ML"},
    ]
    talent_hub.set_backend(_FakeTH(docs))
    vacancies.find_and_update(today=today)
    v = db.vacancies_in("open")[0]
    db.update_vacancy(v.id, filled=1)

    # T2 gets extended away, leaving 1 trainee and 1 already filled -> status should become "filled"
    docs.pop()
    vacancies.find_and_update(today=today)
    v_after = db.get_vacancy(v.id)
    assert v_after is not None and v_after.places == 1 and v_after.status == "filled"


def test_checks_and_tools_phone_ligatures_aliases_synonyms_and_duplicate_email(isolated):
    sample_cv = (
        "Kasun Silva\n"
        "kasun.silva@example.com | +94 (77) 123-4567\n\n"
        "Education\nBSc Software Engineering, University of Moratuwa\n\n"
        "Skills\nExcel, PowerPoint, CICD, RESTful API, Git, Linux, Docker\n\n"
        "Projects\n- ﬂow pipeline optimization using CI/CD and ﬁnance Excel reporting\n"
    )
    # '+' single non-digit phone must be rejected cleanly
    _, probs, _ = checks.check_profile({"name": "Kasun Silva", "phone": "+"}, sample_cv)
    assert any("phone" in p.lower() for p in probs)

    # Parenthesized Sri Lankan phone extracted cleanly
    prof = tools.rule_profile(sample_cv, tools.skill_vocabulary(db.active_areas()))
    assert "(77)" in prof["phone"] and "kasun.silva@example.com" == prof["email"]

    # Software Engineering alias + QA + Cybersecurity
    active_names = [a.name for a in db.active_areas()]
    assert checks.resolve_area("Software Engineering", active_names) == "Backend Development"
    assert checks.resolve_area("QA", active_names) == "Software Quality Assurance"

    # Synonyms allow "Excel" to match "Microsoft Excel" and "CICD" to match "CI/CD"
    devops = db.area_by_name("DevOps")
    _, parts_dev, gaps_dev = tools.score_cv(prof, sample_cv, devops)
    assert "CI/CD" not in gaps_dev
    pm = db.area_by_name("Project Management")
    _, _, gaps_pm = tools.score_cv(prof, sample_cv, pm)
    assert "Microsoft Excel" not in gaps_pm

    # Duplicate email flagged even when set manually later via set_email
    r1 = actions.add_cv("k1.txt", b"Kasun One\nSkills: Python\n", "dup@slt.lk")
    r2 = actions.add_cv("k2.txt", b"Kasun Two\nSkills: Java\n")
    actions.set_email(r2["app_no"], "dup@slt.lk")
    c2 = db.get_candidate(r2["app_no"])
    assert any("Possible duplicate" in f["text"] and r1["app_no"] in f["text"] for f in c2.flags)


# ---- 2. Efficiency, SQLite WAL & Concurrency -------------------------------------
def test_sqlite_wal_indexes_concurrency_and_scoring_speed(isolated):
    with db.Session() as s:
        conn = s.connection()
        journal = str(conn.exec_driver_sql("PRAGMA journal_mode").scalar()).lower()
        fk = int(conn.exec_driver_sql("PRAGMA foreign_keys").scalar())
        idx_names = {row[1] for row in conn.exec_driver_sql(
            "SELECT type, name FROM sqlite_master WHERE type='index'"
        )}
    assert journal == "wal" and fk == 1
    assert {"ix_candidates_email", "ix_drafts_status", "ix_events_at", "ix_runs_started_at"} <= idx_names

    # Thread-safe next_app_no + optimistic status lock under concurrent calls
    with ThreadPoolExecutor(max_workers=6) as ex:
        app_nos = list(ex.map(lambda _: db.next_app_no(), range(12)))
    assert len(set(app_nos)) == 12

    r = actions.add_cv("concur.txt", b"Arjun Mehta\nSkills: Python Docker\n" * 4)
    c = db.get_candidate(r["app_no"])
    with ThreadPoolExecutor(max_workers=6) as ex:
        wins = list(ex.map(lambda _: db.update_candidate(c.id, expect_status="new", status="shortlisted"), range(6)))
    assert wins.count(True) == 1

    # Scoring all 28 active areas on a full CV stays fast (< 250ms)
    cv_text = (config.SAMPLES_DIR / "arjun_mehta.txt").read_text(encoding="utf-8")
    areas = db.active_areas()
    prof = tools.rule_profile(cv_text, tools.skill_vocabulary(areas))
    t0 = time.perf_counter()
    scores = tools.score_all_areas(prof, cv_text, areas)
    elapsed = time.perf_counter() - t0
    assert len(scores) == len(areas) and elapsed < 0.25


# ---- 3. AI Agent Upgrades & OpenAI-compat LLM Client -----------------------------
def test_llm_openai_compat_health_bearer_key_and_rules_only_fallback(isolated, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path == "/health":
            return httpx.Response(404)
        if request.url.path == "/v1/models":
            if request.headers.get("authorization") != "Bearer sk-test-123":
                return httpx.Response(401)
            return httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
        return httpx.Response(404)

    llm.set_backend(None)
    monkeypatch.setattr(config, "MODEL_URL", "https://llm.example.test")
    monkeypatch.setattr(config, "MODEL_NAME", "")
    monkeypatch.setattr(config, "MODEL_API_KEY", "sk-test-123")
    llm._http_client = httpx.Client(transport=httpx.MockTransport(handler))
    llm._client_key = float(config.MODEL_TIMEOUT)
    try:
        h = llm.health(force=True)
        assert h["ok"] is True and h["model"] == "llama-3.1-8b-instant"
        assert any(auth == "Bearer sk-test-123" for _, auth in seen)
    finally:
        llm.close()

    # ALLOW_RULES_ONLY enables graceful rule fallback when model server is unreachable
    monkeypatch.setattr(config, "ALLOW_RULES_ONLY", True)

    def offline(messages, json_mode):
        raise llm.ModelUnavailable("offline")

    llm.set_backend(offline)
    sr = employees.ask_checked(
        "recruiter",
        "parse-cv",
        "CV",
        lambda d: (d, []),
        lambda: {"name": "Rule Fallback", "skills": ["Python"]},
    )
    assert sr.how == "fallback" and sr.value["name"] == "Rule Fallback"


def test_chief_of_staff_reports_failed_smtp_unmatched_vacancies_and_feedback(isolated):
    p = config.SAMPLES_DIR / "arjun_mehta.txt"
    r = actions.add_cv(p.name, p.read_bytes())
    flows.run_all_once()
    actions.invite_from_pool(r["app_no"], "AI/ML")
    flows.run_all_once()

    def broken_smtp(msg, cfg):
        raise OSError("connection refused")

    mailer.set_backend(broken_smtp)
    mailer.save_settings({"enabled": True, "host": "smtp.test", "user": "u@t.lk", "password": "pw"})
    cand = db.get_candidate(r["app_no"])
    with db.Session() as s:
        from sqlalchemy import select
        draft = s.scalar(select(db.Draft).where(db.Draft.candidate_id == cand.id))
    actions.release_draft(draft.id)

    actions.add_vacancy("Market Research", 1, "Need market research intern")
    supervisor.submit_feedback(reviewer="Dr. Silva", category="agents", rating=5, comment="Check brief")

    out = flows.run_chief()
    assert out["written_by"] in ("AI", "AI (fixed after a check)")
    with db.Session() as s:
        from sqlalchemy import select
        brief = s.scalar(select(db.Brief).order_by(db.Brief.id.desc()).limit(1))
    assert brief.facts["needs_a_person_today"]["failed_email_deliveries_to_retry"] == 1
    assert brief.facts["needs_a_person_today"]["open_feedback_items"] == 1
    assert brief.facts["open_vacancies_needing_cvs"] >= 1
    t_brief = flows._template_brief(brief.facts)
    assert "failed SMTP delivery" in t_brief and "supervisor feedback" in t_brief


# ---- 4. MongoDB Direct Mode, URI Masking & Aggregation --------------------------
def test_mongodb_direct_mode_indexes_masking_and_mock_query_params(isolated, monkeypatch):
    coll = FakeInternCollection()
    talent_hub.set_mongo_collection(coll)
    talent_hub.seed_sample_interns(40)

    monkeypatch.setattr(config, "TALENT_HUB_MODE", "mongo")
    monkeypatch.setattr(config, "TALENT_HUB_MONGO_URI", "mongodb+srv://hruser:SuperSecretPass!@cluster.example.net/")
    assert talent_hub.resolve_mode() == "mongo"

    # URI password masked cleanly in health diagnostics
    h = talent_hub.health(force=True)
    assert h["ok"] is True and h["mode"] == "mongo" and h["count"] == 40
    assert "SuperSecretPass!" not in h["url"] and "***" in h["url"]

    # Indexes include email, name, institute
    idx_names = {i["name"] for i in coll.indexes}
    assert {"Trainee_ID_1", "field_of_spec_name_1", "Training_EndDate_1",
            "Trainee_Email_1", "Trainee_Name_1", "Institute_1"} <= idx_names

    # Direct lookup + analytics in mongo mode without HTTP mock API
    one = talent_hub.get_intern("1001")
    assert one is not None and one["Trainee_ID"] == "1001"
    all_rows = talent_hub.list_interns(specialization=one["field_of_spec_name"], limit=50)
    assert all(x["field_of_spec_name"] == one["field_of_spec_name"] for x in all_rows)
    matches = talent_hub.find_matching_interns(email=one["Trainee_Email"])
    assert len(matches) == 1 and matches[0]["Trainee_ID"] == "1001"
    ma = talent_hub.mongo_analytics()
    assert ma["total"] == 40 and len(ma["by_institute"]) > 0

    # Mock API optional filters + /health endpoint
    from hiring import talent_hub_mock
    monkeypatch.setattr(config, "TALENT_HUB_API_TOKEN", "tok-123")
    mc = TestClient(talent_hub_mock.app)
    assert mc.get("/health").status_code == 200
    filtered = mc.get("/api/interns?q=1001&limit=5", headers={"Authorization": "Bearer tok-123"}).json()
    assert len(filtered) == 1 and filtered[0]["Trainee_ID"] == "1001"


def test_live_mongodb_aggregation_pipeline_when_mongod_running(monkeypatch):
    talent_hub.close()
    if not talent_hub.mongo_health()["ok"]:
        pytest.skip("Local mongod is not running")
    from pymongo import MongoClient

    temp_db = "slt_talent_hub_agg_pytest"
    monkeypatch.setattr(config, "TALENT_HUB_DB_NAME", temp_db)
    talent_hub.close()
    client = MongoClient(config.TALENT_HUB_MONGO_URI, serverSelectionTimeoutMS=2000)
    try:
        client.drop_database(temp_db)
        talent_hub.seed_sample_interns(60)
        stats = talent_hub.mongo_analytics()
        assert stats["total"] == 60
        assert stats["ending_soon"] >= 10
        assert sum(x["count"] for x in stats["by_specialization"]) == 60
        assert sum(x["count"] for x in stats["by_institute"]) == 60
    finally:
        talent_hub.close()
        client.drop_database(temp_db)
        client.close()


# ---- 5. Hosting Security, Proxy Origin, Demo Lock & Supervisor Feedback ---------
def test_hosting_auth_proxy_origins_demo_lock_and_feedback_api(isolated, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_HOSTS", ["localhost", "127.0.0.1", "testserver", ".hosted.test"])
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://cdn.hosted.test"])
    monkeypatch.setattr(config, "AUTH_USER", "supervisor")
    monkeypatch.setattr(config, "AUTH_PASS", "sup-pass-2026")
    monkeypatch.setattr(config, "ACCESS_TOKEN", "bearer-tok-xyz")
    monkeypatch.setattr(config, "DEMO_EMAIL_LOCK", True)
    monkeypatch.setattr(config, "TALENT_HUB_MODE", "mongo")

    coll = FakeInternCollection()
    talent_hub.set_mongo_collection(coll)
    talent_hub.seed_sample_interns(20)

    basic = "Basic " + base64.b64encode(b"supervisor:sup-pass-2026").decode()
    auth_hr = {"authorization": basic, "x-role": "hr"}

    # Demo Email Lock refuses sending to candidate directly when test_recipient is blank
    mailer.save_settings({"enabled": True, "host": "smtp.test", "user": "u@t.lk", "password": "pw", "test_recipient": ""})
    blocked = mailer.send_email("cand@outside.com", "Cand", "Offer", "Congratulations! " * 4)
    assert blocked["ok"] is False and "Demo email lock" in blocked["error"]

    with TestClient(app) as client:
        # /api/health always open without auth for platform readiness probes
        hp = client.get("/api/health")
        assert hp.status_code == 200 and hp.json()["ok"] is True

        # Unauthorized without credentials
        r_unauth = client.get("/api/state")
        assert r_unauth.status_code == 401 and "Basic" in r_unauth.headers.get("www-authenticate", "")

        # Bearer token works cleanly too
        assert client.get("/api/state", headers={"authorization": "Bearer bearer-tok-xyz"}).status_code == 200

        # Proxy host suffix + matching proxy origin allowed; evil origin rejected
        ok_proxy = client.post(
            "/api/feedback",
            headers={**auth_hr, "host": "demo.hosted.test", "origin": "https://demo.hosted.test"},
            json={"reviewer": "Prof. Perera", "category": "hosting", "rating": 5, "comment": "=SUM(A1) Smooth setup"},
        )
        assert ok_proxy.status_code == 200
        assert ok_proxy.headers.get("x-content-type-options") == "nosniff"
        fb_id = ok_proxy.json()["id"]

        bad_origin = client.post(
            "/api/feedback",
            headers={**auth_hr, "host": "demo.hosted.test", "origin": "https://evil.attacker"},
            json={"comment": "csrf"},
        )
        assert bad_origin.status_code == 403

        # Resolve feedback (requires HR) & export CSV / Markdown / Diagnostics / Pipeline Analytics
        assert client.post(
            f"/api/feedback/{fb_id}/resolve",
            headers={"authorization": basic, **IN},
            json={"resolution_note": "nope"},
        ).status_code == 400
        res = client.post(
            f"/api/feedback/{fb_id}/resolve",
            headers=auth_hr,
            json={"resolution_note": "Verified on staging"},
        )
        assert res.status_code == 200 and res.json()["status"] == "resolved"

        fb_list = client.get("/api/feedback?status=resolved", headers=auth_hr).json()
        assert fb_list["summary"]["total"] == 1 and fb_list["summary"]["avg_rating"] == 5.0

        csv_fb = client.get("/api/export/feedback.csv", headers=auth_hr)
        assert csv_fb.status_code == 200 and "'=SUM(A1) Smooth setup" in csv_fb.text

        md_rep = client.get("/api/export/supervisor-report.md", headers=auth_hr)
        assert md_rep.status_code == 200 and "Supervisor Evaluation & System Report" in md_rep.text

        diag = client.get("/api/diagnostics", headers=auth_hr).json()
        assert diag["ready"] is True and diag["sqlite"]["journal_mode"] == "wal"

        pipe = client.get("/api/analytics/pipeline", headers=auth_hr).json()
        assert pipe["mongodb_interns"]["total"] == 20 and pipe["feedback"]["total"] == 1

        db_json = client.get("/api/export/database.json", headers=auth_hr).json()
        assert len(db_json["feedback"]) == 1


def test_cli_doctor_feedback_and_report(isolated, monkeypatch, capsys, tmp_path):
    import run as cli_run

    coll = FakeInternCollection()
    talent_hub.set_mongo_collection(coll)
    talent_hub.seed_sample_interns(15)
    monkeypatch.setattr(config, "TALENT_HUB_MODE", "mongo")

    monkeypatch.setattr(sys, "argv", ["run.py", "doctor"])
    assert cli_run.main() == 0
    doc_out = json.loads(capsys.readouterr().out)
    assert doc_out["ready"] is True and doc_out["sqlite"]["journal_mode"] == "wal"

    monkeypatch.setattr(sys, "argv", [
        "run.py", "feedback", "--reviewer", "Supervisor", "--category", "mongodb", "--rating", "5", "--comment", "Direct Atlas ready"
    ])
    assert cli_run.main() == 0
    capsys.readouterr()

    out_md = tmp_path / "sup_report.md"
    monkeypatch.setattr(sys, "argv", ["run.py", "report", "--out", str(out_md)])
    assert cli_run.main() == 0
    assert out_md.exists() and "Direct Atlas ready" in out_md.read_text(encoding="utf-8")
