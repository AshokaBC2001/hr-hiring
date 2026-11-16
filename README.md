# HR Hiring

A local, AI-assisted hiring workflow. Five AI agents handle the routine work of screening CVs, monitoring intern vacancies, scheduling interviews and drafting emails. Two people make every decision that matters.

| Role | Type | Responsibility |
|---|---|---|
| **Recruiter** | AI agent | Reads each CV, builds a profile checked against the CV, scores the match across areas and places candidates in the CV Pool. |
| **Planner** | AI agent | Monitors active interns in TalentHub (MongoDB), opens vacancies when placements end within 7 days and ranks top CV Pool matches. |
| **Coordinator** | AI agent | Books interview slots, prepares interview questions and drafts emails to candidates. |
| **Onboarding** | AI agent | After a Pass, issues a 4-digit employee ID, drafts the offer email and prepares an interactive onboarding checklist. |
| **Chief of staff** | AI agent | Reviews the day's activity each morning, flags stalled work, archives Pool CVs older than 6 months and writes a short summary. |
| **HR officer** | Person | Adds CVs, invites from the CV Pool, approves vacancy suggestions, manages areas, configures SMTP email and releases every email. |
| **Interviewer** | Person | Runs the interview and records Pass, Fail or Hold. The AI cannot change this result. |

## Features

- Runs fully on your own machine. CVs never leave the computer unless live SMTP delivery is explicitly enabled.
- Local LLM (Gemma 3 4B by default) served by llama.cpp. No cloud API keys needed.
- Scores are calculated by code, not by the model.
- Every model answer is checked against the CV before it is saved.
- Emails are always archived in `data/outbox/`, with optional **Live SMTP delivery** (HTML + plain text, per-draft delivery status, retry, and a safe **Demo Recipient Override**).
- Unified dual database: **SQLite** (`data/register.db`) for hiring pipeline, CV Pool, vacancies, interactive checklists, HR notes and settings; **MongoDB** (`slt_talent_hub_sample.interns`) / TalentHub API as the single source of truth for intern records (no duplicate SQLite intern mirror table), with analytics, returning-intern detection, sample intern enrollment, and CSV/JSON exports.
- Plain web UI with two roles: HR officer and interviewer.

## Tech stack

Python, FastAPI, LangGraph, SQLAlchemy (SQLite), PyMongo (MongoDB), APScheduler, `smtplib` (live SMTP), llama.cpp, and plain HTML, CSS and JavaScript.

## Requirements

