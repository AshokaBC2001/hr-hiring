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

import email
import email.message
import email.policy
import html as html_lib
import io
import json
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from email.utils import formataddr
from pathlib import Path

from sqlalchemy.orm import defer

from . import checks, config, db, llm, mailer, outlook

SHARED = config.ROOT / "shared" / "mail"
DEFAULT_TEMPLATE = SHARED / "invitation.txt"
DEFAULT_SETTINGS = SHARED / "mail.json"
STANDARD_ATTACHMENTS = SHARED / "attachments"    # the required files that come with the template
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

REGISTER_COLUMNS = [("ref_no", "No"), ("mail_date", "Mail date"), ("source", "Source"),
                    ("university", "University"), ("technology", "Technology"), ("name", "Name"),
                    ("nic", "NIC / PP"), ("address", "Address / Company"), ("vehicle", "Vehicle No"),
                    ("tel", "Tel No"), ("laptop", "Laptop Serial No"), ("status", "Status")]
REGISTER_STATUSES = ["Not mailed", "Opened, not confirmed", "Mail sent", "Replied", "Confirmed", "Not coming",
                     "Visited"]
EDITABLE = ("ref_no", "source", "university", "technology", "name", "nic", "address", "vehicle", "tel",
            "laptop", "status")


# Invitation states: "new" (from the template), "draft" (saved), "opened" (handed to the mail app,
# Outlook Drafts or an .eml file, waiting for the HR officer to confirm it was sent) and "sent".
DIRECT_HOWS = {"graph", "graph_send", "smtp"}          # these really send the mail
_KEEP_ON_EDIT = ("history", "last_test", "last_error", "last_error_at", "resend_of")


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


def effective_sender() -> str:
    """Return configured Outlook sender if set, otherwise default template sender."""
    ol = outlook.raw_settings()
    acct = outlook.get_signed_in_account(ol)
    addr = (ol.get("sender_email") or (acct.get("username") if acct else "") or ol.get("smtp_user") or "").strip()
    if addr:
        name = (ol.get("from_name") or "").strip()
        if not name or name == "HR Hiring Team":
            name = "Digital Platforms Development Section"
        return formataddr((name, addr))
    return settings().get("sender", "")


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
    st = (c.invite or {}).get("status")
    if st == "sent":
        raise MailProblem("This mail was already sent. Click Resend to change it and send it again.")
    if st == "opened":
        raise MailProblem("This mail is open in your mail app. Click 'I sent it', or 'Edit mail' to change it.")
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
    for keep in _KEEP_ON_EDIT:            # the send history stays with the mail
        if (c.invite or {}).get(keep):
            invite[keep] = c.invite[keep]
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


def _how_text(inv: dict) -> str:
    how, to = inv.get("how", ""), inv.get("sent_to", "")
    if how == "outlook_app":
        if inv.get("status") == "sent":
            return f"sent by {inv.get('opened_in') or 'Outlook'} to {to}"
        if inv.get("saved_only"):
            return f"saved as {inv.get('file', '')} (no mail app could open it)"
        return f"opened in {inv.get('opened_in') or 'the mail app'}, ready to send to {to}"
    if how == "eml":
        return "saved to the outbox as " + inv.get("file", "")
    if how == "graph_draft":
        return f"saved to Outlook Drafts ({to})"
    if how in ("graph", "graph_send"):
        return f"sent with Microsoft Outlook ({inv.get('sender') or 'Microsoft account'} -> {to})"
    if how == "smtp":
        return f"sent by SMTP ({inv.get('sender') or 'SMTP'} -> {to})"
    return "sent with " + how


def _mark_mailed(reg: dict) -> dict:
    reg = dict(reg)
    reg["mail_date"] = date.today().isoformat()
    if reg.get("status", "Not mailed") in ("", "Not mailed", "Opened, not confirmed"):
        reg["status"] = "Mail sent"
    return reg


