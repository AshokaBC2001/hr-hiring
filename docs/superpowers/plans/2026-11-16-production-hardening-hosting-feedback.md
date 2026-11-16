# Production Hardening, Efficiency, Agents, MongoDB Accessibility, Hosting & Supervisor Feedback Plan

**Goal:** Resolve all logic/concurrency bugs, optimize scoring/SQLite/HTTP/MongoDB performance, upgrade all 5 AI agents, add direct MongoDB accessibility, make the app cloud/tunnel hostable with optional auth and remote LLM key support, add supervisor feedback/diagnostics/reporting APIs & CLI, update documentation, and expand tests — without modifying `web/`.

**Spec:** `docs/superpowers/specs/2026-11-16-production-hardening-hosting-feedback-design.md`

---

### Task 1: Config, Hosting Settings, LLM Pooling & Auth Key Support
- Modify `hiring/config.py`: add stdlib `.env` loader (`_load_dotenv`), `APP_HOST` (`HR_APP_HOST`), `APP_PORT` (`HR_APP_PORT` / `PORT`), `ALLOWED_HOSTS`, `ALLOWED_ORIGINS`, `AUTH_USER`, `AUTH_PASS`, `ACCESS_TOKEN`, `DEMO_EMAIL_LOCK`, `MODEL_API_KEY`, `ALLOW_RULES_ONLY`, `TALENT_HUB_MODE`.
- Modify `hiring/llm.py`: persistent thread-safe `httpx.Client`, `Authorization: Bearer` header when `MODEL_API_KEY` set, `health()` fallback from `/health` to `/v1/models`, `close()`, optional `ALLOW_RULES_ONLY` degraded behavior.

### Task 2: CV Parsing, Scoring Efficiency, Aliases & Deterministic Checks
- Modify `hiring/checks.py`: fix empty `phone="+"` digit bug (`len(digits) >= 7`), expand `ALIASES` (including `"software engineering": "Backend Development"`, `"qa"`, `"frontend development"`, `"full stack"`, etc.), keep all grounding checks strict.
- Modify `hiring/tools.py`: strip PDF ligatures (`ﬁ`, `ﬂ`), broaden phone regex for `+94 (77)` style numbers, pre-tokenize `CVText` once per CV and once per project/experience item in `score_cv()`/`score_all_areas()`/`best_area_by_rules()`, add clean safe skill synonyms (`_SKILL_SYNONYMS`).

### Task 3: SQLite Concurrency, WAL Mode, Indexes & SupervisorFeedback Table
- Modify `hiring/db.py`: enable WAL + synchronous=NORMAL + busy_timeout=5000 + foreign_keys=ON via SQLAlchemy `connect` event; add `SupervisorFeedback` model (`id`, `reviewer`, `role`, `category`, `rating`, `target`, `comment`, `status`, `resolution_note`, `created_at`, `resolved_at`); add missing indexes (`drafts(status)`, `runs(started_at)`, `events(at)`, `candidates(email)`) in model & `_add_missing_columns()`.

### Task 4: MongoDB Direct Mode, Indexes, Aggregation Analytics & Safe Masking
- Modify `hiring/talent_hub.py`: add `_mask_uri()`, additional indexes (`Trainee_Email_1`, `Trainee_Name_1`, `Institute_1`), `resolve_mode()`, direct `mongo` support in `list_interns`, `get_intern`, `health`, `find_matching_interns`, server-side `mongo_analytics()` `$facet` pipeline, persistent HTTP client with short TTL cache (15s, invalidated on upsert/close).
- Modify `hiring/talent_hub_mock.py`: accept optional `?specialization`, `?q`, `?limit` params cleanly and add `GET /health`.

### Task 5: Vacancy Bug Fixes, Coalesced Concurrency, Agent Prompt & Lesson Upgrades
- Modify `hiring/vacancies.py`: distinguish auto-closed (`"no one is finishing now"`) from manually closed/filled so extended end dates reopen properly when due; re-check `filled >= places`; preload `pool_rows` to eliminate N*M DB scans; update `on_result` edge cases.
- Modify `hiring/employees.py`: mtime-cached Markdown reads; strip non-LLM metadata headers; expand `LESSON_RULES`; allow optional `ALLOW_RULES_ONLY`.
- Modify `hiring/flows.py`: replace racy `_kick()` with coalesced `kick_background()`; compact `AREAS` JSON in `r_choose_area`; track `emails_failed_delivery` / unmatched open vacancies in Chief of Staff metrics & template brief; log `Vacancy` status updates cleanly.
- Update Markdown prompt wording in `employees/` (`coordinator/RELEASES.md`, `recruiter/ROLE.md`, `planner/*`, `chief-of-staff/*`) for accuracy with SMTP & MongoDB direct access.

### Task 6: Actions Cleanup, Supervisor Feedback, Hosting Middleware, Pipeline Analytics & Exports
- Modify `hiring/actions.py`: reuse `_profile()`, remove unused tuple in `_draft_facts()`, make `approve_suggestion` clean & race-safe, validate `opens_on` ISO date in `add_vacancy()`, support duplicate-email check in `set_email()`, enforce `DEMO_EMAIL_LOCK` in `release_draft()`/`resend_draft()`/`mailer`, add `submit_feedback()`, `list_feedback()`, `resolve_feedback()`, `pipeline_analytics()`, `diagnostics()`, `supervisor_report_markdown()`.
- Modify `hiring/mailer.py`: add `_demo_lock_error()` guard when `config.DEMO_EMAIL_LOCK` is enabled without `test_recipient`, optimize connection timeout handling.
- Modify `hiring/server.py`: dynamic `TrustedHostMiddleware` via `config.ALLOWED_HOSTS`, proxy-aware `same_site_only` Origin check, optional HTTP Basic Auth / Bearer token gate (leaving `/api/health` open), remove N+1 in `_vacancy_json` & full `cv_text` read in `/api/state`, call `actions.rescore_pool()` + `vacancies.suggest_for_open()` on area save, wire `/api/health`, `/api/diagnostics`, `/api/analytics/pipeline`, `/api/feedback`, `/api/feedback/{id}/resolve`, `/api/export/feedback.csv`, `/api/export/feedback.md`, `/api/export/supervisor-report.md`.
- Modify `app.py` and `run.py`: support hosted mode info + `doctor`, `feedback`, `report` subcommands.

### Task 7: Packaging & Comprehensive Documentation
- Create `Dockerfile`, `.dockerignore`, `.env.example`.
- Update `ARCHITECTURE.md`, `README.md`, and create `docs/HOSTING_AND_SUPERVISOR_GUIDE.md`.

### Task 8: Comprehensive Testing & Live Verification
- Expand `tests/test_core.py`, `tests/test_api.py`, `tests/test_mailer.py`, `tests/test_talent_hub.py`, and add `tests/test_hosting_and_feedback.py` covering logic fixes, benchmarks/caching, WAL & concurrency, MongoDB direct mode & aggregation, hosting auth/proxy/CSP/CSRF, demo email lock, supervisor feedback, diagnostics, and CLI commands.
- Run full `pytest tests -v`, verify `web/` has zero git changes (`git diff web/`), re-seed & restart local daemons, and verify live HTTP + CLI behavior.