- macOS (also works on Linux)
- Python 3.10 or newer
- [llama.cpp](https://github.com/ggml-org/llama.cpp) (`brew install llama.cpp`)
- About 3 GB of free memory for the model

## Installation

```bash
git clone https://github.com/sgautham651-dev/hr-hiring.git
cd hr-hiring
bash setup.sh
```

`setup.sh` creates a `.venv` folder and installs the Python packages.

## Running

Open two terminals in the project folder.

**Terminal 1: start the model server**

```bash
./start_model.sh
```

On the first run this downloads Gemma 3 4B (about 2.5 GB). Wait until the server is listening.

**Terminal 2: start the app**

```bash
./start_app.sh
```

Open **http://localhost:8000** in your browser. Press Control+C in each terminal to stop.

To check that the model returns valid JSON:

```bash
.venv/bin/python run.py check
```

If you see "permission denied", run `bash start_model.sh` and `bash start_app.sh` instead.

## Quick demo

1. Use the **Role** dropdown (top right) to choose **HR Officer**. Go to **Add CV** and click **Load the sample CVs**.
2. Watch the **Dashboard** while the Recruiter screens the CVs, scores them across areas and places qualified CVs into the **CV Pool**.
3. Open **Interns** or **Vacancies** and click **Run Planner** to inspect the 400 MongoDB TalentHub interns, spot placements ending within 7 days and rank CV Pool suggestions. Approve a suggestion or manually invite a candidate from **CV Pool**.
4. Open **Replies**, optionally configure **Email Settings** (including **Demo Recipient Override** to route test sends to your own inbox safely) and release an interview invitation.
5. Switch the role to **Interviewer**, open **Interviews**, pick a candidate and record **Pass**.
6. Switch back to **HR Officer**. Onboarding issues employee ID **1001**, drafts the offer email and creates an interactive onboarding checklist. Release it to close the application (and optionally click **Enroll in MongoDB (Sample)** on closed hires in local sample mode).
7. Click **Write brief now** on the Dashboard to see the daily summary. The **AI team** page shows all five agents, their routines and every step taken.

## How it works

```
HR officer adds a CV
  -> Recruiter:    read CV -> duplicate check -> score all areas -> CV Pool (or HR review if below threshold / unsure area)
  -> Planner:      check TalentHub MongoDB interns -> open vacancy if ending within 7 days -> rank Pool candidates -> HR officer approves
     (or HR officer invites directly from CV Pool)
  -> Coordinator:  interview slot -> questions -> invitation draft    -> HR officer releases (+ optional live SMTP)
  -> Interviewer:  Pass / Fail / Hold
  -> Pass: Onboarding: employee ID -> offer draft -> interactive checklist -> HR officer releases -> Closed (optional MongoDB sample enroll)
     Fail: Coordinator: rejection draft                               -> HR officer releases -> Closed
     Hold: Coordinator: hold note -> HR officer releases -> On hold   -> Interviewer decides later
```

- **The candidate's status is the hand-off.** Each agent runs on its own schedule and picks up candidates in its trigger status.
- **Agents are defined in Markdown** in `employees/`: `ROLE.md` (who it is), `SCHEDULE.md` (when it runs), `RELEASES.md` (what needs a person) and `routines/<name>/SKILL.md` (one task each). The **Instructions for the model** and **Output** sections are the prompt, so you can change behaviour without touching code.
- **Workflows are LangGraph graphs** (`hiring/flows.py`), scheduled with APScheduler (`hiring/scheduler.py`).
- **The model proposes, code decides** (`hiring/checks.py`):
  - profile items, names, emails and evidence must appear in the CV;
  - emails must contain the correct name, area, date and ID, with no invented numbers or placeholders;
  - a failed check is sent back to the model (up to 2 retries), then the agent falls back to rules or a template and flags the candidate;
  - the score is calculated by code: skills 40, projects 30, education 20, experience 10;
  - employee IDs are issued by code only after a Pass, starting at 1001, never reused.
- **Lessons:** when a check fails, the agent saves a short lesson (no personal data) in `data/notes/` and uses it next time.
- **No forced areas:** the model may suggest a new area, but only a person can add one.

## Configuration

| What | Where |
|---|---|
| Areas and required skills | **Areas** page |
| Score weights and thresholds | `hiring/config.py` |
| Agent instructions | `employees/<agent>/ROLE.md` and `routines/<routine>/SKILL.md` |
| Agent schedule | `employees/<agent>/SCHEDULE.md` |
| Model | `start_model.sh`, or set `HR_MODEL_URL`, `HR_MODEL_NAME`, and optional `HR_MODEL_API_KEY` (or `HR_ALLOW_RULES_ONLY=1`) |
| App bind & hosting | `HR_APP_HOST`, `HR_APP_PORT`/`PORT`, `HR_PUBLIC_HOSTING`, `HR_ALLOWED_HOSTS`, `HR_AUTH_USER`/`HR_AUTH_PASS`, `.env` |
| TalentHub API & MongoDB | `TALENT_HUB_MODE` (`auto` / `api` / `mongo`), `TALENT_HUB_*` env vars / `hiring/config.py` |
| Live SMTP email delivery | `HR_SMTP_*` / `HR_DEMO_EMAIL_LOCK` env vars or **Email Settings** in the UI |

To reset all data, stop the app and delete the `data` folder.

## Hosting & Supervisor Evaluation

For hosted deployments (Cloudflare Tunnel / ngrok, Docker Compose, Render, Railway, or MongoDB Atlas) and supervisor feedback workflows, see **[docs/HOSTING_AND_SUPERVISOR_GUIDE.md](docs/HOSTING_AND_SUPERVISOR_GUIDE.md)** and `.env.example`:

- Run `.venv/bin/python run.py doctor` before hosting to check SQLite WAL integrity, MongoDB/TalentHub connectivity, LLM reachability, and security warnings.
- Protect a hosted URL without altering the UI by setting `HR_AUTH_USER`, `HR_AUTH_PASS`, `HR_PUBLIC_HOSTING=1`, and `HR_DEMO_EMAIL_LOCK=1` (with Demo Recipient Override).
- Collect and export supervisor evaluation notes via `POST /api/feedback`, `GET /api/export/supervisor-report.md`, or `.venv/bin/python run.py report`.

## TalentHub Sample MongoDB & Read-Only API

Until access to the live **SLT TalentHub** API is configured, this branch provides a sample MongoDB database (`slt_talent_hub_sample.interns`) matching TalentHub's schema (`Trainee_ID`, `Trainee_Name`, `Training_StartDate`, `Training_EndDate`, `field_of_spec_name`) enriched with Sri Lankan `Institute`, `Trainee_Email` and rolling `ending_soon` cohorts, alongside a local Bearer-authenticated read-only mock API (`http://127.0.0.1:5050/api`, avoiding macOS AirPlay's default port `5000`). **Planner** reads intern placement end dates directly from `hiring/talent_hub.py` rather than mirroring interns in SQLite.