def send(app_no: str) -> dict:
    """Hands the finished mail to mailer.send_mail (the Outlook handover point).

    Only a mail that was really sent (Microsoft Graph, SMTP, or Outlook sending it by itself) counts
    as "Mail sent" in the register. A mail that was only opened in the mail app, saved to Outlook
    Drafts or saved as an .eml file waits as "opened" until the HR officer clicks "I sent it".
    A mail redirected to the test inbox never changes the register."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    if c.status == "new":
        raise MailProblem("The Recruiter is still reading this CV. Try again in a moment.")
    if not c.email or not checks.EMAIL_RE.fullmatch(c.email):
        raise MailProblem("Add the candidate's email address first.")
    inv = dict(c.invite or {})
    if inv.get("status") == "sent":
        raise MailProblem("This mail was already sent. Click Resend to send it again.")
    if inv.get("status") == "opened":
        raise MailProblem("This mail is already open in your mail app. Click 'I sent it' once you have sent it, "
                          "or 'Edit mail' to change it.")
    d = draft(c)
    if "{visit_date}" in d["body"] or "{visit_date}" in d["subject"]:
        raise MailProblem("Pick the visit date first.")
    paths = [attachment_path(n) for n in d.get("attachments", [])]
    to_hdr = formataddr((c.name, c.email)) if c.name else c.email
    history = list(inv.get("history") or [])
    now = db.now()
    try:
        result = mailer.send_mail(to_hdr, d["subject"], to_html(d["body"]),
                                  d["body"], paths, sender=effective_sender(),
                                  name_hint=f"{c.app_no}_invitation_{datetime.now():%Y%m%d%H%M%S}")
    except mailer.MailError as e:
        failed = dict(d, status="draft", last_error=str(e), last_error_at=now,
                      history=history + [{"at": now, "how": "failed", "error": str(e)[:300]}])
        db.update_candidate(c.id, invite=failed)
        db.log_event(c.id, "HR officer", f"The invitation mail could not be sent: {str(e)[:200]}")
        raise MailProblem(str(e)) from e

    how = result.get("how", "")
    sent_to = result.get("sent_to", c.email)
    really_sent = how in DIRECT_HOWS or bool(result.get("sent_by_app"))
    status = "sent" if really_sent else "opened"
    test = sent_to.strip().lower() != c.email.strip().lower()
    info = {"how": how, "file": result.get("file", ""), "sender": result.get("sender", ""), "sent_to": sent_to,
            "message_id": result.get("message_id", ""), "web_link": result.get("web_link", ""),
            "opened_in": result.get("opened_in", ""), "saved_only": bool(result.get("saved_only")),
            "note": result.get("note", "")}
    entry = {"at": now, "how": how, "sent_to": sent_to, "file": info["file"], "status": status, "test": test}
    history.append(entry)

    if test:
        # Redirected to the test inbox in Mail settings: the student did not get it.
        base = {k: v for k, v in d.items() if k not in ("sent_at", "opened_at", "confirmed_at", "test_only")}
        kept = dict(base, status="draft", history=history, last_test=dict(info, at=now, status=status),
                    last_error="", last_error_at="")
        db.update_candidate(c.id, invite=kept)
        db.log_event(c.id, "HR officer", f"Test copy of the invitation {_how_text(dict(info, status=status))} "
                                         f"(test inbox, not the student).")
        return dict(kept, **info, test_only=True)

    base = {k: v for k, v in d.items() if k not in ("sent_at", "opened_at", "confirmed_at", "test_only")}
    done = dict(base, status=status, history=history, last_error="", last_error_at="", **info)
    reg = dict(c.register or {})
    if really_sent:
        done["sent_at"] = now
        reg = _mark_mailed(reg)
    else:
        done["opened_at"] = now
    db.update_candidate(c.id, invite=done, register=reg)
    db.log_event(c.id, "HR officer", f"Invitation mail {_how_text(done)} (visit {d.get('visit_date') or 'date in the text'}, "
                                     f"{len(paths)} attachment(s)).")
    return done


def confirm_sent(app_no: str) -> dict:
    """The HR officer sent the opened mail from their mail app: now it counts as sent."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    inv = dict(c.invite or {})
    if inv.get("status") != "opened":
        raise MailProblem("There is no opened mail waiting for confirmation.")
    now = db.now()
    history = list(inv.get("history") or [])
    for h in reversed(history):
        if h.get("status") == "opened" and not h.get("test"):
            h["status"], h["confirmed_at"] = "sent", now
            break
    inv.update(status="sent", sent_at=now, confirmed_at=now, history=history)
    db.update_candidate(c.id, invite=inv, register=_mark_mailed(c.register or {}))
    db.log_event(c.id, "HR officer", f"Confirmed the invitation mail was sent to {inv.get('sent_to') or c.email}.")
    return inv


