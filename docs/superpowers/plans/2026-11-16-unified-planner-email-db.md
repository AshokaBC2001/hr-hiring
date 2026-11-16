# Unified Planner, Live Email Sender & Database Upgrades Plan

> **For agentic workers:** Implementing sequentially with TDD, keeping `main` untouched and all changes on `feat/talent-hub-mongodb`.

**Goal:** Integrate Ashoka's `Planner` AI employee (CV pool + vacancy suggestions) wired directly to MongoDB/TalentHub API (with NO duplicate SQLite `Intern` table), add a production-grade SMTP `mailer.py` candidate email sender with safe Demo Override, and expand database features across MongoDB and SQLite.

**Architecture:** All intern records stay exclusively in MongoDB (`slt_talent_hub_sample.interns`) accessed via `hiring/talent_hub.py` REST API / test double. Hiring entities (`Candidate`, `Draft`, `Vacancy`, `Area`, `Event`, `Run`, `Brief`, `Setting`) remain in `register.db` with auto-migrating columns. Releasing an approved draft archives to `data/outbox/*.txt` and dispatches live SMTP HTML+text email when enabled.

**Tech Stack:** Python 3, FastAPI, SQLAlchemy 2, LangGraph, APScheduler, PyMongo, httpx, stdlib `smtplib`/`email`, Vanilla JS SPA.

**Spec:** [2026-11-16-unified-planner-email-db-design.md](../specs/2026-11-16-unified-planner-email-db-design.md)

## Global Constraints
- Zero `db.Intern` table in SQLite and no `shared/example_interns.json` duplicate file.
- `Trainee_Email` & `Institute` optional fields populated cleanly in MongoDB without breaking `$jsonSchema`.
- Existing `pytest` suite (`tests/`) runs completely offline on GitHub Actions CI (`ubuntu-latest`, no `mongod` or SMTP server).
- Never touch `main`; work exclusively on `feat/talent-hub-mongodb`.

## Review Focus
- Upgrading an existing `data/register.db` without losing rows (`_add_missing_columns()` handles `candidates` AND `drafts`).
- Demo safety: live SMTP disabled by default (`SMTP_ENABLED=0`) and `SMTP_TEST_RECIPIENT` redirects `@example.com` emails cleanly during live demos.
- `release_draft()` stays atomic for candidate progression even if SMTP fails (writes outbox archive, marks `delivery_status="failed"`, exposes Retry).
- Planner survives TalentHub API offline errors cleanly without crashing APScheduler or wiping open vacancies.
- Timezone conversion: TalentHub UTC dates (`...T00:00:00.000Z`) convert cleanly using Sri Lanka `UTC+5:30`.

---

### Task 1: Config, Aliases & SQLite Schema Migration
- **Files:** `hiring/config.py`, `hiring/checks.py`, `hiring/db.py`
- **Changes:**
  - Add Pool, Vacancy, Local TZ, and SMTP settings defaults in `config.py`.
  - Extend `STATUSES`, `POOL_STATUSES`, `NOT_IN_HIRING`.
  - Add `"network engineering": "Networking"` (safe if `"Networking"` active) in `checks.ALIASES`.
  - Add `Candidate` columns (`vacancy_id`, `area_scores`, `hr_notes`), `Draft` columns (`delivery_status`, `delivery_error`, `sent_at`, `sent_to`, `message_id`), `Vacancy` table, helpers (`vacancies_in`, `get_vacancy`, `update_vacancy`), and `_add_missing_columns()`.
- **Verify:** `pytest tests/test_core.py -v`

### Task 2: Live Email Sender Module (`hiring/mailer.py`)
- **Files:** Create `hiring/mailer.py`; Test: `tests/test_mailer.py`
- **Changes:**
  - Implement settings reader/writer combining env defaults and `db.Setting`, HTML/text multipart renderer, `send_email()`, `send_test_email()`, `set_backend()` hook.
  - Ensure `SMTP_TEST_RECIPIENT` redirects deliveries when set and logs actual target.
- **Verify:** `pytest tests/test_mailer.py -v`

### Task 3: MongoDB / TalentHub Enrichment & Planner Query Layer
- **Files:** Modify `hiring/talent_hub.py`, `tests/test_talent_hub.py`
- **Changes:**
  - Enrich `generate_sample_interns(count=400)` with Sri Lankan universities (`Institute`), `Trainee_Email`, and dynamic rolling cohort window (`today + 1..7 days` for ~12 non-reserved IT trainees).
  - Add `as_local_date`, `interns_with_area`, `interns_ending_by`, `analytics`, `find_matching_interns`, `enroll_sample_intern`.
- **Verify:** `pytest tests/test_talent_hub.py -v`

### Task 4: Planner AI Employee, CV Pool & Vacancy Engine
- **Files:** Create `employees/planner/*`, `employees/recruiter/routines/add-to-pool/SKILL.md`, `employees/chief-of-staff/routines/archive-old-cvs/SKILL.md`, `hiring/vacancies.py`; Modify `employees/recruiter/*`, `employees/chief-of-staff/*`, `hiring/tools.py`, `hiring/employees.py`, `hiring/flows.py`, `hiring/scheduler.py`
- **Changes:**
  - Add `score_all_areas`, `top_areas` in `tools.py`.
  - Implement `vacancies.py` backed directly by `talent_hub.interns_ending_by(...)`.
  - Update `Recruiter` -> CV Pool, `Chief of staff` -> `archive-old-cvs`, add 5th AI Employee `Planner`.
- **Verify:** Unit & graph execution tests in `tests/test_core.py`.

### Task 5: Actions, Server Endpoints & CLI
- **Files:** Modify `hiring/actions.py`, `hiring/server.py`, `run.py`
- **Changes:**
  - Add Pool/Vacancy actions, SMTP release/resend/settings/test actions, checklist toggle, HR notes, sample intern enrollment.
  - Wire unified endpoints (`/api/pool`, `/api/vacancies*`, `/api/interns*`, `/api/email/*`, `/api/drafts/*/resend`, `/api/candidates/*`, `/api/export/*`).
  - Keep `run.py` CLI helpers (`seed-talent-hub`, `interns`, plus `plan`).

### Task 6: Unified Web Frontend
- **Files:** Modify `web/index.html`, `web/style.css`, `web/app.js`
- **Changes:**
  - Add Pool, Vacancies, unified Interns (Sync/Planner trigger, cohort/spec/institute analytics, sort, pagination, CSV export, raw modal), Email Settings & Test modal + delivery status tags on Replies & Candidate detail, interactive Checklist checkboxes, HR Notes, Returning-Intern badge, Enroll to Sample MongoDB button, CSV/JSON Export buttons.
- **Verify:** `node --check web/app.js`

### Task 7: Integration Tests, Live Verification & Push
- **Files:** Update `tests/test_core.py`, `tests/test_api.py`, `tests/test_talent_hub.py`, `README.md`
- **Verification:** Run full pytest suite, re-seed live MongoDB with 400 enriched records, restart servers if needed, verify `/api/state`, `/api/interns`, `/api/vacancies`, `/api/pool`, `/api/email/settings`, and push to `origin/feat/talent-hub-mongodb`.
