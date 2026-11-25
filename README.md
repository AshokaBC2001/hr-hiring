# HR Hiring

A local, AI-assisted hiring workflow. Four AI agents handle the routine work of screening CVs, scheduling interviews and drafting emails. Two people make every decision that matters.

| Role | Type | Responsibility |
|---|---|---|
| **Recruiter** | AI agent | Reads each CV, builds a profile checked against the CV, picks the best area, scores the CV for every area and adds it to the CV pool. |
| **Coordinator** | AI agent | Books interview slots, prepares interview questions and drafts emails to candidates. |
| **Onboarding** | AI agent | After a Pass, issues a 4-digit employee ID, drafts the offer email and prepares an onboarding checklist. |
| **Planner** | AI agent | Reads the intern records, opens a vacancy when places are about to come free, and suggests the strongest waiting CVs for them. |
| **Chief of staff** | AI agent | Reviews the day's activity each morning, archives CVs older than 6 months, flags stalled work and writes a short summary. |
| **HR officer** | Person | Adds CVs (several at once), approves who fills each vacancy, manages areas and releases every email. |
| **Interviewer** | Person | Runs the interview and records Pass, Fail or Hold. The AI cannot change this result. |

## Features

- Runs fully on your own machine. CVs never leave the computer.
- Reads the intern records from Talent Hub (MongoDB) and never writes to them.
- Opens a vacancy when interns are about to finish, and suggests the best waiting CVs for it.
- Local LLM (Gemma 3 4B by default) served by llama.cpp. No cloud API keys needed.
- Scores are calculated by code, not by the model.
- Every model answer is checked against the CV before it is saved.
- Emails are drafted only. Nothing is sent. Released emails are saved to `data/outbox/` as `.eml` files
  (double-click one to open it in Mail or Outlook).
- After a Pass, a placement letter (plain A4 PDF) is made by code and attached to the acceptance email.
- Plain web UI with two roles: HR officer and interviewer.

## Tech stack

Python, FastAPI, LangGraph, SQLAlchemy (SQLite), APScheduler, llama.cpp, pymongo (to read Talent Hub),
fpdf2 (placement letter PDF), and plain HTML, CSS and JavaScript.

## Requirements

