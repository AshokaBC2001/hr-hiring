# Hosting, MongoDB Accessibility & Supervisor Evaluation Guide

This guide explains how to host **HR Hiring** for supervisor review, configure MongoDB (local or Atlas), safeguard live email demos, and collect structured supervisor feedback.

---

## 1. Pre-Flight Readiness Check (`doctor`)

Run the diagnostic check before exposing the server:

```bash
.venv/bin/python run.py doctor
```

It verifies:
- **SQLite WAL & integrity:** Confirms `PRAGMA journal_mode=WAL` and `PRAGMA quick_check=ok`.
- **TalentHub / MongoDB mode & connectivity:** Displays active mode (`api`, `mongo`, or `auto`), masked URI (credentials stripped), document count, and sample-write status.
- **LLM availability:** Verifies `/health` or `/v1/models` on `HR_MODEL_URL`, API key status, or `HR_ALLOW_RULES_ONLY`.
- **Hosting security warnings:** Alerts if binding to `0.0.0.0` / `HR_PUBLIC_HOSTING=1` without authentication (`HR_AUTH_USER`/`HR_AUTH_PASS`), without `HR_DEMO_EMAIL_LOCK=1` / Demo Recipient Override, or with the default local mock token.

HTTP equivalents when running:
- `GET /api/health` — unauthenticated lightweight readiness probe (returns `200` when SQLite is healthy, `503` otherwise)
- `GET /api/diagnostics` — full diagnostics JSON
- `GET /api/analytics/pipeline` — hiring funnel, AI accuracy/speed (`ok`/`fixed`/`fallback`), vacancy fill rates, and MongoDB cohort summary

---

## 2. Three Ways to Host for Supervisor Feedback

Copy `.env.example` to `.env` before starting (`cp .env.example .env`). Real environment variables always override `.env`.

### Option A — Quick Tunnel from Your Mac (Fastest for Live Demo)
Use this when keeping your local Gemma 3 4B model, local MongoDB (`slt_talent_hub_sample`), and local SMTP demo ready:

1. Enable access control & demo email safety in `.env`:
   ```env
   HR_PUBLIC_HOSTING=1
   HR_AUTH_USER=supervisor
   HR_AUTH_PASS=choose-a-strong-password
   HR_DEMO_EMAIL_LOCK=1
   HR_SMTP_TEST_RECIPIENT=your-or-supervisor-inbox@gmail.com
   ```
2. Start MongoDB, `./start_model.sh`, `./start_talent_hub.sh`, and `./start_app.sh`.
3. Expose port `8000` using Cloudflare Tunnel or ngrok:
   ```bash
   cloudflared tunnel --url http://127.0.0.1:8000
   # or: ngrok http 8000
   ```
4. Share the generated HTTPS URL plus `HR_AUTH_USER` / `HR_AUTH_PASS`. The browser will display a native login dialog without any changes to the web UI.

### Option B — Docker Container + MongoDB Atlas (Recommended for Permanent Hosting)
No second mock API process is needed when using direct MongoDB mode (`TALENT_HUB_MODE=mongo`).

```bash
docker build -t hr-hiring .
docker run -d --name hr-hiring \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  -e HR_PUBLIC_HOSTING=1 \
  -e HR_AUTH_USER=supervisor \
  -e HR_AUTH_PASS=strong-password \
  -e TALENT_HUB_MODE=mongo \
  -e TALENT_HUB_API_URL= \
  -e TALENT_HUB_MONGO_URI="mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/?retryWrites=true&w=majority" \
  -e HR_MODEL_URL="https://api.groq.com/openai" \
  -e HR_MODEL_NAME="llama-3.1-8b-instant" \
  -e HR_MODEL_API_KEY="gsk_..." \
  -e HR_DEMO_EMAIL_LOCK=1 \
  hr-hiring
```

If you host on a lightweight VM **without** an external LLM key, set `HR_ALLOW_RULES_ONLY=1` so all CV screening, area matching, interview question generation, email templates, and morning briefs fall back automatically to deterministic rules and templates.

### Option C — Cloud PaaS (Render / Railway / Fly.io)
1. Point the service to the `Dockerfile`.
2. Render/Railway automatically injects `PORT`; `hiring/config.py` reads `PORT` alongside `HR_APP_PORT`.
3. Mount a persistent volume at `/app/data` so `data/register.db`, uploaded CVs (`data/inbox`), and `data/outbox` persist across deploys.
4. Set Health Check Path to `/api/health`.