def back_to_draft(app_no: str) -> dict:
    """The opened mail was not sent: back to a draft that can be changed and sent again."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    inv = dict(c.invite or {})
    if inv.get("status") != "opened":
        raise MailProblem("Only a mail that is open in the mail app can go back to a draft.")
    history = list(inv.get("history") or [])
    for h in reversed(history):
        if h.get("status") == "opened" and not h.get("test"):
            h["status"] = "not sent"
            break
    inv.update(status="draft", history=history)
    db.update_candidate(c.id, invite=inv)
    db.log_event(c.id, "HR officer", "Marked the invitation mail as not sent (back to a draft).")
    return inv


def resend(app_no: str) -> dict:
    """Open a sent mail again as a draft, to change it and send it again. The history is kept."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    inv = dict(c.invite or {})
    if inv.get("status") != "sent":
        raise MailProblem("Only a sent mail can be sent again.")
    inv.update(status="draft", resend_of=inv.get("sent_at", ""))
    db.update_candidate(c.id, invite=inv)
    db.log_event(c.id, "HR officer", "Opened the sent invitation mail again to resend it.")
    return inv


def open_sent_eml(app_no: str) -> dict:
    """Re-open an archived invitation .eml in Outlook Desktop or Mail app (never sends it by itself)."""
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
        launched = outlook.launch_desktop_mail(
            path,
            sender_email=outlook.raw_settings().get("sender_email", ""),
            to_email=inv.get("sent_to") or c.email or "",
            to_name=c.name or "",
            subject=inv.get("subject", ""),
            html=to_html(body) if body else "",
            text=body,
            attachments=paths,
            auto_send=False,
        )
    except outlook.OutlookError as exc:
        raise MailProblem(str(exc)) from exc
    if launched["saved_only"]:
        raise MailProblem(launched["note"] or "No mail app could open the mail. Download the .eml file instead.")
    return {"ok": True, "opened_in": launched["app"], "note": launched["note"], "file": fname}


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
            sender=effective_sender(),
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
def _row_status(c: db.Candidate) -> str:
    reg, inv = c.register or {}, c.invite or {}
    st = reg.get("status") or ""
    if inv.get("status") == "opened" and st in ("", "Not mailed"):
        return "Opened, not confirmed"
    return st or ("Mail sent" if inv.get("status") == "sent" else "Not mailed")


def row(c: db.Candidate) -> dict:
    reg = c.register or {}
    inv = c.invite or {}
    inv_status = inv.get("status") or "new"
    return {"app_no": c.app_no, "ref_no": reg.get("ref_no", ""), "mail_date": reg.get("mail_date", ""),
            "source": c.source or "", "university": c.university or "",
            "technology": reg.get("technology") or c.area or "", "name": c.name or "",
            "nic": reg.get("nic", ""), "address": reg.get("address", ""), "vehicle": reg.get("vehicle", ""),
            "tel": reg.get("tel", ""), "laptop": reg.get("laptop", ""),
            "status": _row_status(c),
            "email": c.email or "", "reading": c.status == "new", "created_at": c.created_at,
            "invite_status": inv_status, "nic_hint": nic_hint(reg.get("nic", "")),
            "test_sent": bool(inv.get("last_test")) and inv_status not in ("sent", "opened"),
            "can_mail": c.status != "new" and bool(c.email) and inv_status in ("new", "draft")}


# ---- checking the register fields -------------------------------------------------------------
_OLD_NIC = re.compile(r"^(\d{2})(\d{3})\d{4}[VX]$")
_NEW_NIC = re.compile(r"^((?:19|20)\d{2})(\d{3})\d{5}$")
_PASSPORT = re.compile(r"^[A-Z]{1,2}\d{6,8}$")
_VEHICLE = (re.compile(r"^(?:[A-Z]{2}[ -]?)?[A-Z]{2,3}[ -]?\d{4}$"), re.compile(r"^\d{2,3}[ -]?\d{4}$"))


def _nic_parts(nic: str) -> tuple[int, int] | None:
    """(birth year, day of the year) from a Sri Lankan NIC, or None when it is not a valid NIC."""
    s = re.sub(r"\s+", "", nic or "").upper()
    m = _OLD_NIC.fullmatch(s)
    if m:
        year, day = 1900 + int(m.group(1)), int(m.group(2))
    else:
        m = _NEW_NIC.fullmatch(s)
        if not m:
            return None
        year, day = int(m.group(1)), int(m.group(2))
    if day > 500:
        day -= 500
    return (year, day) if 1 <= day <= 366 else None


def nic_hint(nic: str) -> str:
    parts = _nic_parts(nic)
    return f"Birth year {parts[0]} (from the NIC)" if parts else ""


