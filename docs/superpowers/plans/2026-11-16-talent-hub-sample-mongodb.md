# Sample SLT TalentHub MongoDB & API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Set up a local sample MongoDB (`slt_talent_hub_sample.interns`) with 100 intern records matching Sithuli's exact TalentHub schema, a local read-only mock TalentHub REST server (`http://127.0.0.1:5000/api`), and `hr-hiring` HTTP integration on branch `feat/talent-hub-mongodb` pushed to GitHub.

**Architecture:** Install `mongodb-community` + `mongosh` via Homebrew and `pymongo` in Python. Implement `hiring/talent_hub.py` with both the MongoDB sample repository (used by the mock API and seeder) and the read-only Bearer-token HTTP client (used by `hr-hiring`, mirroring `hiring/llm.py`'s adapter + `set_backend` pattern). Implement `hiring/talent_hub_mock.py` (FastAPI read-only mock on port 5000) and wire `/api/state` and `/api/interns` routes in `hiring/server.py`.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, PyMongo (`>=4.6,<5`), httpx, MongoDB Community (`mongodb/brew/mongodb-community`, `mongosh`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-talent-hub-sample-mongodb-design.md`

## Global Constraints

- Local macOS environment without Docker (Homebrew available).
- Dates follow ISO-8601 UTC format `"2025-01-01T00:00:00.000Z"` / `"2026-05-19T00:00:00.000Z"`. Stored as UTC BSON `date` in MongoDB and serialized as `.000Z` UTC strings in API JSON.
- Existing `pytest` suite (`tests/test_core.py`, `tests/test_api.py`) and GitHub Actions CI (`ubuntu-latest`, no `mongod`) must pass cleanly.
- Never replace SQLite (`data/register.db`).
- Never hardcode production credentials or force `hr-hiring` startup/tests to crash when MongoDB or TalentHub is offline.
- Keep all work on branch `feat/talent-hub-mongodb` (never touch `main`).

## Review Focus

- **TalentHub API or MongoDB offline:** `talent_hub.health()` fails fast returning `{"ok": False, ...}`, `hr-hiring` startup and `/api/state` remain `200 OK`, and `/api/interns` returns HTTP `503` with a clear `detail` message.
- **Auth & Read-Only Contract on Mock TalentHub API:** Missing/wrong `Authorization: Bearer <token>` returns `401`, non-`GET` methods (`POST`, `PUT`, `PATCH`, `DELETE`) are rejected (`405`), and `GET /api/interns/:id` matches either `Trainee_ID` or valid MongoDB `ObjectId` `_id`, returning `404` when not found (never `500` on non-ObjectId `:id`).
- **Date format & validation:** `Training_StartDate` and `Training_EndDate` accept ISO `"Z"` strings or `datetime`, store as UTC BSON `date`, serialize as `"YYYY-MM-DDTHH:MM:SS.000Z"`, and reject invalid dates or `Training_EndDate <= Training_StartDate`.
- **Idempotent 100-record seeding:** Re-running `seed_sample_interns(100)` preserves strict `$jsonSchema` validation and unique index on `Trainee_ID`, keeping 100 records (`"1001"`..`"1100"`) without duplicates.
- **CI / offline `pytest` execution:** `tests/fake_talent_hub.py` isolates all unit and API tests from requiring a running `mongod` or port `5000` process, while an optional live test verifies local MongoDB only when reachable.

---

### Task 1: Homebrew MongoDB Setup, `pymongo` Dependency & Baseline Commit

**Files:**
- Modify: `requirements.txt`
- Spec & Plan: `docs/superpowers/specs/2026-11-16-talent-hub-sample-mongodb-design.md`, `docs/superpowers/plans/2026-11-16-talent-hub-sample-mongodb.md`

**Interfaces:**
- Produces: Running `mongod` (`127.0.0.1:27017`), `.venv` with `pymongo`, baseline commit on `feat/talent-hub-mongodb`.

- [ ] **Step 1: Install and start MongoDB Community locally via Homebrew**

```bash
brew tap mongodb/brew
brew install mongodb-community
brew services start mongodb-community
mongosh --quiet --eval "db.runCommand({ ping: 1 })"
```
Expected: `{ ok: 1 }`.

- [ ] **Step 2: Add `pymongo>=4.6,<5` to `requirements.txt` and set up `.venv`**

Run `bash setup.sh` and `.venv/bin/python -m pytest tests -v`.
Expected: All existing tests pass (`0` failures).

- [ ] **Step 3: Commit baseline spec, plan, and `requirements.txt`**

```bash
git add docs/superpowers/specs/2026-11-16-talent-hub-sample-mongodb-design.md docs/superpowers/plans/2026-11-16-talent-hub-sample-mongodb.md requirements.txt
git commit -m "docs & deps: add TalentHub spec, plan, and pymongo dependency"
```

---

### Task 2: TalentHub Config, MongoDB Repository & HTTP Client (`hiring/talent_hub.py`)

**Files:**
- Modify: `hiring/config.py:60-89`
- Create: `hiring/talent_hub.py`
- Create: `tests/fake_talent_hub.py`
- Create: `tests/test_talent_hub.py`

**Interfaces:**
- Consumes: `hiring.config` (`TALENT_HUB_MONGO_URI`, `TALENT_HUB_DB_NAME`, `TALENT_HUB_COLLECTION`, `TALENT_HUB_TIMEOUT_MS`, `TALENT_HUB_API_URL`, `TALENT_HUB_API_TOKEN`)
- Produces in `hiring/talent_hub.py`:
  - `TalentHubUnavailable(Exception)`
  - `TalentHubAuthError(Exception)`
  - `set_backend(backend: Any | None) -> None`
  - `set_mongo_collection(coll: Any | None) -> None`
  - `close() -> None`
  - `parse_utc_date(value: str | datetime) -> datetime`
  - `format_utc_date(value: str | datetime) -> str`
  - `normalize_intern(doc: dict) -> dict`
  - `to_storage_doc(record: dict) -> dict`
  - `ensure_collection() -> dict`
  - `mongo_list_interns(specialization: str = "", q: str = "", limit: int = 200) -> list[dict]`
  - `mongo_get_intern(identifier: str) -> dict | None`
  - `upsert_interns(records: list[dict]) -> dict`
  - `health(force: bool = False) -> dict`
  - `list_interns(specialization: str = "", q: str = "", limit: int = 200) -> list[dict]`
  - `get_intern(identifier: str) -> dict | None`

- [ ] **Step 1: Write failing tests in `tests/fake_talent_hub.py` and `tests/test_talent_hub.py`**

Test date round-trip (`"2025-01-01T00:00:00.000Z"` / `"2026-05-19T00:00:00.000Z"`), invalid dates and `Training_EndDate <= Training_StartDate` rejection, Sithuli's exact field mapping (`_id`, `Trainee_ID`, `Trainee_Name`, `Training_StartDate`, `Training_EndDate`, `field_of_spec_name`, plus optional fields like `Trainee_Email` and `Institute`), `ensure_collection` + `upsert_interns` + `mongo_get_intern` (by `Trainee_ID` and `_id`), HTTP client `list_interns`/`get_intern` auth error + unreachable error handling, and `health()` graceful failure when unreachable.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_talent_hub.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'hiring.talent_hub'`).

- [ ] **Step 3: Add TalentHub settings to `hiring/config.py` and implement `hiring/talent_hub.py`**

- Add `TALENT_HUB_*` config constants in `hiring/config.py` (`TALENT_HUB_MONGO_URI`, `TALENT_HUB_DB_NAME`, `TALENT_HUB_COLLECTION`, `TALENT_HUB_TIMEOUT_MS`, `TALENT_HUB_API_URL`, `TALENT_HUB_API_TOKEN`, `TALENT_HUB_MOCK_HOST`, `TALENT_HUB_MOCK_PORT`).
- Implement `hiring/talent_hub.py` with documented PyMongo pool settings (`maxPoolSize=10`, `minPoolSize=0`, `maxIdleTimeMS=300000`, `connectTimeoutMS=3000`, `serverSelectionTimeoutMS=2000`, `socketTimeoutMS=10000`, `tz_aware=True`), strict `$jsonSchema` validator on `interns` (requiring `Trainee_ID`, `Trainee_Name`, `Training_StartDate`, `Training_EndDate`, `field_of_spec_name`, allowing optional fields), indexes (`Trainee_ID` unique, `field_of_spec_name`, `Training_EndDate`), and Bearer-token `httpx` client calls to `GET /interns` and `GET /interns/{id}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_talent_hub.py -v`
Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add hiring/config.py hiring/talent_hub.py tests/fake_talent_hub.py tests/test_talent_hub.py
git commit -m "feat(db): add TalentHub MongoDB repository and read-only HTTP client"
```

---

### Task 3: 100-Record Sample Generator, Idempotent Seeder & Live MongoDB Seeding

**Files:**
- Modify: `hiring/talent_hub.py`
- Create: `scripts/seed_talent_hub.py`
- Modify: `run.py`
- Modify: `tests/test_talent_hub.py`

**Interfaces:**
- Consumes: `hiring.talent_hub` (`ensure_collection`, `upsert_interns`, `mongo_list_interns`), `config.AREAS_FILE`
- Produces:
  - `generate_sample_interns(count: int = 100) -> list[dict]`
  - `seed_sample_interns(count: int = 100, reset: bool = False) -> dict`
  - `scripts/seed_talent_hub.py` CLI (`--count`, `--reset`)
  - `run.py` subcommands: `seed-talent-hub` and `interns`

- [ ] **Step 1: Write failing tests for 100-record generation and idempotent seeding**

Verify `generate_sample_interns(100)` generates 100 unique `Trainee_ID` values (`"1001"`..`"1100"`), realistic names, UTC ISO dates ending in `T00:00:00.000Z` with `Training_EndDate > Training_StartDate`, and valid specializations; verify `seed_sample_interns(100)` is idempotent on re-run (remains 100).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_talent_hub.py -k sample_interns -v`
Expected: FAIL (`AttributeError: module 'hiring.talent_hub' has no attribute 'generate_sample_interns'`).

- [ ] **Step 3: Implement generator, seeder, `scripts/seed_talent_hub.py`, and `run.py` commands**

- [ ] **Step 4: Run unit tests and seed live local MongoDB**

Run:
```bash
.venv/bin/python -m pytest tests/test_talent_hub.py -v
.venv/bin/python scripts/seed_talent_hub.py
mongosh --quiet slt_talent_hub_sample --eval "print('count:', db.interns.countDocuments()); printjson(db.interns.findOne({ Trainee_ID: '1042' })); printjson(db.interns.getIndexes().map(i => i.name));"
```
Expected: Tests PASS, `count: 100`, `Trainee_ID: "1042"` printed with `ISODate("...T00:00:00.000Z")`, indexes include `Trainee_ID_1`, `field_of_spec_name_1`, `Training_EndDate_1`.

- [ ] **Step 5: Commit Task 3**

```bash
git add hiring/talent_hub.py scripts/seed_talent_hub.py run.py tests/test_talent_hub.py
git commit -m "feat(db): add 100-record TalentHub interns seeder and CLI helpers"
```

---

### Task 4: Mock TalentHub REST Server & `hr-hiring` API Endpoints

**Files:**
- Create: `hiring/talent_hub_mock.py`
- Create: `start_talent_hub.sh`
- Modify: `hiring/server.py`
- Modify: `tests/test_talent_hub.py`

**Interfaces:**
- Consumes: `hiring.talent_hub` (`mongo_list_interns`, `mongo_get_intern`, `list_interns`, `get_intern`, `health`, `close`, `TalentHubUnavailable`, `TalentHubAuthError`)
- Produces:
  - `hiring/talent_hub_mock.py` (`app` exposing `GET /api/interns` and `GET /api/interns/{identifier}` with Bearer token check)
  - `start_talent_hub.sh` (launches mock server on `127.0.0.1:5000`)
  - `hiring/server.py`: `"talent_hub"` in `GET /api/state`, `GET /api/interns`, `GET /api/interns/{intern_id}`, `talent_hub.close()` in lifespan shutdown

- [ ] **Step 1: Write failing API tests for both the mock TalentHub server and `hr-hiring` endpoints**

Test:
- Mock server `GET /api/interns` without token -> `401`; with wrong token -> `401`; with valid Bearer token -> `200` JSON list of interns with exact Sithuli fields (`_id`, `Trainee_ID`, `Trainee_Name`, `Training_StartDate`, `Training_EndDate`, `field_of_spec_name`).
- Mock server `GET /api/interns/1042` -> `200`; `GET /api/interns/<object_id>` -> `200`; `GET /api/interns/9999` -> `404`; `POST /api/interns` -> `405`.
- `hr-hiring` `GET /api/state` includes `"talent_hub"` whether TalentHub is up or offline.
- `hr-hiring` `GET /api/interns` (with `specialization`, `q`, `limit`) and `GET /api/interns/{id}` work when connected to mock server via `TestClient` transport, and return `503` cleanly when TalentHub is unreachable (and `502` on auth error).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_talent_hub.py -k api -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'hiring.talent_hub_mock'` / `404`).

- [ ] **Step 3: Implement `hiring/talent_hub_mock.py`, `start_talent_hub.sh`, and update `hiring/server.py`**

- [ ] **Step 4: Run full test suite**

Run: `.venv/bin/python -m pytest tests -v`
Expected: All unit & API tests across `tests/test_core.py`, `tests/test_api.py`, and `tests/test_talent_hub.py` pass with 0 failures.

- [ ] **Step 5: Commit Task 4**

```bash
git add hiring/talent_hub_mock.py start_talent_hub.sh hiring/server.py tests/test_talent_hub.py
git commit -m "feat(api): add local mock TalentHub REST server and connect hr-hiring"
```

---

### Task 5: Documentation, Live End-to-End Verification & GitHub Push

**Files:**
- Modify: `README.md`
- Modify: `tests/test_talent_hub.py` (live MongoDB check when `mongod` is reachable)

- [ ] **Step 1: Document TalentHub sample DB, mock server, and env vars in `README.md`**

- [ ] **Step 2: Verify end-to-end against live MongoDB + mock TalentHub server + `hr-hiring`**

Run full `pytest`, start `talent_hub_mock` temporarily to test live HTTP `GET http://127.0.0.1:5000/api/interns/1042` with Bearer token and `hr-hiring` `run.py interns`, verify `$jsonSchema` rejects bad writes in `mongosh`, and verify 100 records exist.

- [ ] **Step 3: Commit docs/live test, run whole-branch review, fix any Critical/Important findings, and push `feat/talent-hub-mongodb` to `origin`**
