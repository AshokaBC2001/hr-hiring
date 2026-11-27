# SLT HR Hiring

A local, AI-assisted intern hiring workflow built for **SLT (Sri Lanka Telecom)**. Five AI agents handle the routine work of screening CVs, tracking intern vacancies, scheduling interviews, preparing placement letters, and drafting/sending Outlook emails. Two people make every decision that matters.

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

- Runs locally on your machine with SQLite and llama.cpp (no local database server required).
- Reads intern records from Talent Hub (example JSON or read-only MongoDB connection string) and never writes to them.
- Opens a vacancy when interns are about to finish, and suggests the best waiting CVs for it.
- Local LLM (Gemma 3 4B by default) served by llama.cpp with flash-attention and KV prompt-cache reuse.
- Scores are calculated by code, not by the model (with pre-tokenized CV and stem-prefix caching).
- Every model answer is checked against the CV before it is saved.
- Flexible Microsoft Outlook email integration (`hiring/mailer.py` + `hiring/outlook.py`):
  - **One-click Outlook Desktop / Mail app (`outlook_app`, default on `./start_mail.sh` & `./start_app.sh`):** Zero Azure setup or passwords — clicking Send generates the draft with all attachments, archives an `.eml` in `data/outbox/`, and opens it right inside Outlook Desktop / Apple Mail so the supervisor just clicks **Send**.
  - **Microsoft Graph sending (`graph` / `graph_draft`)** powered by Microsoft's official `msal` library (OAuth 2.0 Device Code Flow): creates an Outlook draft, adds each attachment one by one (using `createUploadSession` in 320 KiB chunks for files $\ge 3\text{ MB}$), and sends it.
  - **Office 365 SMTP fallback (`smtp.office365.com:587`)** if Microsoft Graph fails or when SMTP AUTH is preferred.
  - **`.eml` archive (`eml` mode):** Archives Outlook-ready `.eml` files (`X-Unsent: 1`) in `data/outbox/` alongside Outlook Web compose links.
- After a Pass, a placement letter (plain A4 PDF) is made by code and attached to the acceptance email.
- Plain web UI with two roles (plus **Mail-only mode** via `./start_mail.sh`).

## Tech stack

Python, FastAPI, LangGraph, SQLAlchemy (SQLite in WAL mode), APScheduler, llama.cpp, `msal` + Microsoft Graph Mail API v1.0, `openpyxl`, pymongo (optional read-only Talent Hub lookup),
fpdf2 (placement letter PDF), and plain HTML, CSS and JavaScript.

## Requirements