def check_nic(value: str) -> tuple[str, str, str]:
    """(cleaned value, error, warning) for the NIC / PP field."""
    s = re.sub(r"\s+", "", value or "").upper()
    if not s:
        return "", "", ""
    if re.fullmatch(r"\d+[VX]?", s):
        if _nic_parts(s):
            return s, "", ""
        return s, (f'NIC "{value}" is not valid. An old NIC is 9 digits and V or X (for example 200112345V), '
                   "a new NIC is 12 digits (for example 200112345678)."), ""
    if _PASSPORT.fullmatch(s):
        return s, "", ""
    if re.fullmatch(r"[A-Z0-9]{6,12}", s) and re.search(r"\d", s):
        return s, "", f'"{s}" was saved as a passport number. Check it.'
    return s, f'"{value}" is not a valid NIC or passport number.', ""


def _one_phone(part: str) -> tuple[str, bool]:
    raw = part.strip()
    s = re.sub(r"[\s\-().\/]", "", raw)
    m = re.fullmatch(r"(?:\+94|0094|94)?0?([1-9]\d{8})", s)
    if m:
        d = m.group(1)
        return f"0{d[:2]} {d[2:5]} {d[5:]}", True
    if re.fullmatch(r"\+[1-9]\d{7,14}", s):          # a number from another country
        return s, True
    return raw, False


def check_tel(value: str) -> tuple[str, str]:
    """(cleaned value, error). Sri Lankan numbers are written as 077 123 4567."""
    v = (value or "").strip()
    if not v:
        return "", ""
    parts = [p for p in re.split(r"[,;]|\s/\s", v) if p.strip()]
    out = []
    for p in parts:
        cleaned, ok = _one_phone(p)
        if not ok:
            return v, f'Tel No "{p.strip()}" is not a valid phone number. Use 0771234567 or +94 77 123 4567.'
        out.append(cleaned)
    return ", ".join(out), ""


