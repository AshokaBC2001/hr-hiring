"""The invitation mail and the intern register (mail-only mode).

    Add CV  ->  the Recruiter reads it (name, email, university, area)
            ->  the invitation mail is filled from the template (plain code, no model)
            ->  the HR officer picks the visit date and edits anything in the mail
            ->  Preview  ->  Send (handed to mailer.send_mail, the Outlook handover point)
            ->  the register row gets the mail date and "Mail sent"
            ->  after the student replies, the HR officer fills NIC, Tel No, Laptop serial
                and gives the reference number (the system suggests the next one)

The template is shared/mail/invitation.txt. The HR officer can change it on the Mail settings
page; that copy is kept in data/mail/. {visit_date} in the template is replaced with the date
the HR officer picks, written as 29-DEC-2026 (Tuesday).
"""
from __future__ import annotations

import html as html_lib
import io
import json
import re
from datetime import date, datetime, timedelta
from email.utils import formataddr
from pathlib import Path

from sqlalchemy.orm import defer

from . import checks, config, db, mailer, outlook

SHARED = config.ROOT / "shared" / "mail"
DEFAULT_TEMPLATE = SHARED / "invitation.txt"
DEFAULT_SETTINGS = SHARED / "mail.json"
STANDARD_ATTACHMENTS = SHARED / "attachments"    # the required files that come with the template
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

REGISTER_COLUMNS = [("ref_no", "No"), ("mail_date", "Mail date"), ("source", "Source"),
                    ("university", "University"), ("technology", "Technology"), ("name", "Name"),
                    ("nic", "NIC / PP"), ("address", "Address / Company"), ("vehicle", "Vehicle No"),
                    ("tel", "Tel No"), ("laptop", "Laptop Serial No"), ("status", "Status")]
REGISTER_STATUSES = ["Not mailed", "Mail sent", "Replied", "Confirmed", "Not coming", "Visited"]
EDITABLE = ("ref_no", "source", "university", "technology", "name", "nic", "address", "vehicle", "tel",
            "laptop", "status")


class MailProblem(ValueError):
    """Something the HR officer has to fix (shown as a plain message)."""


# ---- settings, template and attachments ---------------------------------------------------
def _own(name: str) -> Path:
    return db.paths.mail / name


def settings() -> dict:
    out = {"subject": "", "sender": "", "visit_weekday": "Tuesday"}
    for path in (DEFAULT_SETTINGS, _own("settings.json")):
        try:
            out.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return out


def template() -> str:
    own = _own("invitation.txt")
    return (own if own.exists() else DEFAULT_TEMPLATE).read_text(encoding="utf-8")