1. Ensure MongoDB is running locally on `127.0.0.1:27017` and seed 400 Sri Lankan IT-focused sample intern records:

```bash
.venv/bin/python scripts/seed_talent_hub.py
```

2. Start the local mock TalentHub server in a separate terminal:

```bash
./start_talent_hub.sh
```

3. Verify from `hr-hiring` CLI or HTTP endpoints (`/api/state`, `/api/interns`, `/api/interns/1042`):

```bash
.venv/bin/python run.py interns
```

To switch to the real TalentHub service later, set `TALENT_HUB_API_URL` (e.g. `https://talenthub.slt.lk/api`) and `TALENT_HUB_API_TOKEN` in the environment. To connect directly to local MongoDB or MongoDB Atlas without running the mock API server, set `TALENT_HUB_MODE=mongo` (or leave `TALENT_HUB_API_URL=` empty) alongside `TALENT_HUB_MONGO_URI`. Optional overrides: `TALENT_HUB_DB_NAME`, `TALENT_HUB_COLLECTION`.

## Project structure

```
setup.sh, start_model.sh, start_app.sh, start_talent_hub.sh   setup and start scripts
Dockerfile, .dockerignore, .env.example  container & hosting configuration
app.py                                   web app entry point (supports HR_APP_HOST, PORT)
run.py                                   CLI helpers: check, samples, once, plan, brief, status, seed-talent-hub, interns, generate-cvs, doctor, feedback, report
employees/                               the 5 AI agents (Markdown)
shared/areas.json                        starter areas
hiring/
  config.py     settings & .env          db.py              database (SQLite WAL + migrations)
  llm.py        pooled LLM client        checks.py          checks on model answers
  mailer.py     live SMTP sender         vacancies.py       MongoDB-backed vacancy planning
  supervisor.py diagnostics & feedback   cv_generator.py    400 unique PDF CV model & loader
  talent_hub.py TalentHub API & MongoDB  talent_hub_mock.py local mock API
  tools.py      CV reading, scoring      employees.py       cached Markdown runner
  flows.py      LangGraph workflows      scheduler.py       APScheduler jobs
  actions.py    user actions             server.py          FastAPI routes, hosting guards & exports
scripts/                                 dataset importers, TalentHub seeder & generate_cv_pool.py
docs/                                    architecture, specs, plans & hosting/supervisor guide
web/                                     front end (HTML, CSS, JavaScript)
```

## Testing

```bash
.venv/bin/python -m pytest tests -q
```

The tests use a scripted fake model (`tests/fake_model.py`), so no real model is needed. To try the UI without a real model, run the fake model in terminal 1 instead of `./start_model.sh`:

```bash
.venv/bin/python tests/fake_model.py --port 8080 --delay 1
```

## Limitations

- Quality and speed depend on the model. The **AI team** page shows how many steps passed, needed a fix or fell back to rules, which helps compare models.
- The checks confirm facts appear in the CV, not that a decision is correct. Borderline and low scores always go to a person.
- Evidence matching is word-based, not semantic.
- Scanned PDFs are not supported (no OCR). They are flagged for a person.
- The role switcher (`HR Officer` / `Interviewer`) controls role actions locally; when hosting externally, enable `HR_AUTH_USER` / `HR_AUTH_PASS` (HTTP Basic Auth) or `HR_ACCESS_TOKEN`.
- One local LLM call runs at a time (`WORK_LOCK`).

## Troubleshooting

| Problem | Fix |
|---|---|
| Top bar shows "Model: not running" | Run `./start_model.sh` in another terminal and wait until it is listening. |
| `llama-server: command not found` | `brew install llama.cpp` |
| Port 8000 or 8080 already in use | Close the other running copy, or change the port. |
| Very slow | Close other apps. The model needs about 3 GB of memory. |
| Many "fell back to rules" steps | Try another model or adjust the SKILL instructions. |

## Acknowledgements

The Markdown agent structure (ROLE, SCHEDULE, RELEASES and SKILL files) follows the idea from [markfulton/ai-employees](https://github.com/markfulton/ai-employees). All people in `samples/` are fictional.