def check_vehicle(value: str) -> tuple[str, str]:
    """(cleaned value, warning). Only a warning: number plates come in many forms."""
    v = re.sub(r"\s+", " ", (value or "").strip()).upper()
    if not v or v in ("-", "N/A", "NA", "NO", "NONE"):
        return (value or "").strip(), ""
    if any(rx.fullmatch(v) for rx in _VEHICLE):
        return v, ""
    return v, f'Vehicle No "{v}" does not look like a Sri Lankan number plate (for example WP CAB-1234). Check it.'


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
    """Save the register details. Returns the row with a list of warnings (nothing blocked)."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    f = {k: str(fields.get(k, "") or "").strip()[:200] for k in EDITABLE if k in fields}
    if "status" in f and f["status"] not in REGISTER_STATUSES:
        raise MailProblem("Choose a status from the list.")
    if "name" in f and not f["name"]:
        raise MailProblem("The name cannot be empty.")
    warnings: list[str] = []
    if "nic" in f:
        f["nic"], err, warn = check_nic(f["nic"])
        if err:
            raise MailProblem(err)
        if warn:
            warnings.append(warn)
    if "tel" in f:
        f["tel"], err = check_tel(f["tel"])
        if err:
            raise MailProblem(err)
    if "vehicle" in f:
        f["vehicle"], warn = check_vehicle(f["vehicle"])
        if warn:
            warnings.append(warn)
    want_ref = f.get("ref_no", "").lower()
    want_nic = f.get("nic", "")
    if want_ref or want_nic:
        with db.Session() as s:
            for other_app, other_name, other_reg in s.execute(
                db.select(db.Candidate.app_no, db.Candidate.name, db.Candidate.register)
                .where(db.Candidate.id != c.id, db.Candidate.status != "archived")
            ):
                other_reg = other_reg or {}
                if want_ref and other_reg.get("ref_no", "").strip().lower() == want_ref:
                    raise MailProblem(f"{f['ref_no']} is already used for {other_name or other_app}.")
                if want_nic and re.sub(r"\s+", "", other_reg.get("nic", "")).upper() == want_nic:
                    raise MailProblem(f"NIC / PP {want_nic} is already saved for {other_name or other_app}.")
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
    return dict(row(db.get_candidate(c.id)), warnings=warnings)


REGISTER_SHOW = ("all", "sent", "not_sent", "waiting")


def register_data(show: str = "all", q: str = "", date_from: str = "", date_to: str = "", status: str = "",
                  source: str = "") -> tuple[list[dict], dict[str, int]]:
    """Fetch register rows and filter counts in one deferred-column SQLite query.

    show: all / sent (has a mail date) / not_sent / waiting (opened, not confirmed).
    date_from / date_to filter on the mail date (YYYY-MM-DD); status and source on those columns."""
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
    waiting = sum(1 for r in all_rows if r["invite_status"] == "opened")
    counts = {"all": len(all_rows), "sent": sent_count, "not_sent": len(all_rows) - sent_count, "waiting": waiting}
    if show == "sent":
        out = [r for r in all_rows if r["mail_date"]]
    elif show == "not_sent":
        out = [r for r in all_rows if not r["mail_date"]]
    elif show == "waiting":
        out = [r for r in all_rows if r["invite_status"] == "opened"]
    else:
        out = all_rows
    for name, value in (("from", date_from), ("to", date_to)):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError as e:
                raise MailProblem(f"The '{name}' date is not valid. Use the date picker.") from e
    if date_from:
        out = [r for r in out if r["mail_date"] and r["mail_date"] >= date_from]
    if date_to:
        out = [r for r in out if r["mail_date"] and r["mail_date"] <= date_to]
    if status:
        out = [r for r in out if r["status"] == status]
    if source:
        sl = source.strip().lower()
        out = [r for r in out if sl in (r["source"] or "").lower()]
    q = (q or "").strip().lower()
    if q:
        out = [r for r in out if q in " ".join(str(r[k]) for k, _ in REGISTER_COLUMNS + [("app_no", ""), ("email", "")]).lower()]
    return out, counts


def sources() -> list[str]:
    """The different sources in the register, for the filter."""
    with db.Session() as s:
        found = s.scalars(db.select(db.Candidate.source).where(db.Candidate.source != "").distinct())
        return sorted({x.strip() for x in found if x and x.strip()}, key=str.lower)


def rows(show: str = "all", q: str = "", **filters: str) -> list[dict]:
    return register_data(show, q, **filters)[0]


_CSV_RISKY = ("=", "+", "-", "@", "\t", "\r")


def export_csv(data: list[dict]) -> bytes:
    """The register as a CSV file (UTF-8 with BOM so Excel shows Sinhala and Tamil names correctly).
    Cells that start like a formula get a leading apostrophe so a spreadsheet does not run them."""
    import csv

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in REGISTER_COLUMNS])
    for r in data:
        out = []
        for key, _ in REGISTER_COLUMNS:
            v = str(r.get(key, "") or "")
            if v.startswith(_CSV_RISKY):
                v = "'" + v
            out.append(v)
        w.writerow(out)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


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
            values.append(v)
        ws.append(values)
        for cell in ws[ws.max_row]:
            cell.border = border
            if cell.data_type == "f":           # text that starts with "=": keep it as text, never a formula
                cell.data_type = "s"
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


# ---- reading the student's reply ---------------------------------------------------------------
_REPLY_LABELS = {
    "nic": r"(?:nic|n\.i\.c|national\s+id(?:entity)?(?:\s+card)?|passport|pp)\b",
    "tel": r"(?:tel(?:ephone)?|phone|mobile|contact(?:\s+no)?|whats\s?app)\b",
    "laptop": r"(?:laptop|serial)\b",
    "address": r"(?:address|company|institute|organi[sz]ation)\b",
    "vehicle": r"(?:vehicle)\b",
}
_REPLY_PROMPT = """You read a student's email reply to an internship invitation. Find these details if the
student wrote them: their laptop serial number, their address or company, and their vehicle number.
Answer only JSON: {"laptop": "", "address": "", "vehicle": ""}. Copy each value exactly as written in the
reply. Leave a value empty when it is not in the reply. Do not guess."""


def _labelled(text: str, key: str) -> str:
    m = re.search(_REPLY_LABELS[key] + r"[^\n:=\-–]{0,30}[:=\-–]\s*(.+)", text, flags=re.I)
    return m.group(1).strip().strip(".,;") if m else ""


def read_reply(app_no: str, text: str) -> dict:
    """Suggest register details from a student's pasted reply. Nothing is saved here: the HR officer
    checks the suggestions and clicks Save. Rules find the NIC, phone and labelled lines; the model
    (when running) only adds the laptop serial, address and vehicle, and only text found in the reply."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    text = (text or "").strip()[:8000]
    if not text:
        raise MailProblem("Paste the student's reply first.")
    found: dict[str, str] = {}
    by: dict[str, str] = {}

    nic = _labelled(text, "nic")
    if not nic or check_nic(nic)[1]:
        m = re.search(r"\b(\d{9}[VvXx]|(?:19|20)\d{10})\b", text)
        nic = m.group(1) if m else ""
    if nic and not check_nic(nic)[1]:
        found["nic"], by["nic"] = check_nic(nic)[0], "rules"

    tel = _labelled(text, "tel")
    if not tel or check_tel(tel)[1]:
        m = re.search(r"(?:\+94|0094|\b0)\s?7\d[\s-]?\d{3}[\s-]?\d{4}\b", text)
        tel = m.group(0) if m else ""
    if tel and not check_tel(tel)[1]:
        found["tel"], by["tel"] = check_tel(tel)[0], "rules"

    for key in ("laptop", "address", "vehicle"):
        v = _labelled(text, key)
        if v:
            found[key], by[key] = v[:200], "rules"

    missing = [k for k in ("laptop", "address", "vehicle") if k not in found]
    notes: list[str] = []
    if missing:
        try:
            if llm.health().get("ok"):
                answer = llm.chat([{"role": "system", "content": _REPLY_PROMPT},
                                   {"role": "user", "content": f"REPLY:\n<<<\n{text}\n>>>"}], json_mode=True)
                data = checks.parse_json_object(answer)
                hay = " " + checks.norm(text) + " "
                for key in missing:
                    v = str(data.get(key) or "").strip()[:200]
                    if v and (" " + checks.norm(v) + " ") in hay:   # only text that really is in the reply
                        found[key], by[key] = v, "AI"
            else:
                notes.append("The model is not running, so only the rules were used.")
        except Exception:
            notes.append("The model could not read the reply, so only the rules were used.")
    if "vehicle" in found:
        found["vehicle"] = check_vehicle(found["vehicle"])[0]
    found["status"] = "Replied"
    db.log_event(c.id, "HR officer", f"Read the student's reply ({len(by)} detail(s) suggested).")
    return {"fields": found, "found_by": by, "notes": notes}


