# HR Hiring — System Audit, Efficiency, Agent, MongoDB Accessibility, Hosting & Supervisor Feedback Design

**Branch:** `feat/talent-hub-mongodb` (keeping `main` untouched; leaving `web/` UI files `index.html`, `style.css`, `app.js` completely untouched).

## 1. Goals
1. **Correctness & Cleanups:** Fix logic bugs across vacancy lifecycle, duplicate detection, email/phone extraction, and concurrency (`_kick()` race, atomic suggestion approval); remove redundant helpers and dead/wasteful loops.
2. **Efficiency:** Eliminate O(N * M * K) `CVText` rebuilds in deterministic scoring (~2,500x -> 1x per project/experience), eliminate N+1 SQLite queries on `/api/vacancies` and `/api/state`, remove full-table `cv_text` loads, enable SQLite WAL + foreign keys + indexes, add persistent `httpx.Client` connection pooling for LLM & TalentHub HTTP calls, and cache parsed Markdown agent specs by mtime.
3. **Agent & Scoring Upgrades:** Fix unmapped `Software Engineering` interns (56 of 400 in MongoDB) and common skill synonyms (`Excel`/`Microsoft Excel`, `CI/CD`, `UI/UX`, `RESTful API`), support PDF ligature cleanup (`ﬁ`, `ﬂ`), slim down 4B LLM prompts (`AREAS` token reduction), expand `LESSON_RULES`, and enrich `Chief of staff` & `Planner` awareness (failed SMTP deliveries, zero-candidate urgent vacancies, returning-intern visibility).
4. **MongoDB Accessibility:** Support `TALENT_HUB_MODE` (`api`, `mongo`, `auto`), add indexes (`Trainee_Email`, `Trainee_Name`, `Institute`), MongoDB server-side `$facet` analytics (`mongo_analytics`), server-side identity lookup (`find_matching_interns`) with short TTL HTTP cache so candidate detail views never download 400 records per click, mask MongoDB credentials in URI diagnostics, and add optional query filters + `/health` on `talent_hub_mock.py`.
5. **Hosting & Supervisor Feedback Readiness (Zero UI Changes):**
   - Env-configurable `HR_APP_HOST` / `HR_APP_PORT` / `PORT`, `HR_ALLOWED_HOSTS`, proxy-aware `Origin`/`Host`/`X-Forwarded-Host` validation, optional HTTP Basic Auth + Bearer token (`HR_AUTH_USER` / `HR_AUTH_PASS` / `HR_ACCESS_TOKEN`), optional `HR_DEMO_EMAIL_LOCK`, and remote OpenAI-compatible `HR_MODEL_API_KEY` + optional `HR_ALLOW_RULES_ONLY`.
   - New `/api/health`, `/api/diagnostics`, `/api/analytics/pipeline`, `/api/feedback` (+ resolve, `.csv`, `.md`), `/api/export/supervisor-report.md`, and CLI commands `doctor`, `feedback`, `report`.
   - Packaging & Docs: `.env.example`, `Dockerfile`, `.dockerignore`, updated `README.md`, `ARCHITECTURE.md`, and `docs/HOSTING_AND_SUPERVISOR_GUIDE.md`.

## 2. Constraints & Non-Goals
- Do **not** modify `web/index.html`, `web/style.css`, or `web/app.js`.
- Do **not** introduce a duplicate SQLite `Intern` mirror table; MongoDB / TalentHub remains the single intern store.
- Preserve existing localhost test contracts (`FakeModel`, `FakeInternCollection`, HTTP 503 on explicit unreachable API URL, HTTP 400/403 on untrusted hosts/origins).
