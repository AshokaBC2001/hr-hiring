"""Placement letters: a plain A4 PDF made by code and attached to the acceptance email.

The approach follows faizanfirdousi/appointment-letter-automation (MIT license):
a letter template and an email template, the letter turned into a PDF, and the PDF
attached to the email. Here the HR officer approves the email before it is released,
and the PDF is made with the fpdf2 library (py-pdf/fpdf2, LGPL-3.0).

The model never writes the letter. Code fills the wording in
employees/onboarding/templates/placement-letter.md with the candidate's own details,
so the name, area, work ID and dates are always exactly right.

    after a Pass  ->  work ID  ->  acceptance email (draft)  ->  placement letter PDF
    dates (automatic): starts the Monday after the interview, ends six months later
    the HR officer can change the dates  ->  the letter is made again
    HR officer releases the email  ->  outbox: one .eml file with the PDF attached
"""
from __future__ import annotations

import json
import re
import calendar
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate

from fpdf import FPDF

from . import config, db

SETTINGS_FILE = config.ROOT / "shared" / "letter" / "letter.json"
FONT_DIR = config.ROOT / "shared" / "letter" / "fonts"
TEMPLATE_FILE = config.ROOT / "employees" / "onboarding" / "templates" / "placement-letter.md"
TO_BE_CONFIRMED = "To be confirmed"
DEFAULTS = {"department": "HR Department", "reference_prefix": "PL", "sender": "Hiring Team <hiring@example.com>"}
PLACEMENT_MONTHS = 6       # how long a placement lasts


class LetterError(ValueError):
    """The letter details are not valid (for example an end date before the start date)."""


# ---- settings and wording ---------------------------------------------------------------
def settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def wording() -> tuple[str, list[str]]:
    """The subject and the paragraphs from the template file (the part after the dashes)."""
    text = TEMPLATE_FILE.read_text(encoding="utf-8")
    parts = re.split(r"^-{10,}\s*$", text, maxsplit=1, flags=re.M)
    text = parts[1] if len(parts) == 2 else text
    lines = text.strip().splitlines()
    subject = "Placement Letter"
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0].split(":", 1)[1].strip() or subject
        lines = lines[1:]
    paragraphs = [" ".join(x.split()) for x in re.split(r"\n\s*\n", "\n".join(lines)) if x.strip()]
    return subject, paragraphs


class _Blanks(dict):
    def __missing__(self, key):          # an unknown blank is left as it is, never an error
        return "{" + key + "}"


# ---- the details ------------------------------------------------------------------------
def long_date(value: str) -> str:
    """2026-12-01 -> 1 December 2026. An empty date is "To be confirmed"."""
    if not value:
        return TO_BE_CONFIRMED
    d = date.fromisoformat(value)
    return f"{d.day} {d.strftime('%B %Y')}"


def check_dates(start: str, end: str) -> tuple[str, str]:
    """Both dates as YYYY-MM-DD (or empty). The end date must be after the start date."""
    out = []
    for label, value in (("start", start), ("end", end)):
        value = (value or "").strip()
        if value:
            try:
                value = date.fromisoformat(value).isoformat()
            except ValueError as e:
                raise LetterError(f"The {label} date is not a valid date. Use the date picker.") from e
        out.append(value)
    if out[0] and out[1] and out[1] <= out[0]:
        raise LetterError("The end date must be after the start date.")
    return out[0], out[1]