# ---- bulk invitations ------------------------------------------------------------------------
_bulk_lock = threading.Lock()
_bulk_jobs: dict[str, dict] = {}


def bulk_prepare(app_nos: list[str], visit_date: str, files: list[str]) -> dict:
    """Get the invitation mail of several students ready with one visit date and one set of files."""
    try:
        day = date.fromisoformat(visit_date)
    except (TypeError, ValueError) as e:
        raise MailProblem("Pick the visit date.") from e
    have = {a["name"] for a in attachments()}
    files = [f for f in dict.fromkeys(files or []) if f in have]
    results = []
    for app_no in list(dict.fromkeys(app_nos or []))[:200]:
        c = db.get_candidate(str(app_no))
        if c is None:
            results.append({"app_no": app_no, "name": "", "ok": False, "reason": "Not found."})
            continue
        name = c.name or c.app_no
        inv = c.invite or {}
        reason = ("The Recruiter is still reading this CV." if c.status == "new"
                  else "No email address yet." if not c.email or not checks.EMAIL_RE.fullmatch(c.email)
                  else "Already sent." if inv.get("status") == "sent"
                  else "Open in the mail app: confirm it first." if inv.get("status") == "opened" else "")
        if reason:
            results.append({"app_no": c.app_no, "name": name, "ok": False, "reason": reason})
            continue
        d = draft(c)
        subject, body = d["subject"], d["body"]
        new_text = visit_text(day)
        old_day = d.get("visit_date") or ""
        if old_day:
            try:
                old_text = visit_text(date.fromisoformat(old_day))
                subject, body = subject.replace(old_text, new_text), body.replace(old_text, new_text)
            except ValueError:
                pass
        subject, body = subject.replace("{visit_date}", new_text), body.replace("{visit_date}", new_text)
        save_draft(c.app_no, subject, body, day.isoformat(), files)
        results.append({"app_no": c.app_no, "name": name, "ok": True, "reason": ""})
    ready = [r["app_no"] for r in results if r["ok"]]
    mode = outlook.effective_mode()
    return {"results": results, "ready": ready, "mode": mode, "direct": mode in DIRECT_HOWS,
            "visit_date": day.isoformat(), "attachments": files}


