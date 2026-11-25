"""Settings for the HR Hiring system.

Every value can be changed here. A few can also be set with environment
variables, so you can try another model or port without editing code.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where the system keeps its data (register database, CV files, released replies, logs).
DATA_DIR = Path(os.environ.get("HR_DATA_DIR", str(ROOT / "data")))

EMPLOYEES_DIR = ROOT / "employees"     # one Markdown folder per AI employee
AREAS_FILE = ROOT / "shared" / "areas.json"  # starter areas (copied into the register once)
WEB_DIR = ROOT / "web"
SAMPLES_DIR = ROOT / "samples"

# ---- The model (llama.cpp llama-server, OpenAI-style API) -------------------
MODEL_URL = os.environ.get("HR_MODEL_URL", "http://127.0.0.1:8080").rstrip("/")
MODEL_NAME = os.environ.get("HR_MODEL_NAME", "gemma-3-4b-it")
MODEL_TIMEOUT = float(os.environ.get("HR_MODEL_TIMEOUT", "300"))   # seconds per call
MODEL_TEMPERATURE = 0.2
MODEL_MAX_TOKENS = 900
# Gemma models have no separate "system" role, so instructions go into the user message.
MERGE_SYSTEM_INTO_USER = True

# ---- The web app ---------------------------------------------------------------
APP_HOST = "127.0.0.1"   # only this computer can open the pages
APP_PORT = int(os.environ.get("HR_APP_PORT", "8000"))

# ---- Scoring (our own placeholder values, out of 100) ------------------------
WEIGHTS = {"skills": 40, "projects": 30, "education": 20, "experience": 10}
SHORTLIST_AT = 60      # 60 or above: Shortlisted
BORDERLINE_AT = 50     # 50 to 59: the HR officer decides; below 50: Not shortlisted (HR officer confirms)
# Each area has its own list of related studies (in shared/areas.json and on the Areas page),
# so a degree only gives full education points for the areas it really fits.
SHARED_SKILL_AREAS = 3     # a skill or keyword listed by more than this many areas (for example
                           # Excel, SQL, Communication) does not on its own make a project "related"

# ---- Limits ----------------------------------------------------------------------
MAX_FIXES = 2              # after a failed check the model gets this many more tries
MAX_CV_CHARS = 8000        # longer CVs are cut before they go to the model
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ALLOWED_CV_TYPES = {".pdf", ".docx", ".txt"}
CANDIDATES_PER_RUN = 3     # each employee handles at most this many candidates per run
WORK_ID_START = 1001
WORK_ID_MAX = 9999
APP_NO_PREFIX = "APP-"
NEEDS_REVIEW_AREA = "Needs human review"
NEW_AREA_MARK = "New area:"

# Hours a candidate may wait in a status before the Chief of staff flags it as stalled.
STALLED_AFTER_HOURS = {
    "needs_area": 24, "borderline": 24, "not_shortlisted_check": 24, "needs_person": 24,
    "invite_ready": 24, "reply_ready": 24, "hold_note_ready": 24, "offer_ready": 24,
    "awaiting_result": 72, "on_hold": 168,
}

# ---- The CV pool ------------------------------------------------------------------
POOL_MIN_SCORE = 50        # an area's ranked list only shows CVs scoring this or more for that area
POOL_OTHER_AREAS = 3       # how many other good areas are shown for each CV
POOL_CLOSE_POINTS = 10     # a CV is also suggested for its second best area only if that area is
                           # within this many points of its best area
POOL_SUGGEST_AREAS = 2     # the most areas one CV is suggested for at the same time
POOL_EXPIRE_DAYS = 182     # CVs waiting in the pool longer than this (about 6 months) are archived

# ---- Talent Hub: the intern records (read only; we never write to Talent Hub) -------
# "example" reads shared/example_interns.json, for testing without access.
# "mongodb" reads the real Talent Hub with pymongo.
TALENTHUB_SOURCE = os.environ.get("HR_TALENTHUB", "example")
TALENTHUB_EXAMPLE_FILE = ROOT / "shared" / "example_interns.json"
TALENTHUB_URL = os.environ.get("HR_TALENTHUB_URL", "mongodb://127.0.0.1:27017")
TALENTHUB_DB = os.environ.get("HR_TALENTHUB_DB", "talenthub")
TALENTHUB_COLLECTION = os.environ.get("HR_TALENTHUB_COLLECTION", "interns")
# The field names inside a Talent Hub document. Change these once the real names are known;
# nothing else in the code needs to change.
TALENTHUB_FIELDS = {
    "trainee_id": "traineeId",
    "name": "name",
    "start_date": "startDate",
    "end_date": "endDate",
    "specialization": "specialization",
}
# Talent Hub stores dates in UTC (2026-05-19T00:00:00.000Z). We read them as local dates.
LOCAL_UTC_OFFSET_HOURS = 5.5          # Sri Lanka, UTC+5:30

# ---- Vacancies (places that open when interns finish) -------------------------------
VACANCY_NOTICE_DAYS = 7    # a place opens this many days before the intern's end date
VACANCY_RESERVES = 2       # suggest this many candidates beyond the number of places

# Interview slots: working days only, 45 minutes each.
INTERVIEW_TIMES = ["09:30", "10:15", "11:00", "11:45", "14:00", "14:45", "15:30"]

SCHEDULER_ENABLED = os.environ.get("HR_SCHEDULER", "1") != "0"

# ---- Mail-only mode ------------------------------------------------------------------------
# "full": the whole hiring system. "mail": only Add CV, the invitation mail and the register,
# for testing the mail part on its own. Start with:  HR_MODE=mail ./start_app.sh
MODE = "mail" if os.environ.get("HR_MODE", "full").strip().lower() == "mail" else "full"
MAIL_ATTACH_TYPES = {".pdf", ".xlsx", ".xls", ".docx", ".doc"}
MAIL_ATTACH_MAX_BYTES = 10 * 1024 * 1024      # per file

# ---- Statuses: label shown to people, chip colour, who acts next ----------------
STATUSES = {
    "new":                   ("New CV", "gray", "Recruiter"),
    "in_pool":               ("Waiting in pool", "gray", ""),
    "no_area":               ("No matching area", "gray", ""),
    "archived":              ("Archived", "gray", ""),
    "needs_person":          ("CV needs a person", "amber", "HR officer"),
    "needs_area":            ("Area needs a person", "amber", "HR officer"),
    "borderline":            ("Borderline score", "amber", "HR officer"),
    "not_shortlisted_check": ("Not shortlisted - confirm", "amber", "HR officer"),
    "shortlisted":           ("Shortlisted", "blue", "Coordinator"),
    "invite_ready":          ("Invitation waiting for release", "amber", "HR officer"),
    "awaiting_result":       ("Waiting for interview result", "blue", "Interviewer"),
    "not_shortlisted":       ("Not shortlisted", "gray", "Coordinator"),
    "passed":                ("Passed", "green", "Onboarding"),
    "failed":                ("Failed", "red", "Coordinator"),
    "hold":                  ("On hold", "amber", "Coordinator"),
    "hold_note_ready":       ("Hold note waiting for release", "amber", "HR officer"),
    "on_hold":               ("On hold", "amber", "Interviewer"),
    "reply_ready":           ("Reply waiting for release", "amber", "HR officer"),
    "offer_ready":           ("Acceptance waiting for release", "amber", "HR officer"),
    "closed":                ("Closed", "gray", ""),
}

# Statuses where a person (the HR officer) must act.
HR_STATUSES = {"needs_person", "needs_area", "borderline", "not_shortlisted_check"}
RELEASE_STATUSES = {"invite_ready", "hold_note_ready", "reply_ready", "offer_ready"}
# CVs waiting in the pool (not in the hiring flow yet). The HR officer invites from here.
POOL_STATUSES = {"in_pool", "no_area"}
# Statuses that are shown in the CV pool (Add CV page) and not on the Candidates page.
NOT_IN_HIRING = POOL_STATUSES | {"archived"}