def save_settings(subject: str, template_text: str) -> None:
    subject, template_text = (subject or "").strip(), (template_text or "").strip()
    if not subject:
        raise MailProblem("The subject cannot be empty.")
    if not template_text:
        raise MailProblem("The mail text cannot be empty.")
    own = {}
    try:
        own = json.loads(_own("settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    own["subject"] = subject[:300]
    _own("settings.json").write_text(json.dumps(own, indent=1), encoding="utf-8")
    _own("invitation.txt").write_text(template_text + "\n", encoding="utf-8")


def reset_settings() -> None:
    """Back to the template and subject that came with the system."""
    for name in ("settings.json", "invitation.txt"):
        if _own(name).exists():
            _own(name).unlink()


def _standard() -> dict[str, Path]:
    if not STANDARD_ATTACHMENTS.exists():
        return {}
    return {p.name: p for p in STANDARD_ATTACHMENTS.iterdir() if p.is_file() and not p.name.startswith(".")}


def attachments() -> list[dict]:
    """The files attached to every invitation mail: the required ones that come with the template
    (shared/mail/attachments) and any extra ones the HR officer uploaded (data/mail/attachments).
    An uploaded file with the same name as a required one replaces it, for a newer version."""
    files = dict(_standard())
    standard = set(files)
    for p in db.paths.mail_attachments.iterdir():
        if p.is_file() and not p.name.startswith("."):
            files[p.name] = p
    return [{"name": n, "size": p.stat().st_size, "standard": n in standard,
             "replaced": n in standard and p.parent == db.paths.mail_attachments}
            for n, p in sorted(files.items(), key=lambda x: (x[0] not in standard, x[0].lower()))]


def attachment_path(name: str) -> Path:
    name = Path(name).name
    own = db.paths.mail_attachments / name
    if own.parent != db.paths.mail_attachments:
        raise MailProblem("Not a valid file name.")
    if own.exists():
        return own
    return _standard().get(name, own)


def add_attachment(filename: str, data: bytes) -> dict:
    name = re.sub(r"[^\w .()-]", "", Path(filename or "file").name).strip()[:120] or "file"
    if Path(name).suffix.lower() not in config.MAIL_ATTACH_TYPES:
        return {"ok": False, "file": name, "reason": "Only PDF, Excel and Word files can be attached."}
    if not data:
        return {"ok": False, "file": name, "reason": "The file is empty."}
    if len(data) > config.MAIL_ATTACH_MAX_BYTES:
        return {"ok": False, "file": name, "reason": "The file is larger than 10 MB."}
    (db.paths.mail_attachments / name).write_bytes(data)
    return {"ok": True, "file": name}


def remove_attachment(name: str) -> None:
    """Removes an uploaded file. The required files that come with the template cannot be removed
    here (untick them on a single mail instead); removing an uploaded newer version brings back
    the original."""
    name = Path(name).name
    own = db.paths.mail_attachments / name
    if own.exists():
        own.unlink()
        return
    if name in _standard():
        raise MailProblem("This file comes with the mail template, so it cannot be removed. "
                          "Untick it on a mail if that student does not need it.")
    raise MailProblem("That file is not in the attachments.")


# ---- the visit date -------------------------------------------------------------------------
def next_visit_day(today: date | None = None) -> date:
    """The next visit weekday (Tuesday) after today. If today is Tuesday, the one after it."""
    today = today or date.today()
    want = WEEKDAYS.index(settings().get("visit_weekday", "Tuesday"))
    days = (want - today.weekday()) % 7 or 7
    return today + timedelta(days=days)


def visit_text(day: date) -> str:
    """29-DEC-2026 (Tuesday), the format used in the mail."""
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    return f"{day.day:02d}-{months[day.month - 1]}-{day.year}  ({WEEKDAYS[day.weekday()]})"


# ---- the mail for one candidate ---------------------------------------------------------------
def draft(c: db.Candidate) -> dict:
    """The candidate's invitation mail: the saved one, or a new one from the template."""
    saved = dict(c.invite or {})
    if saved.get("body"):
        saved.setdefault("status", "draft")
        return saved
    day = next_visit_day()
    return {"subject": settings()["subject"], "body": template().replace("{visit_date}", visit_text(day)),
            "visit_date": day.isoformat(), "attachments": [a["name"] for a in attachments()],
            "status": "new"}


def save_draft(app_no: str, subject: str, body: str, visit_date: str, files: list[str]) -> dict:
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    if (c.invite or {}).get("status") == "sent":
        raise MailProblem("This mail was already sent.")
    subject, body = (subject or "").strip(), (body or "").strip()
    if not subject:
        raise MailProblem("The subject cannot be empty.")
    if not body:
        raise MailProblem("The mail text cannot be empty.")
    if "{visit_date}" in body or "{visit_date}" in subject:
        raise MailProblem("Pick the visit date; {visit_date} is still in the mail.")
    try:
        visit_date = date.fromisoformat(visit_date).isoformat() if visit_date else ""
    except ValueError as e:
        raise MailProblem("The visit date is not valid. Use the date picker.") from e
    have = {a["name"] for a in attachments()}
    files = [f for f in dict.fromkeys(files or []) if f in have]
    invite = {"subject": subject[:300], "body": body, "visit_date": visit_date, "attachments": files,
              "status": "draft", "saved_at": db.now()}
    db.update_candidate(c.id, invite=invite)
    return invite


def to_html(body: str) -> str:
    """The mail text as simple HTML: paragraphs, line breaks and clickable links, like the
    mails the section sends today. The confidentiality notice is shown in small grey text."""
    notice_at = body.find("CONFIDENTIALITY NOTICE")
    main, notice = (body[:notice_at], body[notice_at:]) if notice_at >= 0 else (body, "")

    def block(text: str) -> str:
        out = []
        for para in re.split(r"\n\s*\n", text.strip()):
            safe = html_lib.escape(para)
            safe = re.sub(r"(https?://[^\s<]+?)([).,;:!?]*(?=\s|<|$))", r'<a href="\1">\1</a>\2', safe)
            out.append("<p>" + safe.replace("\n", "<br>") + "</p>")
        return "\n".join(out)

    parts = [f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5">{block(main)}']
    if notice:
        title, _, rest = notice.partition("\n")
        parts.append(f'<p style="margin-top:28px;color:#c00000;font-weight:bold;font-family:Georgia,serif">'
                     f'{html_lib.escape(title)}</p><div style="font-size:11px;color:#555">{block(rest)}</div>')
    parts.append("</div>")
    return "\n".join(parts)


def send(app_no: str) -> dict:
    """Hands the finished mail to mailer.send_mail (the Outlook handover point)."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    if c.status == "new":
        raise MailProblem("The Recruiter is still reading this CV. Try again in a moment.")
    if not c.email or not checks.EMAIL_RE.fullmatch(c.email):
        raise MailProblem("Add the candidate's email address first.")
    d = draft(c)
    if "{visit_date}" in d["body"] or "{visit_date}" in d["subject"]:
        raise MailProblem("Pick the visit date first.")
    paths = [attachment_path(n) for n in d.get("attachments", [])]
    to_hdr = formataddr((c.name, c.email)) if c.name else c.email
    try:
        result = mailer.send_mail(to_hdr, d["subject"], to_html(d["body"]),
                                  d["body"], paths, sender=settings().get("sender", ""),
                                  name_hint=f"{c.app_no}_invitation_{datetime.now():%Y%m%d%H%M%S}")
    except mailer.MailError as e:
        raise MailProblem(str(e)) from e
    how_code = result.get("how", "")
    sent = dict(
        d,
        status="sent",
        sent_at=db.now(),
        how=how_code,
        file=result.get("file", ""),
        sender=result.get("sender", ""),
        sent_to=result.get("sent_to", c.email),
        message_id=result.get("message_id", ""),
        web_link=result.get("web_link", ""),
        opened_in=result.get("opened_in", ""),
    )
    reg = dict(c.register or {})
    reg["mail_date"] = date.today().isoformat()
    if reg.get("status", "Not mailed") == "Not mailed":
        reg["status"] = "Mail sent"
    db.update_candidate(c.id, invite=sent, register=reg)
    if how_code == "outlook_app":
        how_text = f"opened in {sent['opened_in'] or 'Outlook / Mail app'} ready to send ({sent['file']})"
    elif how_code == "eml":
        how_text = "saved to the outbox as " + sent["file"]
    elif how_code == "graph_draft":
        how_text = f"saved to Outlook Drafts ({sent['sent_to']})"
    elif how_code == "graph":
        how_text = f"sent with Outlook Graph ({sent['sender'] or 'Microsoft account'} -> {sent['sent_to']})"
    elif how_code == "smtp":
        how_text = f"sent with Office 365 SMTP ({sent['sender'] or 'SMTP'} -> {sent['sent_to']})"
    else:
        how_text = "sent with " + how_code
    db.log_event(c.id, "HR officer", f"Invitation mail {how_text} (visit {d.get('visit_date') or 'date in the text'}, "
                                     f"{len(paths)} attachment(s)).")
    return sent


def open_sent_eml(app_no: str) -> dict:
    """Re-open an archived invitation .eml in Outlook Desktop or Mail app."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    inv = c.invite or {}
    fname = inv.get("file") or ""
    if not fname:
        raise MailProblem("No .eml archive exists for this invitation yet.")
    path = (db.paths.outbox / fname).resolve()
    if path.parent != db.paths.outbox.resolve() or not path.is_file():
        raise MailProblem("Saved .eml file could not be found.")
    paths = [attachment_path(n) for n in inv.get("attachments", []) if attachment_path(n).is_file()]
    body = inv.get("body", "")
    try:
        opened_in = outlook.open_eml_in_desktop_app(
            path,
            to_email=inv.get("sent_to") or c.email or "",
            to_name=c.name or "",
            subject=inv.get("subject", ""),
            html=to_html(body) if body else "",
            text=body,
            attachments=paths,
        )
    except outlook.OutlookError as exc:
        raise MailProblem(str(exc)) from exc
    return {"ok": True, "opened_in": opened_in, "file": fname}


def send_test_invite(to_email: str) -> dict:
    """Send a test invitation mail with all active attachments to verify live delivery."""
    clean_to = re.sub(r"[\r\n]+", " ", (to_email or "").strip())
    if not checks.EMAIL_RE.fullmatch(clean_to):
        raise MailProblem("Enter a valid test email address.")
    cfg = settings()
    day = next_visit_day()
    body = template().replace("{visit_date}", visit_text(day))
    subject = f"{cfg['subject']} [TEST]"
    att_rows = attachments()
    paths = [attachment_path(a["name"]) for a in att_rows]
    try:
        res = mailer.send_mail(
            f"Test Recipient <{clean_to}>",
            subject,
            to_html(body),
            body,
            paths,
            sender=cfg.get("sender", ""),
            name_hint=f"test_invitation_{datetime.now():%Y%m%d%H%M%S}",
        )
    except mailer.MailError as exc:
        raise MailProblem(str(exc)) from exc
    return {
        "ok": True,
        **res,
        "attachments": [p.name for p in paths],
        "total_bytes": sum(p.stat().st_size for p in paths),
    }


# ---- the register -----------------------------------------------------------------------------
def row(c: db.Candidate) -> dict:
    reg = c.register or {}
    sent = (c.invite or {}).get("status") == "sent"
    return {"app_no": c.app_no, "ref_no": reg.get("ref_no", ""), "mail_date": reg.get("mail_date", ""),
            "source": c.source or "", "university": c.university or "",
            "technology": reg.get("technology") or c.area or "", "name": c.name or "",
            "nic": reg.get("nic", ""), "address": reg.get("address", ""), "vehicle": reg.get("vehicle", ""),
            "tel": reg.get("tel", ""), "laptop": reg.get("laptop", ""),
            "status": reg.get("status") or ("Mail sent" if sent else "Not mailed"),
            "email": c.email or "", "reading": c.status == "new", "created_at": c.created_at}


def suggest_ref() -> str:
    """The next reference number: the last one used with its final number plus one,
    keeping the zeros (SLTDPDS-2026/11/03/06 -> SLTDPDS-2026/11/03/07)."""
    last = db.get_setting("last_ref") or ""
    m = re.search(r"(\d+)(\D*)$", last)
    if not m:
        return ""
    n = str(int(m.group(1)) + 1).zfill(len(m.group(1)))
    return last[:m.start(1)] + n + m.group(2)


def save_row(app_no: str, fields: dict) -> dict:
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    f = {k: str(fields.get(k, "") or "").strip()[:200] for k in EDITABLE if k in fields}
    if "status" in f and f["status"] not in REGISTER_STATUSES:
        raise MailProblem("Choose a status from the list.")
    if "name" in f and not f["name"]:
        raise MailProblem("The name cannot be empty.")
    if f.get("ref_no"):
        want = f["ref_no"].lower()
        with db.Session() as s:
            for other_app, other_name, other_reg in s.execute(
                db.select(db.Candidate.app_no, db.Candidate.name, db.Candidate.register)
                .where(db.Candidate.id != c.id)
            ):
                if (other_reg or {}).get("ref_no", "").strip().lower() == want:
                    raise MailProblem(f"{f['ref_no']} is already used for {other_name or other_app}.")
    reg = dict(c.register or {})
    for k in ("ref_no", "technology", "nic", "address", "vehicle", "tel", "laptop", "status"):
        if k in f:
            reg[k] = f[k]
    update = {"register": reg}
    if "source" in f:
        update["source"] = f["source"]
    if "university" in f:
        update["university"] = f["university"]
    if "name" in f:
        update["name"] = f["name"]
    db.update_candidate(c.id, **update)
    if f.get("ref_no") and f["ref_no"] != (c.register or {}).get("ref_no"):
        db.set_setting("last_ref", f["ref_no"])
    db.log_event(c.id, "HR officer", "Updated the register details.")
    return row(db.get_candidate(c.id))


def register_data(show: str = "all", q: str = "") -> tuple[list[dict], dict[str, int]]:
    """Fetch register rows and filter counts in one deferred-column SQLite query."""
    with db.Session() as s:
        every = list(
            s.scalars(
                db.select(db.Candidate)
                .options(defer(db.Candidate.cv_text), defer(db.Candidate.profile), defer(db.Candidate.area_scores))
                .where(db.Candidate.status != "archived")
                .order_by(db.Candidate.id.desc())
            )
        )
    all_rows = [row(c) for c in every]
    sent_count = sum(1 for r in all_rows if r["mail_date"])
    counts = {"all": len(all_rows), "sent": sent_count, "not_sent": len(all_rows) - sent_count}
    if show == "sent":
        out = [r for r in all_rows if r["mail_date"]]
    elif show == "not_sent":
        out = [r for r in all_rows if not r["mail_date"]]
    else:
        out = all_rows
    q = (q or "").strip().lower()
    if q:
        out = [r for r in out if q in " ".join(str(r[k]) for k, _ in REGISTER_COLUMNS + [("app_no", ""), ("email", "")]).lower()]
    return out, counts


def rows(show: str = "all", q: str = "") -> list[dict]:
    return register_data(show, q)[0]


def export_xlsx(data: list[dict]) -> bytes:
    """The register as an Excel file, in the same column order as the section's own sheet."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Interns"
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws.append([label for _, label in REGISTER_COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="FFFF66")
        cell.alignment = Alignment(vertical="bottom", wrap_text=True)
        cell.border = border
    for r in data:
        values = []
        for key, _ in REGISTER_COLUMNS:
            v = r.get(key, "")
            if key == "mail_date" and v:
                try:
                    v = date.fromisoformat(str(v))
                except ValueError:
                    v = str(v)
            elif isinstance(v, str) and v.startswith("="):
                v = "'" + v
            values.append(v)
        ws.append(values)
        for cell in ws[ws.max_row]:
            cell.border = border
            if isinstance(cell.value, date):
                cell.number_format = "d-mmm-yy"
    widths = {"No": 30, "Mail date": 11, "Source": 20, "University": 28, "Technology": 20, "Name": 32,
              "NIC / PP": 16, "Address / Company": 26, "Vehicle No": 12, "Tel No": 14,
              "Laptop Serial No": 20, "Status": 13}
    for i, (_, label) in enumerate(REGISTER_COLUMNS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = widths.get(label, 15)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