def bulk_start(app_nos: list[str]) -> dict:
    """Send the prepared mails one after another in the background (Microsoft sign-in or SMTP only)."""
    mode = outlook.effective_mode()
    if mode not in DIRECT_HOWS:
        raise MailProblem("Sending many mails at once needs Microsoft sign-in or an SMTP password in Mail settings. "
                          "With the Outlook / Mail app, open the mails one at a time with 'Open next'.")
    todo = [str(a) for a in dict.fromkeys(app_nos or [])][:200]
    if not todo:
        raise MailProblem("Choose at least one student.")
    with _bulk_lock:
        if any(j["running"] for j in _bulk_jobs.values()):
            raise MailProblem("Mails are already being sent. Wait until they are finished.")
        job = {"id": uuid.uuid4().hex[:12], "total": len(todo), "done": 0, "sent": 0, "failed": 0,
               "running": True, "results": [], "started_at": db.now(), "mode": mode}
        _bulk_jobs.clear()
        _bulk_jobs[job["id"]] = job

    def work() -> None:
        for i, app_no in enumerate(todo):
            c = db.get_candidate(app_no)
            name = (c.name if c else "") or app_no
            try:
                res = send(app_no)
                ok, msg = True, ("test inbox" if res.get("test_only") else res.get("sent_to", ""))
            except Exception as e:          # one failure does not stop the others
                ok, msg = False, str(e)[:300]
            with _bulk_lock:
                job["results"].append({"app_no": app_no, "name": name, "ok": ok, "detail": msg})
                job["done"] += 1
                job["sent" if ok else "failed"] += 1
            if i < len(todo) - 1 and config.MAIL_BULK_DELAY_SECONDS > 0:
                time.sleep(config.MAIL_BULK_DELAY_SECONDS)     # stay under the mail service's rate limit
        with _bulk_lock:
            job["running"] = False
            job["finished_at"] = db.now()
        db.log_event(None, "HR officer", f"Bulk invitation mails: {job['sent']} sent, {job['failed']} failed.")

    threading.Thread(target=work, daemon=True, name="bulk-mail").start()
    return bulk_status(job["id"])


def bulk_status(job_id: str) -> dict:
    with _bulk_lock:
        job = _bulk_jobs.get(job_id)
        if job is None:
            raise MailProblem("That sending job is not known (the app may have restarted).")
        return {**job, "results": list(job["results"])}


# ---- shrinking old mail copies in the outbox ---------------------------------------------------
_MAIL_EML = re.compile(r"^(?:APP-\d+_invitation_\d{14}|test_invitation_\d{14})\.eml$")


def outbox_stats() -> dict:
    """The invitation mail copies (.eml) kept in data/outbox/."""
    files = [p for p in db.paths.outbox.iterdir() if p.is_file() and _MAIL_EML.fullmatch(p.name)]
    return {"files": len(files), "bytes": sum(p.stat().st_size for p in files),
            "keep_days": config.MAIL_OUTBOX_KEEP_DAYS}


def _slim_eml(path: Path) -> int:
    """Rewrite one .eml without its attachments (their names are listed in the text). Returns bytes saved."""
    raw = path.read_bytes()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if msg.get("X-HR-Slimmed"):
        return 0
    names = [part.get_filename() or "file" for part in msg.iter_attachments()]
    if not names:
        return 0
    plain = msg.get_body(preferencelist=("plain",))
    rich = msg.get_body(preferencelist=("html",))
    out = email.message.EmailMessage(policy=email.policy.default.clone(max_line_length=998))
    for h in ("From", "To", "Subject", "Date", "Message-ID"):
        if msg.get(h):
            out[h] = str(msg[h])
    out["X-HR-Slimmed"] = "1"
    note = "\n\n[Archived copy. The attachments were removed to save space: " + ", ".join(names) + "]"
    out.set_content((plain.get_content() if plain else "") + note)
    if rich is not None:
        out.add_alternative(rich.get_content() + f"<p><i>{html_lib.escape(note.strip())}</i></p>", subtype="html")
    data = out.as_bytes()
    path.write_bytes(data)
    return max(0, len(raw) - len(data))