def _day_of(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date() if value else None
    except ValueError:
        return None


def add_months(day: date, months: int) -> date:
    """The same day some months later (31 August + 6 months = 28 or 29 February)."""
    m = day.month - 1 + months
    year, month = day.year + m // 12, m % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def automatic_dates(c: db.Candidate) -> tuple[str, str]:
    """The placement starts on the Monday after the interview and lasts six months.
    For example: interview on Tuesday 24 November 2026 -> 30 November 2026 to 29 May 2027.
    An interview on a Monday starts the following Monday."""
    day = _day_of(c.interview_at) or _day_of(c.result_at) or date.today()
    start = day + timedelta(days=7 - day.weekday())
    end = add_months(start, PLACEMENT_MONTHS) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def letter_dates(c: db.Candidate) -> tuple[str, str]:
    """The dates on the letter: the ones the HR officer saved, otherwise the automatic ones."""
    letter = c.letter or {}
    auto_start, auto_end = automatic_dates(c)
    return letter.get("start_date") or auto_start, letter.get("end_date") or auto_end


def dates_ready(c: db.Candidate) -> bool:
    start, end = letter_dates(c)
    return bool(start and end)


def file_name(c: db.Candidate) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", f"Placement_Letter_{c.app_no}") + ".pdf"


def reference(c: db.Candidate) -> str:
    return f"{settings()['reference_prefix']}/{c.app_no}"


def details(c: db.Candidate) -> list[tuple[str, str]]:
    start, end = letter_dates(c)
    return [("Work ID", str(c.work_id or "")), ("Area", c.area or ""),
            ("Start date", long_date(start)), ("End date", long_date(end))]


# ---- the PDF ----------------------------------------------------------------------------
def build(c: db.Candidate, today: date | None = None) -> bytes:
    """The placement letter as PDF bytes: one plain A4 page."""
    s = settings()
    subject, paragraphs = wording()
    start, end = letter_dates(c)
    blanks = _Blanks(name=c.name, first_name=(c.name or "").split()[0] if c.name else "", area=c.area,
                     work_id=str(c.work_id or ""), start_date=long_date(start), end_date=long_date(end))

    pdf = FPDF(format="A4")
    pdf.set_title(f"{subject} - {c.name}")
    pdf.set_author(s["department"])
    pdf.set_margins(25, 25, 25)
    pdf.set_auto_page_break(True, margin=25)
    pdf.add_font("Body", "", str(FONT_DIR / "DejaVuSans.ttf"))
    pdf.add_font("Body", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    line = 6

    pdf.set_font("Body", "", 10.5)
    pdf.cell(width, line, long_date((today or date.today()).isoformat()), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.cell(width, line, f"Ref: {reference(c)}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(line)
    pdf.cell(width, line, c.name, new_x="LMARGIN", new_y="NEXT")
    if c.email:
        pdf.cell(width, line, c.email, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(line)

    pdf.set_font("Body", "B", 11)
    pdf.cell(width, line, f"Subject: {subject}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(line / 2)

    pdf.set_font("Body", "", 10.5)
    for para in paragraphs:
        if para.strip() == "{details}":
            with pdf.table(col_widths=(35, 85), width=120, align="LEFT", first_row_as_headings=False,
                           borders_layout="NONE", line_height=line + 1, padding=(0, 2)) as table:
                for label, value in details(c):
                    row = table.row()
                    row.cell(label)
                    row.cell(value)
            pdf.ln(line / 2)
            continue
        pdf.multi_cell(width, line, para.format_map(blanks), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(line / 2)

    pdf.ln(line)
    pdf.cell(width, line, "Yours sincerely,", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(line)
    pdf.cell(width, line, s["department"], new_x="LMARGIN", new_y="NEXT")   # basic version: no signature line
    return bytes(pdf.output())


def make(candidate_id: int) -> str:
    """Makes the candidate's letter again and saves it in data/letters. Returns the file name."""
    c = db.get_candidate(candidate_id)
    if c is None or not c.work_id:
        raise LetterError("A placement letter needs a work ID first.")
    name = file_name(c)
    (db.paths.letters / name).write_bytes(build(c))
    start, end = letter_dates(c)
    letter = dict(c.letter or {})
    letter.update(start_date=start, end_date=end, file=name, made_at=db.now())
    db.update_candidate(c.id, letter=letter)
    return name


def read(c: db.Candidate) -> bytes:
    """The saved letter, made again if the file is missing."""
    name = (c.letter or {}).get("file") or file_name(c)
    path = db.paths.letters / name
    if not path.exists():
        make(c.id)
    return path.read_bytes()


# ---- the email file -------------------------------------------------------------------
def email_file(c: db.Candidate, subject: str, body: str, attach: bool) -> bytes:
    """A real email file (.eml). Double-click it to open it in Mail or Outlook; the placement
    letter is inside it as an attachment. Nothing is sent by the system."""
    msg = EmailMessage()
    msg["From"] = settings()["sender"]
    msg["To"] = f"{c.name} <{c.email}>"
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["X-Unsent"] = "1"                 # Outlook opens it as a message ready to send
    msg.set_content(body)
    if attach:
        msg.add_attachment(read(c), maintype="application", subtype="pdf", filename=file_name(c))
    return msg.as_bytes()
