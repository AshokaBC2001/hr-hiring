"""Tests of the rules, checks and employee flows, using the scripted fake model.

    .venv/bin/python -m pytest tests -q
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel  # noqa: E402
from hiring import actions, checks, config, db, employees, flows, llm, talenthub, tools, vacancies  # noqa: E402

SAMPLES = config.SAMPLES_DIR


@pytest.fixture()
def fake(tmp_path):
    db.setup(tmp_path / "data")
    model = FakeModel()
    llm.set_backend(model)
    flows.NOTICE.update(text="", at="")
    yield model
    llm.set_backend(None)


def add(name: str, email: str = "") -> str:
    p = SAMPLES / name
    r = actions.add_cv(p.name, p.read_bytes(), email)
    assert r["ok"], r
    return r["app_no"]


def cand(app_no):
    return db.get_candidate(app_no)


IT_AREAS = {"AI/ML", "DevOps", "MLOps", "Flutter"}


def keep_only_areas(names: set) -> None:
    """Turns off every other starter area, so a test can use a CV that fits none of them."""
    with db.Session() as s:
        for a in s.scalars(select(db.Area)):
            a.active = a.name in names
        s.commit()


def set_area_active(name: str, active: bool) -> None:
    with db.Session() as s:
        s.scalar(select(db.Area).where(db.Area.name == name)).active = active
        s.commit()


def invite(app_no: str, area: str = "") -> None:
    """The HR officer invites a CV from the pool (for its best area unless another is given)."""
    actions.invite_from_pool(app_no, area or cand(app_no).area)


# ---------------------------------------------------------------- checks
CV = (SAMPLES / "arjun_mehta.txt").read_text()


def test_profile_check_removes_items_not_in_the_cv():
    data = {"name": "Arjun Mehta", "email": "arjun.mehta@example.com", "phone": "+91 98450 01122",
            "education": ["B.Tech in Data Science, Vellore Institute of Technology"],
            "skills": ["Python", "Pandas", "Kubernetes"], "projects": [], "experience": []}
    clean, problems, removed = checks.check_profile(data, CV)
    assert problems == []
    assert removed == ["Kubernetes"]
    assert "Kubernetes" not in clean["skills"] and "Python" in clean["skills"]


def test_profile_check_rejects_invented_name_and_email():
    clean, problems, _ = checks.check_profile({"name": "John Invented", "email": "fake@nowhere.com"}, CV)
    assert any("name" in p for p in problems)
    assert any("email" in p for p in problems)


def test_area_check_needs_evidence_from_the_cv():
    names = ["AI/ML", "DevOps"]
    good, problems = checks.check_area({"area": "machine learning", "reasons": ["Strong ML"], "evidence": ["Scikit-learn"]}, names, CV)
    assert problems == [] and good["area"] == "AI/ML"
    _, problems = checks.check_area({"area": "DevOps", "reasons": ["x"], "evidence": ["Kubernetes expert at Google"]}, names, CV)
    assert any("evidence" in p for p in problems)


def test_area_check_new_area_and_needs_review():
    r, _ = checks.check_area({"area": "New area: Human Resources", "reasons": ["HR"], "evidence": ["Python"]}, ["AI/ML"], CV)
    assert r["new_area"] == "Human Resources" and r["area"] == ""
    r, problems = checks.check_area({"area": "Needs human review", "reasons": ["unsure"], "evidence": []}, ["AI/ML"], CV)
    assert r["area"] == config.NEEDS_REVIEW_AREA and problems == []


def test_draft_check_catches_placeholders_numbers_and_missing_facts():
    facts = {"name": "Arjun Mehta", "area": "AI/ML", "app_no": "APP-0002", "interview": "Friday 23 October 2026 at 10:15"}
    bad = {"subject": "Hi", "body": "Dear [Name], come within 14 days for the interview. " * 3}
    _, problems = checks.check_draft(bad, facts, ["AI/ML", facts["interview"]])
    text = " ".join(problems)
    assert "placeholder" in text and "14" in text and "Arjun" in text and "AI/ML" in text
    good = tools.template_draft("invitation", facts)
    _, problems = checks.check_draft(good, facts, ["AI/ML", facts["interview"]])
    assert problems == []


def test_brief_check_rejects_invented_numbers():
    _, problems = checks.check_brief({"brief": "Good morning. 999 candidates applied. " * 4}, {"a": 3})
    assert any("999" in p for p in problems)


# ---------------------------------------------------------------- scoring
def test_scoring_uses_the_weights_and_finds_gaps(fake):
    area = db.area_by_name("DevOps")
    text = tools.read_cv(SAMPLES / "daniel_kim.txt")
    profile = tools.rule_profile(text, tools.skill_vocabulary(db.active_areas()))
    score, parts, gaps = tools.score_cv(profile, text, area)
    assert parts["skills"]["max"] == 40 and parts["projects"]["max"] == 30
    assert parts["education"]["max"] == 20 and parts["experience"]["max"] == 10
    assert sorted(gaps) == ["Bash", "CI/CD", "Cloud"]
    assert score == sum(p["points"] for p in parts.values()) or abs(score - sum(p["points"] for p in parts.values())) <= 2
    assert tools.shortlist_status(score) == "borderline"


def test_shortlist_thresholds():
    assert tools.shortlist_status(60) == "shortlisted"
    assert tools.shortlist_status(59) == "borderline"
    assert tools.shortlist_status(50) == "borderline"
    assert tools.shortlist_status(49) == "not_shortlisted_check"


# ---------------------------------------------------------------- work IDs
def _to_status(app_no, status, email=None):
    c = cand(app_no)
    fields = {"status": status}
    if email is not None:
        fields["email"] = email
    db.update_candidate(c.id, **fields)
    return c.id


def test_work_id_rules(fake):
    a = add("arjun_mehta.txt")
    b = add("emily_carter.txt")
    cid_a = _to_status(a, "shortlisted")
    with pytest.raises(PermissionError):
        db.issue_work_id(cid_a)                      # only after Pass
    _to_status(a, "passed")
    assert db.issue_work_id(cid_a) == 1001
    assert db.issue_work_id(cid_a) == 1001           # asking again gives the same ID
    cid_b = _to_status(b, "passed", email="other@example.com")
    assert db.issue_work_id(cid_b) == 1002           # counts up
    # a new application from the same email gets the same ID
    p = SAMPLES / "arjun_mehta.txt"
    r = actions.add_cv("again.txt", p.read_bytes() + b"\nUpdated CV", "arjun.mehta@example.com")
    _to_status(a, "passed", email="arjun.mehta@example.com")
    cid_c = _to_status(r["app_no"], "passed")
    assert db.issue_work_id(cid_c) == 1001
    assert db.get_setting("next_work_id") == "1003"  # never reused


# ---------------------------------------------------------------- Talent Hub and vacancies
def utc(day):
    return day.isoformat() + "T00:00:00.000Z"


def fake_talenthub(monkeypatch, docs):
    """Pretends Talent Hub returned these documents."""
    monkeypatch.setattr(talenthub, "_from_example", lambda: docs)


def test_talenthub_dates_are_read_as_local_days():
    # Talent Hub keeps UTC. Both of these are the same day in Sri Lanka (UTC+5:30).
    assert talenthub.as_local_date("2026-05-19T00:00:00.000Z") == "2026-05-19"
    assert talenthub.as_local_date("2026-05-18T18:30:00.000Z") == "2026-05-19"
    assert talenthub.as_local_date(datetime(2026, 5, 18, 18, 30, tzinfo=timezone.utc)) == "2026-05-19"
    assert talenthub.as_local_date("2026-05-19") == "2026-05-19"      # a plain date is kept
    assert talenthub.as_local_date(None) == "" and talenthub.as_local_date("rubbish") == ""


def test_sync_reads_talenthub_without_changing_it(fake, monkeypatch):
    soon = date.today() + timedelta(days=3)
    docs = [
        {"traineeId": "T1", "name": "A One", "startDate": utc(soon - timedelta(days=180)),
         "endDate": utc(soon), "specialization": "AI/ML"},
        {"traineeId": "T2", "name": "B Two", "startDate": utc(soon), "endDate": utc(soon),
         "specialization": "AI and ML"},                      # written differently: still AI/ML
        {"traineeId": "T3", "name": "C Three", "startDate": utc(soon), "endDate": utc(soon),
         "specialization": "Civil Engineering"},              # no area of ours fits
        {"traineeId": "T1", "name": "Copy", "startDate": utc(soon), "endDate": utc(soon),
         "specialization": "AI/ML"},                          # the same ID twice
        {"name": "No Id", "endDate": utc(soon), "specialization": "AI/ML"},   # no ID
    ]
    fake_talenthub(monkeypatch, docs)
    out = talenthub.sync()
    assert out == {"read": 3, "added": 3, "updated": 0, "unknown_specialization": 1}
    with db.Session() as s:
        rows = {i.trainee_id: i for i in s.scalars(select(db.Intern))}
    assert rows["T2"].area == "AI/ML" and rows["T2"].specialization == "AI and ML"
    assert rows["T3"].area == "" and rows["T3"].specialization == "Civil Engineering"
    assert rows["T1"].name == "A One"                         # the first record of an ID wins
    # reading again changes nothing; an extended end date is picked up
    assert talenthub.sync()["added"] == 0
    docs[0]["endDate"] = utc(soon + timedelta(days=90))
    assert talenthub.sync()["updated"] == 1


def test_a_vacancy_opens_only_within_the_notice_period(fake, monkeypatch):
    late = date.today() + timedelta(days=config.VACANCY_NOTICE_DAYS + 10)
    soon = date.today() + timedelta(days=2)
    fake_talenthub(monkeypatch, [
        {"traineeId": "T1", "name": "Later", "endDate": utc(late), "specialization": "AI/ML"},
        {"traineeId": "T2", "name": "Soon A", "endDate": utc(soon), "specialization": "MLOps"},
        {"traineeId": "T3", "name": "Soon B", "endDate": utc(soon), "specialization": "MLOps"},
    ])
    talenthub.sync()
    out = vacancies.find_and_update()
    assert out["created"] == 1
    v = db.vacancies_in("open")[0]
    assert v.area == "MLOps" and v.places == 2 and sorted(v.trainee_ids) == ["T2", "T3"]
    assert v.created_by == "Planner" and v.opens_on == soon.isoformat()
    vacancies.find_and_update()
    assert len(db.vacancies_in("open")) == 1                  # running again adds nothing


def test_an_extended_end_date_closes_the_vacancy_again(fake, monkeypatch):
    soon, late = date.today() + timedelta(days=2), date.today() + timedelta(days=200)
    docs = [{"traineeId": "T1", "name": "One", "endDate": utc(soon), "specialization": "Flutter"}]
    fake_talenthub(monkeypatch, docs)
    talenthub.sync()
    vacancies.find_and_update()
    assert len(db.vacancies_in("open")) == 1
    docs[0]["endDate"] = utc(late)                            # the internship was extended
    talenthub.sync()
    vacancies.find_and_update()
    assert db.vacancies_in("open") == [] and len(db.vacancies_in("closed")) == 1


def test_suggestions_are_ranked_and_only_strong_enough(fake, monkeypatch):
    for name in ("arjun_mehta.txt", "rahul_nair.txt", "ravi_kumar.txt"):
        add(name)
    flows.run_employee("recruiter", limit=10, wait=True)
    fake_talenthub(monkeypatch, [{"traineeId": "T1", "name": "One",
                                  "endDate": utc(date.today() + timedelta(days=2)), "specialization": "AI/ML"}])
    talenthub.sync()
    vacancies.find_and_update()
    v = db.vacancies_in("open")[0]
    scores = [s["score"] for s in v.suggestions]
    assert scores == sorted(scores, reverse=True) and min(scores) >= config.POOL_MIN_SCORE
    assert len(v.suggestions) <= v.places + config.VACANCY_RESERVES
    assert v.suggestions[0]["name"] == "Arjun Mehta"          # the best AI/ML CV comes first
    assert all(s["state"] == "suggested" for s in v.suggestions)


def _pass_interview(app_no, result="Pass"):
    flows.run_all_once()
    _release_waiting(app_no)
    actions.enter_result(app_no, result)
    flows.run_all_once()


def test_approving_a_suggestion_starts_the_hiring_flow_and_fills_the_place(fake, monkeypatch):
    add("arjun_mehta.txt")
    flows.run_employee("recruiter", wait=True)
    fake_talenthub(monkeypatch, [{"traineeId": "T1", "name": "One",
                                  "endDate": utc(date.today() + timedelta(days=2)), "specialization": "AI/ML"}])
    talenthub.sync()
    vacancies.find_and_update()
    v = db.vacancies_in("open")[0]
    app_no = v.suggestions[0]["app_no"]
    actions.approve_suggestion(v.id, app_no)
    c = cand(app_no)
    assert c.status == "shortlisted" and c.vacancy_id == v.id
    with pytest.raises(actions.ActionError):
        actions.approve_suggestion(v.id, app_no)              # only once
    _pass_interview(app_no)
    v = db.get_vacancy(v.id)
    assert v.status == "filled" and v.filled == 1
    assert [s["state"] for s in v.suggestions if s["app_no"] == app_no] == ["passed"]


def test_a_candidate_who_fails_is_replaced_from_the_pool(fake, monkeypatch):
    for name in ("arjun_mehta.txt", "rahul_nair.txt"):
        add(name)
    flows.run_employee("recruiter", limit=10, wait=True)
    fake_talenthub(monkeypatch, [{"traineeId": "T1", "name": "One",
                                  "endDate": utc(date.today() + timedelta(days=2)), "specialization": "AI/ML"}])
    talenthub.sync()
    vacancies.find_and_update()
    v = db.vacancies_in("open")[0]
    first, second = v.suggestions[0]["app_no"], v.suggestions[1]["app_no"]
    actions.approve_suggestion(v.id, first)
    _pass_interview(first, "Fail")
    v = db.get_vacancy(v.id)
    assert v.status == "open" and v.filled == 0
    assert [s["state"] for s in v.suggestions if s["app_no"] == first] == ["failed"]
    actions.approve_suggestion(v.id, second)                  # the next one can take the place
    _pass_interview(second)
    assert db.get_vacancy(v.id).status == "filled"


def test_one_cv_cannot_be_approved_for_two_areas(fake, monkeypatch):
    add("rahul_nair.txt")                                     # fits MLOps, AI/ML and DevOps
    flows.run_employee("recruiter", wait=True)
    soon = utc(date.today() + timedelta(days=2))
    fake_talenthub(monkeypatch, [
        {"traineeId": "T1", "name": "One", "endDate": soon, "specialization": "MLOps"},
        {"traineeId": "T2", "name": "Two", "endDate": soon, "specialization": "AI/ML"}])
    talenthub.sync()
    vacancies.find_and_update()
    mlops = [v for v in db.vacancies_in("open") if v.area == "MLOps"][0]
    actions.approve_suggestion(mlops.id, "APP-0001")
    other = [v for v in db.vacancies_in("open") if v.area == "AI/ML"][0]
    assert [s["state"] for s in other.suggestions if s["app_no"] == "APP-0001"] == ["taken"]
    with pytest.raises(actions.ActionError):
        actions.approve_suggestion(other.id, "APP-0001")


def test_the_hr_officer_can_open_and_close_a_vacancy(fake):
    add("arjun_mehta.txt")
    flows.run_employee("recruiter", wait=True)
    vid = actions.add_vacancy("AI/ML", 2, "An intern left early.")
    v = db.get_vacancy(vid)
    assert v.created_by == "HR officer" and v.places == 2 and v.suggestions
    with pytest.raises(actions.ActionError):
        actions.add_vacancy("AI/ML", 0, "")                   # at least one place
    with pytest.raises(actions.ActionError):
        actions.add_vacancy("No Such Area", 1, "")
    actions.close_vacancy(vid)
    assert db.get_vacancy(vid).status == "closed"
    with pytest.raises(actions.ActionError):
        actions.approve_suggestion(vid, v.suggestions[0]["app_no"])


def test_declining_a_suggestion_brings_in_the_next_one(fake):
    for name in ("arjun_mehta.txt", "rahul_nair.txt", "priya_sharma.txt"):
        add(name)
    flows.run_employee("recruiter", limit=10, wait=True)
    vid = actions.add_vacancy("AI/ML", 1, "Extra place.")
    v = db.get_vacancy(vid)
    first = v.suggestions[0]["app_no"]
    actions.decline_suggestion(vid, first)
    v = db.get_vacancy(vid)
    assert [s["state"] for s in v.suggestions if s["app_no"] == first] == ["failed"]
    assert cand(first).status == "in_pool"                    # still in the pool for other areas


def test_the_planner_runs_every_step_and_survives_talenthub_being_down(fake, monkeypatch):
    add("arjun_mehta.txt")
    flows.run_employee("recruiter", wait=True)
    fake_talenthub(monkeypatch, [{"traineeId": "T1", "name": "One",
                                  "endDate": utc(date.today() + timedelta(days=2)), "specialization": "AI/ML"}])
    out = flows.run_planner()
    assert out["sync"]["added"] == 1 and out["found"]["created"] == 1
    assert out["suggested"]["waiting_for_approval"] >= 1

    def down():
        raise talenthub.TalentHubUnavailable("Talent Hub could not be read.")
    monkeypatch.setattr(talenthub, "_from_example", down)
    out = flows.run_planner()
    assert "could not be read" in out["error"]
    assert len(db.vacancies_in("open")) == 1                  # the copy we already have still works


# ---------------------------------------------------------------- employee files
def test_employee_folders_are_complete():
    for key in employees.EMPLOYEES:
        assert employees.role_text(key)
        assert employees.role_summary(key)
        plan = employees.schedule(key)
        assert plan["every_seconds"] or plan["daily_at"]
        for r in employees.routines(key):
            if r["uses_model"]:
                assert "JSON" in employees.instructions(key, r["name"])


# ---------------------------------------------------------------- Recruiter
def test_recruiter_reads_scores_and_adds_to_the_pool(fake):
    a = add("arjun_mehta.txt")
    flows.run_employee("recruiter", wait=True)
    c = cand(a)
    assert c.status == "in_pool" and c.area == "AI/ML" and c.score >= 60
    assert set(c.area_scores) == {x.name for x in db.active_areas()}       # scored for every area
    assert c.area_scores["AI/ML"]["score"] == c.score
    flows.run_employee("coordinator", wait=True)
    assert cand(a).status == "in_pool"                 # nothing is drafted while the CV waits in the pool
    assert c.email == "arjun.mehta@example.com" and c.email_source == "Read from CV"
    assert c.area_evidence and all(checks.CVText(c.cv_text).has(e) for e in c.area_evidence)


def test_recruiter_fixes_a_bad_answer(fake):
    fake.bad["parse-cv"] = 1
    a = add("arjun_mehta.txt")
    flows.run_employee("recruiter", wait=True)
    c = cand(a)
    assert c.profile_source == "AI" and c.name == "Arjun Mehta"
    with db.Session() as s:
        from sqlalchemy import select
        outcomes = [r.outcome for r in s.scalars(select(db.Run).where(db.Run.routine == "parse-cv"))]
    assert outcomes == ["fixed"]
    assert employees.lessons("recruiter")            # learned a lesson
    assert "Arjun" not in (db.paths.notes / "recruiter.md").read_text()   # no personal data in lessons


def test_recruiter_falls_back_to_rules_and_flags(fake):
    fake.bad["parse-cv"] = 5
    fake.bad["choose-area"] = 5
    a = add("emily_carter.txt")
    flows.run_employee("recruiter", wait=True)
    c = cand(a)
    assert c.profile_source == "Rules"
    assert c.area == "DevOps" and c.area_source == "Chosen by rules"
    assert any("simple rules" in f["text"] for f in c.flags)


def test_hr_cv_matches_the_human_resources_area(fake):
    a = add("laura_bennett.pdf")
    flows.run_employee("recruiter", wait=True)
    c = cand(a)
    assert c.area == "Human Resources" and c.status == "in_pool"


def test_cv_that_fits_no_area_gets_a_new_area_suggestion_not_forced_into_one(fake):
    keep_only_areas(IT_AREAS)                        # only IT areas: the HR CV fits none of them
    a = add("laura_bennett.pdf")
    flows.run_employee("recruiter", wait=True)
    c = cand(a)
    assert c.status == "no_area" and c.area == "" and c.suggested_new_area == "Human Resources"
    assert not any(f["open"] for f in c.flags)       # it waits in the pool, not in the Review queue
    set_area_active("Human Resources", True)         # the HR officer adds the area ...
    assert actions.rescore_pool()["requeued"] == 1   # ... so the CV is read again
    flows.run_employee("recruiter", wait=True)
    assert cand(a).status == "in_pool" and cand(a).area == "Human Resources"


def test_invite_from_the_pool_starts_the_hiring_flow(fake):
    a = add("daniel_kim.txt")
    flows.run_employee("recruiter", wait=True)
    assert cand(a).status == "in_pool" and cand(a).area == "DevOps" and cand(a).score == 55
    invite(a, "MLOps")                               # the HR officer may pick another area
    c = cand(a)
    assert c.status == "shortlisted" and c.area == "MLOps" and c.area_source == "Chosen by HR officer"
    assert c.score == c.area_scores["MLOps"]["score"]
    with pytest.raises(actions.ActionError):
        invite(a)                                    # it is no longer in the pool
    flows.run_employee("coordinator", wait=True)
    assert cand(a).status == "invite_ready"


def test_archive_restore_and_six_month_expiry(fake):
    a = add("emily_carter.txt")
    b = add("rahul_nair.txt")
    flows.run_employee("recruiter", wait=True)
    actions.archive(a)
    assert cand(a).status == "archived"
    with pytest.raises(actions.ActionError):
        invite(a)                                    # archived CVs cannot be invited
    actions.restore(a)
    flows.run_employee("recruiter", wait=True)
    assert cand(a).status == "in_pool"
    old = (datetime.now() - timedelta(days=config.POOL_EXPIRE_DAYS + 1)).replace(microsecond=0).isoformat()
    db.update_candidate(cand(b).id, created_at=old)
    flows.run_chief()
    assert cand(b).status == "archived" and cand(a).status == "in_pool"


def test_rescoring_when_areas_change(fake):
    a = add("daniel_kim.txt")
    flows.run_employee("recruiter", wait=True)
    set_area_active("DevOps", False)                  # its best area is turned off
    actions.rescore_pool()
    c = cand(a)
    assert c.status == "in_pool" and c.area != "DevOps" and "DevOps" not in c.area_scores
    assert c.area == max(c.area_scores, key=lambda k: c.area_scores[k]["score"])


def test_unreadable_cv_needs_a_person(fake):
    r = actions.add_cv("scan.txt", b"   \n  ")
    assert r["ok"]
    flows.run_employee("recruiter", wait=True)
    assert cand(r["app_no"]).status == "needs_person"


def test_model_down_stops_without_changing_anything(fake):
    a = add("arjun_mehta.txt")

    def down(messages, json_mode):
        raise llm.ModelUnavailable("The model server is not running.")
    llm.set_backend(down)
    flows.run_employee("recruiter", wait=True)
    assert cand(a).status == "new"
    assert "not running" in flows.NOTICE["text"]


# ---------------------------------------------------------------- whole flows
def _release_waiting(app_no):
    with db.Session() as s:
        from sqlalchemy import select
        d = s.scalar(select(db.Draft).where(db.Draft.candidate_id == cand(app_no).id, db.Draft.status == "waiting"))
    return actions.release_draft(d.id)


def test_pass_flow_end_to_end(fake):
    a = add("arjun_mehta.txt")
    flows.run_all_once()
    assert cand(a).status == "in_pool"
    invite(a)
    flows.run_all_once()
    c = cand(a)
    assert c.status == "invite_ready" and c.interview_label and len(c.questions) >= 4
    with pytest.raises(actions.ActionError):
        actions.enter_result(a, "Pass")              # invitation not released yet
    assert _release_waiting(a) == "awaiting_result"
    assert list(db.paths.outbox.glob("*.txt"))       # saved, not sent
    actions.enter_result(a, "Pass", "Good")
    flows.run_all_once()
    c = cand(a)
    assert c.status == "offer_ready" and c.work_id == 1001 and c.checklist
    assert _release_waiting(a) == "closed"
    assert cand(a).outcome.startswith("Selected")


def test_fail_and_hold_flows(fake):
    a = add("emily_carter.txt")
    b = add("rahul_nair.txt")
    flows.run_all_once()
    invite(a)
    invite(b)
    flows.run_all_once()
    _release_waiting(a)
    _release_waiting(b)
    actions.enter_result(a, "Fail")
    actions.enter_result(b, "Hold")
    flows.run_all_once()
    assert cand(a).status == "reply_ready" and cand(a).work_id is None
    assert cand(b).status == "hold_note_ready"
    _release_waiting(a)
    _release_waiting(b)
    assert cand(a).status == "closed" and cand(b).status == "on_hold"
    with pytest.raises(actions.ActionError):
        actions.enter_result(b, "Hold")              # from On hold only Pass or Fail
    actions.enter_result(b, "Pass")
    flows.run_all_once()
    assert cand(b).work_id == 1001


def test_not_shortlisted_needs_confirmation_then_reply(fake):
    a = add("ravi_kumar.txt")
    flows.run_employee("recruiter", wait=True)
    assert cand(a).status == "in_pool" and cand(a).score < 50
    _to_status(a, "not_shortlisted_check")           # the confirmation step still works when used
    out = actions.choose_area(a, "AI/ML")             # a different area is also scored low
    assert out["status"] == "not_shortlisted_check"
    flows.run_employee("coordinator", wait=True)
    assert cand(a).status == "not_shortlisted_check"  # no reply before the HR officer confirms
    actions.decide(a, "confirm")
    flows.run_employee("coordinator", wait=True)
    assert cand(a).status == "reply_ready"


def test_bad_drafts_fall_back_to_the_template(fake):
    fake.bad["invite-and-slots"] = 5
    a = add("arjun_mehta.txt")
    flows.run_all_once()
    invite(a)
    flows.run_all_once()
    with db.Session() as s:
        from sqlalchemy import select
        d = s.scalar(select(db.Draft).where(db.Draft.candidate_id == cand(a).id))
    assert d.written_by.startswith("Template")
    assert "[Name]" not in d.body and cand(a).interview_label in d.body


def test_release_needs_an_email(fake):
    p = SAMPLES / "arjun_mehta.txt"
    text = p.read_text().replace("arjun.mehta@example.com | ", "")
    r = actions.add_cv("no_email.txt", text.encode())
    flows.run_all_once()
    invite(r["app_no"])
    flows.run_all_once()
    c = cand(r["app_no"])
    assert c.email == "" and any("No email" in f["text"] for f in c.flags)
    with pytest.raises(actions.ActionError):
        _release_waiting(r["app_no"])
    actions.set_email(r["app_no"], "a.mehta@example.com")
    assert _release_waiting(r["app_no"]) == "awaiting_result"


def test_morning_brief(fake):
    add("arjun_mehta.txt")
    flows.run_all_once()
    out = flows.run_chief()
    assert out["written_by"] in ("AI", "AI (fixed after a check)")
    fake.bad["morning-brief"] = 5
    out = flows.run_chief()
    assert out["written_by"].startswith("Template")


def test_duplicate_upload_is_refused(fake):
    add("arjun_mehta.txt")
    p = SAMPLES / "arjun_mehta.txt"
    r = actions.add_cv(p.name, p.read_bytes())
    assert not r["ok"] and "already added" in r["reason"]
    assert not actions.add_cv("cv.exe", b"x")["ok"]
    assert not actions.add_cv("big.txt", b"x" * (config.MAX_UPLOAD_BYTES + 1))["ok"]