def shrink_outbox(keep_days: int | None = None) -> dict:
    """Shrink invitation copies older than keep_days whose mail was sent (and old test mails).

    Copies of mails that are still open in the mail app, or not confirmed, are never touched."""
    keep_days = config.MAIL_OUTBOX_KEEP_DAYS if keep_days is None else keep_days
    cutoff = datetime.now() - timedelta(days=keep_days)
    cutoff_iso = cutoff.replace(microsecond=0).isoformat()
    targets: set[str] = set()
    protected: set[str] = set()
    with db.Session() as s:
        for inv in s.scalars(db.select(db.Candidate.invite)):
            inv = inv or {}
            if inv.get("status") != "sent" and inv.get("file"):
                protected.add(inv["file"])
            for h in inv.get("history") or []:
                if h.get("file") and h.get("status") == "sent" and (h.get("confirmed_at") or h.get("at") or "") < cutoff_iso:
                    targets.add(h["file"])
            if inv.get("status") == "sent" and inv.get("file") and (inv.get("sent_at") or "") < cutoff_iso:
                targets.add(inv["file"])
    for p in db.paths.outbox.iterdir():
        if p.name.startswith("test_invitation_") and _MAIL_EML.fullmatch(p.name) \
                and datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
            targets.add(p.name)
    shrunk, saved = 0, 0
    for name in sorted(targets - protected):
        if not _MAIL_EML.fullmatch(name):
            continue
        path = db.paths.outbox / name
        if not path.is_file():
            continue
        try:
            n = _slim_eml(path)
        except Exception:
            continue
        if n:
            shrunk, saved = shrunk + 1, saved + n
    return {"shrunk": shrunk, "saved_bytes": saved, **outbox_stats()}


# ---- adding a student by hand (no CV, or the model is not running) -----------------------------
def _hand_fields(fields: dict) -> dict:
    out = {k: str(fields.get(k, "") or "").strip()[:200] for k in ("name", "email", "university", "technology", "source")}
    if not out["name"]:
        raise MailProblem("Enter the student's name.")
    if not checks.EMAIL_RE.fullmatch(out["email"]):
        raise MailProblem("Enter a valid email address for the student.")
    return out


def _email_taken(email_addr: str, except_id: int | None = None) -> str:
    with db.Session() as s:
        q = db.select(db.Candidate.app_no, db.Candidate.name).where(
            db.func.lower(db.Candidate.email) == email_addr.lower(), db.Candidate.status != "archived")
        if except_id is not None:
            q = q.where(db.Candidate.id != except_id)
        hit = s.execute(q).first()
    return f"{hit[1] or hit[0]} ({hit[0]})" if hit else ""


def add_by_hand(fields: dict) -> dict:
    """A student without a CV file: typed in by the HR officer, ready for the invitation mail."""
    f = _hand_fields(fields)
    taken = _email_taken(f["email"])
    if taken:
        raise MailProblem(f"{f['email']} is already in the register as {taken}.")
    app_no = db.next_app_no()
    now = db.now()
    with db.Session() as s:
        c = db.Candidate(app_no=app_no, name=f["name"], email=f["email"], email_source="Typed by HR officer",
                         cv_filename="", cv_stored="", cv_hash="", cv_text="", source=f["source"],
                         university=f["university"], status="in_pool", profile={}, profile_source="Typed by HR officer",
                         register={"technology": f["technology"]} if f["technology"] else {},
                         area_reasons=[], area_evidence=[], score_parts={}, gaps=[], questions=[], checklist=[],
                         flags=[], created_at=now, updated_at=now)
        s.add(c)
        s.commit()
        cid = c.id
    db.log_event(cid, "HR officer", "Added the student by hand (no CV file).")
    return {"ok": True, "app_no": app_no}


def fill_by_hand(app_no: str, fields: dict) -> dict:
    """Type the details of a CV the Recruiter has not read (for example when the model is not running)."""
    c = db.get_candidate(app_no)
    if c is None:
        raise MailProblem("Candidate not found.")
    if c.status not in ("new", "needs_person"):
        raise MailProblem("This CV has already been read. Change the details in the Register box instead.")
    f = _hand_fields(fields)
    taken = _email_taken(f["email"], except_id=c.id)
    if taken:
        raise MailProblem(f"{f['email']} is already in the register as {taken}.")
    reg = dict(c.register or {})
    if f["technology"]:
        reg["technology"] = f["technology"]
    update = {"name": f["name"], "email": f["email"], "email_source": "Typed by HR officer",
              "university": f["university"], "profile_source": "Typed by HR officer", "register": reg,
              "status": "in_pool"}
    if f["source"]:
        update["source"] = f["source"]
    if not db.update_candidate(c.id, expect_status=c.status, **update):
        raise MailProblem("The Recruiter has just finished reading this CV. Reload the page.")
    db.log_event(c.id, "HR officer", "Typed the student's details by hand (the CV was not read by the AI).")
    return {"ok": True, "app_no": c.app_no}


def email_in_cv(c: db.Candidate) -> str:
    """The first email address written in the CV text, to fill the by-hand form."""
    m = checks.EMAIL_RE.search(c.cv_text or "")
    return m.group(0) if m else ""
