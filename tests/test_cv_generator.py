"""Tests for the 400 unique Sri Lankan CV PDF generator, pypdf parsing, and Pool/Agent integration."""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel  # noqa: E402
from fake_talent_hub import FakeInternCollection  # noqa: E402
from hiring import checks, config, cv_generator, db, flows, llm, talent_hub, tools, vacancies  # noqa: E402


@pytest.fixture()
def pool_env(tmp_path):
    db.setup(tmp_path / "data")
    model = FakeModel()
    llm.set_backend(model)
    flows.NOTICE.update(text="", at="")
    yield tmp_path
    llm.set_backend(None)
    talent_hub.set_backend(None)
    talent_hub.set_mongo_collection(None)
    talent_hub.close()


def test_generate_400_specs_strictly_unique_and_disjoint_from_mongodb():
    specs = cv_generator.generate_cv_specs(400)
    assert len(specs) == 400

    names = {s.name for s in specs}
    emails = {s.email for s in specs}
    phones = {s.phone for s in specs}
    skill_sets = {tuple(sorted(s.skills)) for s in specs}
    assert len(names) == 400
    assert len(emails) == 400
    assert len(phones) == 400
    assert len(skill_sets) == 400

    # Zero overlap with all 400 MongoDB sample interns
    mongo_rows = talent_hub.generate_sample_interns(400)
    mongo_names = {r["Trainee_Name"].strip().lower() for r in mongo_rows}
    mongo_emails = {r["Trainee_Email"].strip().lower() for r in mongo_rows}
    assert {n.lower() for n in names}.isdisjoint(mongo_names)
    assert {e.lower() for e in emails}.isdisjoint(mongo_emails)

    # Diverse mix across IT, non-IT, strong/borderline/developing/unmapped tiers
    tiers = {s.tier for s in specs}
    assert {"strong", "borderline", "developing", "unmapped"} <= tiers
    areas = {s.target_area for s in specs}
    assert {"AI/ML", "DevOps", "MLOps", "Flutter", "Web Development", "Backend Development"} <= areas


def test_generated_pdfs_parse_cleanly_with_pypdf_and_pass_checks(pool_env):
    out_dir = pool_env / "pdfs"
    res = cv_generator.write_cv_pdfs(out_dir=out_dir, count=30)
    assert res["written"] == 30
    assert Path(res["manifest"]).exists()
    manifest = json.loads(Path(res["manifest"]).read_text(encoding="utf-8"))
    assert manifest["count"] == 30

    vocab = tools.skill_vocabulary(db.active_areas())
    areas = db.active_areas()
    for p_str, spec in zip(res["paths"], res["specs"]):
        path = Path(p_str)
        raw_bytes = path.read_bytes()
        assert raw_bytes.startswith(b"%PDF-1.4") and b"%%EOF" in raw_bytes
        extracted = tools.read_cv(path)
        assert spec.name in extracted and spec.email in extracted
        for sk in spec.skills[:3]:
            assert checks.CVText(extracted).has(sk)
        prof = tools.rule_profile(extracted, vocab)
        assert prof["name"] == spec.name
        assert prof["email"] == spec.email
        assert prof["phone"] == spec.phone
        assert len(prof["projects"]) >= 1
        scores = tools.score_all_areas(prof, extracted, areas)
        assert len(scores) == len(areas)


def test_load_generated_pdfs_into_pool_and_agents_pipeline(pool_env, monkeypatch):
    out_dir = pool_env / "pdfs"
    res = cv_generator.write_cv_pdfs(out_dir=out_dir, count=25)
    pdf_paths = [Path(p) for p in res["paths"]]
    by_name = {s.filename: s for s in res["specs"]}

    stats1 = cv_generator.load_pdfs_into_register(pdf_paths, pending_ai=3, specs_by_filename=by_name)
    assert stats1 == {"added_pool": 22, "added_new": 3, "skipped": 0, "total_loaded": 25}

    # Re-running is idempotent via cv_hash deduplication
    stats2 = cv_generator.load_pdfs_into_register(pdf_paths, pending_ai=3, specs_by_filename=by_name)
    assert stats2["skipped"] == 25 and stats2["total_loaded"] == 0

    assert len(db.candidates_in({"new"})) == 3
    assert len(db.candidates_in(config.POOL_STATUSES)) == 22

    # Recruiter AI processes the 3 pending new PDF CVs into the Pool cleanly
    flows.run_employee("recruiter", limit=5, wait=True)
    assert db.candidates_in({"new"}) == []
    assert len(db.candidates_in(config.POOL_STATUSES)) == 25

    # Planner sees MongoDB interns finishing soon and suggests ranked generated PDF candidates
    coll = FakeInternCollection()
    talent_hub.set_mongo_collection(coll)
    talent_hub.seed_sample_interns(50)
    monkeypatch.setattr(config, "TALENT_HUB_MODE", "mongo")

    plan = flows.run_planner()
    assert plan["found"]["created"] > 0
    open_vacs = db.vacancies_in("open")
    assert any(len(v.suggestions or []) > 0 for v in open_vacs)


def test_cli_generate_cvs_command(pool_env, monkeypatch, capsys):
    import run as cli_run

    out_dir = pool_env / "cli_pdfs"
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "generate-cvs", "--count", "12", "--out", str(out_dir), "--load-db", "--pending-ai", "2"],
    )
    assert cli_run.main() == 0
    out_txt = capsys.readouterr().out
    assert "Generated 12 PDF CVs" in out_txt
    assert len(list(out_dir.glob("cv_*.pdf"))) == 12
    assert len(db.candidates_in({"new"})) == 2
    assert len(db.candidates_in(config.POOL_STATUSES)) == 10