---

## 3. MongoDB Accessibility Modes

Set `TALENT_HUB_MODE` according to your environment:

| Mode | When to use | Behavior |
|---|---|---|
| `auto` (default) | Local dev or standard setup | Uses `TALENT_HUB_API_URL` if set; switches automatically to direct MongoDB if `TALENT_HUB_API_URL` is blank (`""`). |
| `api` | Strict TalentHub production REST integration | Queries `GET /api/interns` and `GET /api/interns/{id}` with Bearer auth and a 15-second read cache invalidated on writes. |
| `mongo` | Direct local MongoDB or MongoDB Atlas hosting | Queries PyMongo directly with `$facet` aggregation (`mongo_analytics`), server-side email/name lookup (`find_matching_interns`), and indexes on `Trainee_ID`, `field_of_spec_name`, `Training_EndDate`, `Trainee_Email`, `Trainee_Name`, and `Institute`. |

**Safety guardrails:**
- Enrollment writes (`POST /api/candidates/{app_no}/enroll-intern`) are blocked with `PermissionError` unless `TALENT_HUB_MONGO_URI` points to `127.0.0.1`, `localhost`, or `::1`.
- Connection string passwords are automatically masked as `***` in `/api/diagnostics`, `/api/state`, `/api/health`, and logs.

---

## 4. Supervisor Review Checklist & Feedback Workflow

### Recommended 5-Minute Supervisor Walkthrough
1. **Dashboard & MongoDB Interns (`#/interns`):** Verify 400 Sri Lankan IT intern records (`Trainee_Email`, `Institute`, `ending_soon`, specialization/institute charts).
2. **Planner & Vacancies (`#/vacancies`):** Click **Run Planner** (`POST /api/vacancies/plan`) to open vacancies for interns ending within 7 days and view ranked CV Pool matches. Approve a top match.
3. **CV Pool & Scoring (`#/pool`):** Upload a sample CV or add samples (`#/add`), watch **Recruiter** score all 28 areas automatically, and test inviting or archiving CVs.
4. **Live Email Sender & Demo Override (`#/replies`):** Open **Email Settings**, enable SMTP with **Demo Recipient Override** set to the supervisor's email, send a test email, and release an invitation/acceptance email. Verify `[DEMO]` subject, HTML formatting, and `data/outbox/` archive.
5. **Interviewer & Onboarding (`#/interviews`):** Switch role top-right to **Interviewer**, record `Pass`, switch back to **HR Officer**, check Work ID `1001+`, toggle interactive onboarding checklist items, add HR notes, and inspect CSV/JSON exports.

### Recording & Exporting Supervisor Feedback (API + CLI)
Because the web UI is kept frozen, feedback and evaluation reports are collected cleanly through HTTP endpoints or the CLI:

**Submit feedback via API (`POST /api/feedback`):**
```bash
curl -X POST http://localhost:8000/api/feedback \
  -u supervisor:strong-password \
  -H "Content-Type: application/json" \
  -d '{
    "reviewer": "Dr. Perera",
    "role": "supervisor",
    "category": "agents",
    "rating": 5,
    "target": "Planner",
    "comment": "MongoDB direct vacancy planning works cleanly and email Demo Override prevented test spam."
  }'
```
Valid categories: `general`, `workflow`, `agents`, `scoring`, `email`, `mongodb`, `hosting`, `bug`.

**List, resolve, or export via API:**
- `GET /api/feedback` (supports `?category=` and `?status=open|resolved`)
- `POST /api/feedback/{id}/resolve` (`x-role: hr`, JSON `{"resolution_note": "..."}`)
- `GET /api/export/feedback.csv` — Excel-safe CSV download
- `GET /api/export/supervisor-report.md` — Downloadable Markdown evaluation pack combining hosting readiness, pipeline metrics, AI accuracy/speed stats, MongoDB cohort totals, and supervisor feedback

**Equivalent CLI commands:**
```bash
.venv/bin/python run.py feedback --reviewer "Dr. Perera" --category workflow --rating 5 --comment "Smooth pool flow"
.venv/bin/python run.py feedback
.venv/bin/python run.py report --out supervisor_report.md
```
