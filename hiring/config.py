"""Settings for the HR Hiring system.

Every value can be changed here. A few can also be set with environment
variables, so you can try another model or port without editing code.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines from .env without overriding real environment variables."""
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)
    except OSError:
        pass


_load_dotenv(ROOT / ".env")

# Where the system keeps its data (register database, CV files, released replies, logs).
DATA_DIR = Path(os.environ.get("HR_DATA_DIR", str(ROOT / "data")))

EMPLOYEES_DIR = ROOT / "employees"     # one Markdown folder per AI employee
AREAS_FILE = ROOT / "shared" / "areas.json"  # starter areas (copied into the register once)
WEB_DIR = ROOT / "web"
SAMPLES_DIR = ROOT / "samples"

# ---- The model (llama.cpp llama-server or OpenAI-compatible HTTP endpoint) ---
MODEL_URL = os.environ.get("HR_MODEL_URL", "http://127.0.0.1:8080").rstrip("/")
MODEL_NAME = os.environ.get("HR_MODEL_NAME", "gemma-3-4b-it")
MODEL_API_KEY = os.environ.get("HR_MODEL_API_KEY", "").strip()
MODEL_TIMEOUT = float(os.environ.get("HR_MODEL_TIMEOUT", "300"))   # seconds per call
MODEL_TEMPERATURE = 0.2
MODEL_MAX_TOKENS = 900
# Gemma models have no separate "system" role, so instructions go into the user message.
MERGE_SYSTEM_INTO_USER = os.environ.get("HR_MERGE_SYSTEM", "1").strip().lower() not in ("0", "false", "no")
# Optional hosted fallback: when 1, agents fall back directly to deterministic rules if no LLM server is up.
ALLOW_RULES_ONLY = os.environ.get("HR_ALLOW_RULES_ONLY", "0").strip().lower() in ("1", "true", "yes")

# ---- The web app & hosting safeguards ------------------------------------------
APP_HOST = os.environ.get("HR_APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
APP_PORT = int(os.environ.get("HR_APP_PORT") or os.environ.get("PORT") or "8000")
PUBLIC_HOSTING = os.environ.get("HR_PUBLIC_HOSTING", "0").strip().lower() in ("1", "true", "yes")
ALLOWED_HOSTS = (
    ["*"]
    if PUBLIC_HOSTING
    else [h.strip() for h in os.environ.get("HR_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if h.strip()]
)
ALLOWED_ORIGINS = [
    o.strip().lower() for o in os.environ.get("HR_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
AUTH_USER = os.environ.get("HR_AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("HR_AUTH_PASS", "")
ACCESS_TOKEN = os.environ.get("HR_ACCESS_TOKEN", "").strip()
# When 1, live SMTP only sends when Demo Recipient Override (`test_recipient`) is configured.
DEMO_EMAIL_LOCK = os.environ.get("HR_DEMO_EMAIL_LOCK", "0").strip().lower() in ("1", "true", "yes")

# ---- Scoring (our own placeholder values, out of 100) ------------------------
WEIGHTS = {"skills": 40, "projects": 30, "education": 20, "experience": 10}
SHORTLIST_AT = 60      # 60 or above: Shortlisted
BORDERLINE_AT = 50     # 50 to 59: the HR officer decides; below 50: Not shortlisted (HR officer confirms)
RELEVANT_EDUCATION = [
    "computer science", "computing", "information technology", "software engineering",
    "data science", "computer engineering", "electronic", "electrical", "information systems",
    "artificial intelligence", "engineering", "ict",
]

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
POOL_EXPIRE_DAYS = 182     # CVs waiting in the pool longer than this (about 6 months) are archived

# ---- Vacancies & TalentHub timezone -----------------------------------------------
LOCAL_UTC_OFFSET_HOURS = 5.5   # Sri Lanka Standard Time (UTC+5:30)
VACANCY_NOTICE_DAYS = 7        # a place opens this many days before an intern's end date
VACANCY_LOOKBACK_DAYS = 14     # ignore historical interns who finished earlier than this window
VACANCY_RESERVES = 2           # suggest this many candidates beyond the number of places

# Interview slots: working days only, 45 minutes each.
INTERVIEW_TIMES = ["09:30", "10:15", "11:00", "11:45", "14:00", "14:45", "15:30"]

SCHEDULER_ENABLED = os.environ.get("HR_SCHEDULER", "1") != "0"

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
# Statuses that are shown on the Pool page and not on the Candidates page.
NOT_IN_HIRING = POOL_STATUSES | {"archived"}

# ---- Live Email Sender (SMTP) -----------------------------------------------------
# Defaults to disabled (outbox archive only) until enabled via env or HR Email Settings UI.
SMTP_ENABLED = os.environ.get("HR_SMTP_ENABLED", "0").strip().lower() in ("1", "true", "yes")
SMTP_HOST = os.environ.get("HR_SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.environ.get("HR_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("HR_SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("HR_SMTP_PASSWORD", "")
SMTP_USE_TLS = os.environ.get("HR_SMTP_USE_TLS", "1").strip().lower() in ("1", "true", "yes")
SMTP_USE_SSL = os.environ.get("HR_SMTP_USE_SSL", "0").strip().lower() in ("1", "true", "yes")
SMTP_FROM_NAME = os.environ.get("HR_SMTP_FROM_NAME", "SLT Talent Acquisition").strip()
SMTP_FROM_EMAIL = os.environ.get("HR_SMTP_FROM_EMAIL", "").strip()
# Optional safe demo redirect: when set, outbound SMTP goes to this address instead of candidate email.
SMTP_TEST_RECIPIENT = os.environ.get("HR_SMTP_TEST_RECIPIENT", "").strip()
SMTP_TIMEOUT = float(os.environ.get("HR_SMTP_TIMEOUT", "15"))

# ---- SLT TalentHub (external intern DB + read-only REST API) --------------------
# Mode: "auto" (API if TALENT_HUB_API_URL is set, otherwise direct MongoDB), "api", or "mongo"
TALENT_HUB_MODE = os.environ.get("TALENT_HUB_MODE", "auto").strip().lower() or "auto"
TALENT_HUB_MONGO_URI = os.environ.get("TALENT_HUB_MONGO_URI", "mongodb://127.0.0.1:27017")
TALENT_HUB_DB_NAME = os.environ.get("TALENT_HUB_DB_NAME", "slt_talent_hub_sample")
TALENT_HUB_COLLECTION = os.environ.get("TALENT_HUB_COLLECTION", "interns")
TALENT_HUB_TIMEOUT_MS = int(os.environ.get("TALENT_HUB_TIMEOUT_MS", "2000"))
TALENT_HUB_API_URL = os.environ.get("TALENT_HUB_API_URL", "http://127.0.0.1:5050/api").rstrip("/")
TALENT_HUB_API_TOKEN = os.environ.get("TALENT_HUB_API_TOKEN", "talenthub-local-dev-token")
TALENT_HUB_MOCK_HOST = os.environ.get("TALENT_HUB_MOCK_HOST", "127.0.0.1")
# Default to 5050 rather than 5000 because macOS AirPlay Receiver (ControlCenter) listens on 5000.
TALENT_HUB_MOCK_PORT = int(os.environ.get("TALENT_HUB_MOCK_PORT", "5050"))
