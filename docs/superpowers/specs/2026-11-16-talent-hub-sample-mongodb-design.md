# Sample SLT TalentHub MongoDB & API Integration Design

**Date:** 2026-11-16
**Size:** Small change
**Target Branch:** `feat/talent-hub-mongodb` (separate from `main`, pushed to GitHub)

## Intent & Why
`hr-hiring` integrates with **TalentHub** (SLT's intern management system). Because live TalentHub credentials/network access are not available yet — and per Sithuli's TalentHub specification, `Trainee_Name` is AES-256 encrypted at rest in TalentHub's MongoDB and decrypted only by TalentHub's read-only HTTP API — we will:
1. Run a local sample MongoDB (`slt_talent_hub_sample`, collection `interns`) matching Sithuli's exact schema and seeded with **100 sample records**.
2. Run a lightweight local mock TalentHub read-only REST API (`http://localhost:5000/api/interns` and `/api/interns/:id`, Bearer token auth) backed by that sample MongoDB.
3. Wire `hr-hiring` to consume the TalentHub REST API (`TALENT_HUB_API_URL` + `TALENT_HUB_API_TOKEN`) so switching to production (`https://talenthub.slt.lk/api`) requires only changing environment variables.

## Goals
- Install and start MongoDB Community locally via Homebrew (`mongodb/brew/mongodb-community` and `mongosh`).
- Initialize database `slt_talent_hub_sample` and collection `interns` with strict `$jsonSchema` validation, indexes (`Trainee_ID` unique, `field_of_spec_name`, `Training_EndDate`), and **100 sample intern records** matching Sithuli's exact field names:
  - `Trainee_ID` (`string`, e.g., `"1001"`..`"1100"`)
  - `Trainee_Name` (`string`)
  - `Training_StartDate` (UTC BSON `date`, ISO-8601 `"YYYY-MM-DDTHH:MM:SS.000Z"`)
  - `Training_EndDate` (UTC BSON `date`, ISO-8601 `"YYYY-MM-DDTHH:MM:SS.000Z"`)
  - `field_of_spec_name` (`string`)
- Build `hiring/talent_hub.py` containing:
  - Reused `MongoClient` singleton with explicit pool/timeout rationale and schema/seeding helpers for the sample `interns` collection.
  - Read-only HTTP client (`httpx`) targeting `TALENT_HUB_API_URL` with `Authorization: Bearer <TALENT_HUB_API_TOKEN>`, health check (`health()`), `list_interns()`, and `get_intern()`, plus `set_backend(...)` test hook.
- Build a local mock TalentHub FastAPI service (`hiring/talent_hub_mock.py` + `start_talent_hub.sh`) implementing Sithuli's exact read-only contract on port `5000` (`GET /api/interns`, `GET /api/interns/{id}` supporting both `Trainee_ID` and MongoDB `_id`, Bearer auth returning `401`, `404` for missing intern, read-only `GET` guarantee).
- Add `GET /api/interns`, `GET /api/interns/{intern_id}`, and `"talent_hub"` health in `GET /api/state` to `hiring/server.py`, degrading gracefully when TalentHub/MongoDB is offline.
- Add `scripts/seed_talent_hub.py` and `run.py` CLI helpers (`seed-talent-hub`, `interns`).
- Keep `pytest` passing offline (via test doubles) and verify live against local MongoDB + mock TalentHub API.

## Non-Goals
- Replacing SQLite (`data/register.db`) for hiring candidates, drafts, runs, areas, or briefs.
- Implementing AES-256 encryption inside the sample MongoDB (unencrypted sample records keep the sample DB easy to inspect and seed; the mock API preserves the exact external REST boundary).
- Changing AI agent workflows (`Recruiter`, `Coordinator`, `Onboarding`, `Chief of staff`) or frontend UI pages yet.

## Anti-Goals
- Hardcoding production credentials or breaking `hr-hiring` startup/tests when MongoDB or TalentHub is offline.
- Modifying or pushing to `main`.

## Constraints
- Local macOS environment without Docker (Homebrew available).
- Dates follow ISO-8601 UTC format `"2025-01-01T00:00:00.000Z"` / `"2026-05-19T00:00:00.000Z"`. Stored as UTC BSON `date` in MongoDB and serialized as `.000Z` UTC strings in API JSON.
- Existing `pytest` suite (`tests/test_core.py`, `tests/test_api.py`) and GitHub Actions CI (`ubuntu-latest`, no `mongod`) must pass cleanly.

## Approaches Considered
1. **Local sample MongoDB + Local mock TalentHub REST API + `hr-hiring` API client (Chosen):** Matches Sithuli's exact MongoDB `interns` schema AND the read-only Bearer-token REST API boundary required because `Trainee_Name` is AES-256 encrypted in production. Allows full local testing and one-env-var switch to production.
2. **Direct `pymongo` from `hr-hiring` only (Not chosen):** Works for unencrypted sample DB, but cannot read decrypted names from the real TalentHub MongoDB in production.
3. **In-memory mock only (Dropped):** Does not provide a real persistent MongoDB database to inspect and populate with 100 records.

## Configuration Defaults
- `TALENT_HUB_MONGO_URI`: `mongodb://127.0.0.1:27017`
- `TALENT_HUB_DB_NAME`: `slt_talent_hub_sample`
- `TALENT_HUB_COLLECTION`: `interns`
- `TALENT_HUB_API_URL`: `http://127.0.0.1:5000/api`
- `TALENT_HUB_API_TOKEN`: `talenthub-local-dev-token`
- `TALENT_HUB_MOCK_PORT`: `5000`
- PyMongo client pool: `maxPoolSize=10`, `minPoolSize=0`, `maxIdleTimeMS=300000`, `connectTimeoutMS=3000`, `serverSelectionTimeoutMS=2000`, `socketTimeoutMS=10000`, `tz_aware=True`.
