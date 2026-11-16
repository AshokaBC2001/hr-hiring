# Unified Planner Agent, Live Email Sender & Database Upgrades Design

**Date:** 2026-11-16
**Target Branch:** `feat/talent-hub-mongodb` (never modify `main`)

## Intent & Why
Combine three capabilities into a single cohesive `hr-hiring` system without duplicating databases:
1. **Ashoka's `Planner` agent, CV Pool, and Vacancy Suggestions** (ported from `origin/vacancy-suggestions`) wired directly to our **MongoDB + Bearer-token TalentHub API** — explicitly **without** Ashoka's duplicate SQLite `Intern` mirror table or `shared/example_interns.json`.
2. **Live Candidate Email Sender** (`hiring/mailer.py`), upgrading candidate email release from outbox-only text files to configurable SMTP delivery with a safe Demo Override recipient, per-draft delivery status tracking, retry support, and live test email verification.
3. **Database Upgrades across MongoDB & SQLite**, including enriched Sri Lankan intern records (`Institute`, `Trainee_Email`, relative cohort dates ensuring live upcoming vacancies), MongoDB analytics, returning-intern detection on candidate pages, optional sample-mode onboarding enrollment into MongoDB, interactive onboarding checklist persistence, HR notes, and CSV/JSON database exports.

## Goals & Architecture
### 1. Single-Source MongoDB Interns + `Planner` Agent (No Duplicate SQLite Intern Table)
- Keep `slt_talent_hub_sample.interns` in MongoDB (`hiring/talent_hub.py` + `hiring/talent_hub_mock.py`) as the **sole** intern store. Do **not** add `db.Intern` in SQLite, `shared/example_interns.json`, or a duplicate `hiring/talenthub.py`.
- Extend `hiring/talent_hub.py`:
  - Populate deterministic `Trainee_Email` (`<slug>.<id>@interns.slt.lk`) and Sri Lankan `Institute` across all 400 generated interns while preserving `REQUIRED_FIELDS` (`1042` Kamal Perera, `1043` Nimali Silva) and strict `$jsonSchema` validation.
  - Ensure a deterministic slice (~12 interns across core IT areas) have `Training_EndDate` within `today + 1..7 days` (relative to `date.today()` when generated/seeded) so `Planner` always finds live opening vacancies during demos.
  - Add `as_local_date(value)` (`UTC+5:30`), `interns_with_area(area_names)`, `interns_ending_by(cutoff_iso, area_names)`, `analytics()`, and `find_matching_interns(email, name)`.
  - Add sample-only `enroll_sample_intern(record)` helper guarded cleanly for explicit HR enrollment of accepted candidates into local sample MongoDB (never used in read-only external TalentHub mode).
- Port Ashoka's `employees/planner/` (`ROLE.md`, `SCHEDULE.md`, `RELEASES.md`, routines `read-interns`/`sync-interns`, `find-vacancies`, `suggest-candidates`), `employees/chief-of-staff/routines/archive-old-cvs/SKILL.md`, and `employees/recruiter/` CV Pool skills (`add-to-pool`).
- Port `hiring/vacancies.py` updated to read intern records directly from `talent_hub.interns_ending_by(...)` and link suggestions to `db.Vacancy` and `db.Candidate.area_scores` / `vacancy_id`.
- Wire `Planner` into `hiring/employees.py`, `hiring/flows.py` (`PLANNER_LOCK`, `PLANNER_GRAPH`, `run_planner`), `hiring/scheduler.py` (`07:30`), and `hiring/server.py` (`/api/vacancies*`, `/api/pool`, `/api/run` supporting `"planner"`, `/api/candidates/{app_no}/invite`, `/archive`, `/restore`, `/vacancies/plan`).

### 2. Live Candidate Email Sender (`hiring/mailer.py`)
- Create `hiring/mailer.py` using Python stdlib `smtplib` + `email.message.EmailMessage` supporting plain + styled HTML multipart messages, STARTTLS (587), SSL (465), or plain SMTP (e.g. local Mailpit/MailHog on port 1025), and `set_backend()` hook for zero-network pytest verification.
- Configuration defaults in `hiring/config.py` (`HR_SMTP_*` env vars) overridable at runtime via `db.Setting` keys (`smtp_enabled`, `smtp_host`, `smtp_port`, `smtp_user`, `smtp_password`, `smtp_use_tls`, `smtp_use_ssl`, `smtp_from_name`, `smtp_from_email`, `smtp_test_recipient`) managed in UI (`Email settings & test` modal). Passwords are write-only / masked (`has_password`) in API GETs.
- Keep `data/outbox/*.txt` archive always written on `release_draft()` for audit safety. When `smtp_enabled` is true, dispatch via `mailer.send_email()`, recording `delivery_status` (`sent` / `failed` / `outbox_only`), `sent_to` (reflecting any `smtp_test_recipient` demo override), `sent_at`, `message_id`, and `delivery_error` on `db.Draft`.
- Expose `/api/email/settings` (`GET`/`POST`), `/api/email/test` (`POST`), and `/api/drafts/{id}/resend` (`POST`).

### 3. SQLite & UI Improvements
- Extend `hiring/db.py` (`Candidate`, `Draft`, `Vacancy`, plus `_add_missing_columns()` SQLite `ALTER TABLE` auto-migration for backward compatibility with existing `register.db`):
  - `Candidate`: `vacancy_id`, `area_scores`, `hr_notes` (JSON list).
  - `Draft`: `delivery_status`, `delivery_error`, `sent_at`, `sent_to`, `message_id`.
  - `Vacancy`: `id`, `area`, `places`, `opens_on`, `reason`, `trainee_ids`, `suggestions`, `filled`, `status`, `created_by`, `created_at`, `updated_at`.
- Add interactive onboarding checklist toggling (`POST /api/candidates/{app_no}/checklist/{index}`), HR candidate notes (`POST /api/candidates/{app_no}/notes`), returning-intern lookup in `detail()`, and CSV/JSON exports (`GET /api/export/candidates.csv`, `GET /api/export/database.json`).
- Unify `web/app.js` + `web/style.css` + `web/index.html`:
  - Navigation tabs: `Dashboard`, `Add CV`, `Pool`, `Vacancies`, `Candidates`, `Review queue`, `Replies`, `Interns`, `Areas`, `AI team`.
  - Unified `#/interns`: `Run Planner` button, Cohort + Specialization + Search filters, Specialization/Institute summary breakdown, sortable & paginated table, detail modal with raw MongoDB JSON, and CSV export.
  - `#/replies` & `#/candidate/{app_no}`: Email Settings & Test modal, live SMTP delivery status badges + `Retry Send`, interactive Onboarding Checkboxes, HR Notes panel, TalentHub returning-intern banner, and Sample Intern MongoDB Enrollment button.

## Non-Goals & Constraints
- Never duplicate intern rows into an SQLite `interns` table.
- Never modify or push to `main`.
- GitHub Actions CI (`ubuntu-latest`, no running `mongod`, no live SMTP server) must pass cleanly via test backends/fakes.