- macOS (also works on Linux)
- Python 3.10 or newer
- [llama.cpp](https://github.com/ggml-org/llama.cpp) (`brew install llama.cpp`)
- About 3 GB of free memory for the model

## Installation

```bash
git clone <your-repo-url>
cd <repo-folder>
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

## Mail-only mode

For testing the invitation mail on its own. Start the app with `./start_mail.sh` in terminal 2
(instead of `./start_app.sh`). On Windows, run `set HR_MODE=mail` and then start the app as usual.
The model is still needed, because the Recruiter reads each CV.

The menu then shows only three pages:

| Page | What it does |
|---|---|
| **Add CV** | Upload CVs and type the **Source** (who sent them). The Recruiter reads the name, email, university and area. |
| **Register** | One row per CV, in the same columns as the section's intern sheet, with **Export to Excel**. |
| **Mail settings** | The required attachments (come with the template), extra attachments you add, and the mail template. |

On each candidate page:

1. **Invitation mail:** filled from the template. Pick the **visit date** (the next Tuesday is suggested); it is
   written into the mail as `29-DEC-2026  (Tuesday)`. The subject, the text and the attachments can all be edited.
2. **Preview** shows the mail as the student will see it, with clickable links. **Send mail** hands it over
   for sending; the register gets the mail date and the status "Mail sent".
3. **Register details:** after the student replies, fill NIC / PP, Tel No and Laptop Serial No, choose a status,
   and give the reference number. The next number is suggested from the last one used.

**Sending:** `hiring/mailer.py` is the handover point for Outlook sending. Until it is connected, a sent mail is
saved in `data/outbox/` as an `.eml` file with the attachments inside (double-click it to open it in Mail or Outlook).
With all three standard attachments the mail is about 4.5 MB, which is over the 4 MB limit of a single Microsoft Graph
request, so attachments must be added to a draft one by one (see the note in `hiring/mailer.py`).

The template is `shared/mail/invitation.txt` and the subject and sender are in `shared/mail/mail.json`.
The four required attachments (Hospital_system.pdf, individual user manual.pdf, POS Task Backlog.xlsx and
Trainee_Guidelines_Agreement.pdf) come with the template in `shared/mail/attachments/`. Extra files added on the
Mail settings page, and newer versions of the required files, are kept in `data/mail/`, together with template changes.

## Quick demo

1. Use the **Role** dropdown (top right) to choose **HR Officer**. Go to **Add CV** and click **Load the sample CVs**.
2. The new CVs appear in the **CV pool** under the upload box, marked **New**, while the Recruiter reads them and fills in each area and score. With a local 4B model this can take a minute or more per CV.
3. Choose an area in the CV pool (for example **AI/ML**) to see its ranking: only CVs scoring 50 or more for that area, with a related project, job or studies, are shown. Open the top candidate and click **Invite to interview**. The Coordinator books a slot and drafts the invitation.
4. Open **Interns** and click **Sync now**. The intern records are read, and the Planner opens a vacancy
   for every area where interns are about to finish.
5. Open **Vacancies**. Each one lists the best waiting CVs for that area, plus 2 reserves. Click **Approve**
   on one: the candidate leaves the pool, and the Coordinator books a slot and drafts the invitation.
6. Open **Replies** and release the interview invitation.
7. Switch the role to **Interviewer**, open **Interviews**, pick a candidate and record **Pass**.
8. Switch back to **HR Officer**. Onboarding issues employee ID **1001**, drafts the offer email and makes the
   placement letter. The dates are set automatically: the Monday after the interview, for six months. Open the
   candidate, click **Preview** in the **Placement letter** box (change the dates there if needed), then release the
   offer email (with the letter attached) to close the application.
   The vacancy now shows one place filled. If you record **Fail** instead, the next best CV from the pool is suggested in its place.
9. Click **Write brief now** on the Dashboard to see the daily summary. The **AI team** page shows each agent, its routines and every step it took.

## How it works

```
HR officer adds CVs (several at once)
  -> Recruiter:    read CV -> duplicate check -> choose best area -> score for every area -> skill gaps -> add to pool
       "Waiting in pool" (best area and score) | "No matching area" (read again when areas change)
  -> CV pool:      on the Add CV page; ranked per area (50 or more), 3 other good areas per CV,
                   archived after 6 months

Talent Hub (read only)
  -> Planner:      read intern records -> interns finishing within 7 days -> a vacancy for their area
                   -> suggest the top CVs from the pool (places + 2 reserves)
                   -> each CV only for its best area, or a second area within 10 points
  -> HR officer:   Approve a suggestion (or Invite from the pool directly) -> Shortlisted
  -> Coordinator:  interview slot -> questions -> invitation draft    -> HR officer releases
  -> Interviewer:  Pass / Fail / Hold
  -> Pass: Onboarding: employee ID -> offer draft -> checklist        -> HR officer releases -> Closed
     Fail: Coordinator: rejection draft                               -> HR officer releases -> Closed
     Hold: Coordinator: hold note -> HR officer releases -> On hold   -> Interviewer decides later

  Pass -> one place of the vacancy is filled; when every place is filled the vacancy closes.
  Fail or dropped out -> the next best CV from the pool is suggested in its place.
```

- **The candidate's status is the hand-off.** Each agent runs on its own schedule and picks up candidates in its trigger status.
- **Agents are defined in Markdown** in `employees/`: `ROLE.md` (who it is), `SCHEDULE.md` (when it runs), `RELEASES.md` (what needs a person) and `routines/<name>/SKILL.md` (one task each). The **Instructions for the model** and **Output** sections are the prompt, so you can change behaviour without touching code.
- **Workflows are LangGraph graphs** (`hiring/flows.py`), scheduled with APScheduler (`hiring/scheduler.py`).
- **The model proposes, code decides** (`hiring/checks.py`):
  - profile items, names, emails and evidence must appear in the CV;
  - emails must contain the correct name, area, date and ID, with no invented numbers or placeholders;
  - a failed check is sent back to the model (up to 2 retries), then the agent falls back to rules or a template and flags the candidate;
  - the score is calculated by code: skills 40, projects 30, education 20, experience 10;
  - education gets full points only for the area's own related studies (for example a Computer
    Science degree for AI/ML, a Human Resource Management degree for Human Resources);
  - a CV is suggested for an area only with real evidence for it (a related project, job or
    studies), not just a skills list, and only for its best area or a second area within 10 points;
  - employee IDs are issued by code only after a Pass, starting at 1001, never reused;
  - the placement letter is filled in by code from a template, never written by the model.
- **Lessons:** when a check fails, the agent saves a short lesson (no personal data) in `data/notes/` and uses it next time.
- **No forced areas:** the model may suggest a new area, but only a person can add one.

## Configuration

| What | Where |
|---|---|
| Areas, required skills and related studies | **Areas** page |
| Which areas a CV is suggested for | `POOL_MIN_SCORE`, `POOL_CLOSE_POINTS`, `POOL_SUGGEST_AREAS`, `SHARED_SKILL_AREAS` in `hiring/config.py` |
| Talent Hub: source, address and field names | `TALENTHUB_*` in `hiring/config.py` |
| Days of notice before a place opens, and reserves | `VACANCY_NOTICE_DAYS`, `VACANCY_RESERVES` in `hiring/config.py` |
| Score weights and thresholds | `hiring/config.py` |
| Placement letter wording | `employees/onboarding/templates/placement-letter.md` |
| Letter department and sender | `shared/letter/letter.json` |
| Placement length (6 months) | `PLACEMENT_MONTHS` in `hiring/letters.py` |
| Agent instructions | `employees/<agent>/ROLE.md` and `routines/<routine>/SKILL.md` |
| Agent schedule | `employees/<agent>/SCHEDULE.md` |
| Model | The model name in `start_model.sh` (any GGUF model on Hugging Face, for example a Qwen3 4B Instruct build) |
| App port | `APP_PORT` in `hiring/config.py` |

To reset all data, stop the app and delete the `data` folder.

## Project structure

```
setup.sh, start_model.sh, start_app.sh   setup and start scripts
start_mail.sh                            starts the app in mail-only mode
app.py                                   web app entry point
run.py                                   CLI helpers: check, samples, once, brief, status
employees/                               the 5 AI agents (Markdown)
shared/areas.json                        starter areas
shared/example_interns.json              made-up intern records (until Talent Hub access is granted)
shared/letter/                           letter settings and the font (DejaVu Sans)
shared/mail/                             invitation mail template and mail settings (mail-only mode)
hiring/
  config.py     settings               db.py         database (SQLite)
  llm.py        llama-server client    checks.py     checks on model answers
  talenthub.py  reads Talent Hub       vacancies.py  vacancies and suggestions
  letters.py    placement letter PDF and the .eml email files
  invitation.py invitation mail, attachments and the intern register (mail-only mode)
  mailer.py     sends a finished mail: the handover point for Outlook
  tools.py      CV reading, scoring, scheduling, templates
  employees.py  loads agent folders, checked model calls
  flows.py      LangGraph workflows    scheduler.py  APScheduler jobs
  actions.py    user actions           server.py     FastAPI routes
web/                                     front end (HTML, CSS, JavaScript)
samples/                                 sample CVs (fictional people)
tests/                                   tests and a fake model server
data/                                    created at runtime: database, inbox, outbox, letters, mail, logs, notes
```

## Testing

```bash
.venv/bin/python -m pytest tests -q
```

The tests use a scripted fake model (`tests/fake_model.py`), so no real model is needed.
`tests/test_mail.py` covers mail-only mode. `tests/ui_check_mail.py` clicks through it in a browser.

To refresh the made-up intern records so the demo dates are near today:

```bash
.venv/bin/python tests/make_interns.py
``` To try the UI without a real model, run the fake model in terminal 1 instead of `./start_model.sh`:

```bash
.venv/bin/python tests/fake_model.py --port 8080 --delay 1
```

## Limitations

- Quality and speed depend on the model. The **AI team** page shows how many steps passed, needed a fix or fell back to rules, which helps compare models.
- The checks confirm facts appear in the CV, not that a decision is correct. Borderline and low scores always go to a person.
- Evidence matching is word-based, not semantic.
- Scanned PDFs are not supported (no OCR). They are flagged for a person.
- No authentication. The role switch is for demo use only.
- Talent Hub access is not available yet, so the intern records are made up. They use the same five fields
  and the same date format, so switching to the real database is a change to `TALENTHUB_*` in `hiring/config.py`.
- The intern records hold no "resigned" field, so an intern who leaves early is not visible. The HR officer
  can open a vacancy by hand for that.
- One leaving intern is treated as one place. There is no "how many places does this area have" field.
- One model call at a time.
- The placement letter is a plain A4 page without a logo or signature line. The font prints Latin letters with accents,
  Greek and Cyrillic; names written in Sinhala or Tamil script need another font and text shaping.

## Troubleshooting

| Problem | Fix |
|---|---|
| Top bar shows "Model: not running" | Run `./start_model.sh` in another terminal and wait until it is listening. |
| `llama-server: command not found` | `brew install llama.cpp` |
| Port 8000 or 8080 already in use | Close the other running copy, or change the port. |
| Very slow | Close other apps. The model needs about 3 GB of memory. |
| Many "fell back to rules" steps | Try another model or adjust the SKILL instructions. |
| No vacancies after Sync now | Nobody is finishing within 7 days. Run `python tests/make_interns.py` to refresh the made-up dates. |
| "Talent Hub could not be read" | With the example records, check that `shared/example_interns.json` exists. With the real database, check `TALENTHUB_URL` and that `pymongo` is installed. |

## Acknowledgements

- The Markdown agent structure (ROLE, SCHEDULE, RELEASES and SKILL files) follows the idea from [markfulton/ai-employees](https://github.com/markfulton/ai-employees).
- The placement letter feature follows the approach of [faizanfirdousi/appointment-letter-automation](https://github.com/faizanfirdousi/appointment-letter-automation) (MIT license): a letter template and an email template, with the letter PDF attached to the email.
- The PDF is made with [fpdf2](https://github.com/py-pdf/fpdf2) (LGPL-3.0), installed as a library.
- The letter font is DejaVu Sans (Bitstream Vera license, see `shared/letter/fonts/LICENSE.txt`).
- All people in `samples/` are fictional.