- macOS (also works on Linux)
- Python 3.10 or newer
- [llama.cpp](https://github.com/ggml-org/llama.cpp) (`brew install llama.cpp`)
- About 3 GB of free memory for the model

## Installation

```bash
git clone https://github.com/AshokaBC2001/hr-hiring.git
cd hr-hiring
git checkout feature/mail-only-mode
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
The model is used to read each CV. If it is not running, you can still type a student's details by hand.

The menu then shows only three pages:

| Page | What it does |
|---|---|
| **Add CV** | Upload CVs and type the **Source** (who sent them). The Recruiter reads the name, email, university and area. **Add a student without a CV** adds a student by hand (no CV file, or the model is not running). |
| **Register** | One row per CV, in the same columns as the section's intern sheet. Filter by mail date, status and source, then **Export to Excel** or **Export CSV** (the exports use the filters). Tick students to send them the invitation together. |
| **Mail settings** | The attachments, how mails are sent (email provider, password, test inbox), the saved mail copies and the mail template. |

On each candidate page:

1. **Invitation mail:** filled from the template. Pick the **visit date** (the next Tuesday is suggested); it is
   written into the mail as `29-DEC-2026  (Tuesday)`. The subject, the text and the attachments can all be edited.
   Gmail and Outlook-on-the-web links are offered too, for writing the mail yourself (text only, add the attachments by hand).
2. **Preview** shows the mail as the student will see it, with clickable links. **Send mail** hands it over for sending:
   - When the mail really goes out (Microsoft sign-in, SMTP, or classic Outlook sending it by itself), the register
     gets the mail date and the status "Mail sent".
   - When the mail only **opens** in Outlook / Apple Mail, is saved to Outlook Drafts or is saved as an `.eml` file,
     it waits as **"Opened, not confirmed"**. Send it from your mail app, then click **I sent it**. If you did not send
     it, click **Not sent: edit mail** to go back to a draft.
   - If sending fails, the error is shown on the mail and you can try again. A sent mail can be sent again with
     **Resend**. Every try is kept in the mail's **Send history**.
3. **Register details:** after the student replies, open **Paste the student's reply** and click
   **Fill the details from the reply**: the NIC, phone, laptop serial and similar details are filled in and marked
   (the AI only adds text that is really in the reply). Check them, choose a status, give the reference number
   (the next number is suggested) and click **Save details**. Nothing is saved before that.
   - **NIC / PP** must be a valid old NIC (9 digits and V or X) or new NIC (12 digits), or a passport number.
     The birth year read from the NIC is shown under the field. The same NIC cannot be saved for two students.
   - **Tel No** is checked and written as `077 123 4567` (numbers from other countries keep their `+` code).
   - **Vehicle No** only gives a warning when it does not look like a Sri Lankan number plate.
4. **Not read yet:** when the model is not running (or the Recruiter could not read the CV), click
   **Enter details by hand**. The email is filled in from the CV text when it can be found.

**Several invitations at once:** on the **Register** page, tick the students who have not been mailed and click
**Prepare invitations**. Choose one visit date and the attachments. Students who were already mailed, have no email,
or are still being read are left out.
- With Microsoft sign-in or an SMTP password, the mails are sent one after another, a few seconds apart, and the
  window shows how many were sent.
- With the Outlook / Mail app, **Open next** opens the mails one at a time. Send each one, then click
  **I sent them all**.

**Test inbox:** when **Safe Demo Recipient Override** is set in Mail settings, a red banner is shown on every page and
the button reads **Send to TEST inbox**. Mails sent to the test inbox never change the register.

**Sending:** `hiring/mailer.py` (backed by `hiring/outlook.py`) sends the mail. In **Mail settings → Mail sending**,
enter your email and choose the **Email provider** (any email domain works; **Detect** suggests the provider from the
address):

| Provider | How mails are sent |
|---|---|
| **Microsoft** (Outlook, Microsoft 365, Outlook.com) | With a password: SMTP through `smtp.office365.com:587` (STARTTLS). With Microsoft sign-in (Advanced options): Microsoft Graph. With neither: your Outlook / Mail app. |
| **Google** (Gmail, Google Workspace) | With a Google **App Password** (Google Account → Security → 2-Step Verification → App passwords): SMTP through `smtp.gmail.com:587`. Without it: your mail app. A Google Workspace admin can turn App Passwords off. |
| **Other mail server** | SMTP with the server name, port and security your IT team gives you: STARTTLS (usually 587), SSL (usually 465) or none. |
| **Outlook or Mail app on this computer** | Always opens the mail in Outlook Desktop / Apple Mail. No password is stored. |

- **Automatic mode (`auto`, default on `Start_HR_Mail.command`, `./start_mail.sh`, and `./start_app.sh`):** Uses 1-click
  browser **Microsoft Graph** if signed in (Microsoft provider only), **SMTP** if a password is saved, or opens
  **Outlook Desktop / Apple Mail** (`outlook_app`) with no setup (see [SUPERVISOR_GUIDE.md](SUPERVISOR_GUIDE.md)).
- **Open in Outlook Desktop / Mail app (`outlook_app`):** clicking Send makes the mail with all its attachments, keeps an
  `.eml` copy in `data/outbox/`, and opens it in Outlook Desktop / Apple Mail. **New Outlook** cannot open ready-to-send
  drafts: on a Mac the mail opens in Apple Mail instead; on Windows you are asked to download the `.eml`, or to use
  Microsoft sign-in or SMTP. **Open again** only opens the mail; it never sends it by itself.
- **Microsoft Graph (`graph` mode):** Signs in via Microsoft OAuth 2.0 (`msal`), creates a draft (`POST /v1.0/me/messages`),
  attaches every file one by one (switching to `createUploadSession` in 320 KiB-aligned chunks for files
  $\ge 3\text{ MB}$ such as `individual user manual.pdf`), and sends (`POST /v1.0/me/messages/{id}/send`).
- **SMTP fallback:** Falls back to SMTP if Graph fails and a password is saved.
- **`.eml` archive (`eml` mode):** Saves a ready-to-open `.eml` file in `data/outbox/` with all attachments inside. Mails
  sent in the other modes are also kept in `data/outbox/`.
- **Login errors** are explained for each provider (for example, Google asks for an App Password, and Microsoft may
  have SMTP sign-in switched off for your account).
- **Size check:** sending stops before it starts when the attachments are too big for the provider (about 35 MB for
  Microsoft and Google, 25 MB for other servers, after the attachments are packed into the mail). The Attachments box
  shows the total.
- **Password:** kept in the computer's keychain (macOS Keychain, Windows Credential Manager) when the `keyring` package
  is installed (it is in `requirements.txt`), otherwise in the local database. Mail settings shows where it is kept.
- **Real test send:** in **Mail settings**, enter a test inbox email and click **Send test mail** (or run
  `.venv/bin/python run.py mail-test --target <email>`).

**Saved mail copies:** a copy of every invitation is kept in `data/outbox/`. Once a day, copies of sent mails older than
90 days are made smaller: the attachments are taken out and their names are kept in the text. Mails that are not
confirmed as sent are never changed. **Shrink old copies now** (Mail settings) does it straight away.

Settings for mail-only mode (in `.env` or the environment):

| Variable | Default | What it does |
|---|---|---|
| `HR_MAIL_BULK_DELAY` | `2.5` | Seconds between mails when several invitations are sent at once. |
| `HR_MAIL_OUTBOX_KEEP_DAYS` | `90` | Sent mail copies older than this are made smaller. |
| `HR_SECRET_STORE` | `auto` | `auto` keeps the password in the keychain when `keyring` is installed; `db` always uses the local database. |

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
| Outlook Graph / SMTP sending & Demo redirect | **Mail settings → Outlook sending**, **Replies → Outlook settings**, or `HR_OUTLOOK_*` / `.env` |
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
run.py                                   CLI helpers: check, samples, once, brief, status, outlook-*, mail-test
employees/                               the 5 AI agents (Markdown)
shared/areas.json                        starter areas
shared/example_interns.json              made-up intern records (until Talent Hub access is granted)
shared/letter/                           letter settings and the font (DejaVu Sans)
shared/mail/                             invitation mail template and mail settings (mail-only mode)
hiring/
  config.py     settings               db.py         database (SQLite)
  llm.py        llama-server client    checks.py     checks on model answers
  talenthub.py  reads Talent Hub       vacancies.py  vacancies and suggestions
  letters.py    placement letter PDF   outlook.py    Microsoft Outlook (MSAL, Graph Upload Session, SMTP)
  invitation.py invitation mail, attachments and the intern register (mail-only mode)
  mailer.py     multi-step Outlook sender + Office 365 SMTP fallback + .eml archiver
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
- Microsoft Outlook authentication and Graph Mail integration use Microsoft's official [AzureAD/microsoft-authentication-library-for-python](https://github.com/AzureAD/microsoft-authentication-library-for-python) (`msal`, MIT license) with the Microsoft Graph Mail API v1.0.
- The PDF is made with [fpdf2](https://github.com/py-pdf/fpdf2) (LGPL-3.0), installed as a library.
- The letter font is DejaVu Sans (Bitstream Vera license, see `shared/letter/fonts/LICENSE.txt`).
- All people in `samples/` are fictional.
