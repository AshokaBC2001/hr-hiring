"""Sending a finished mail. This is the handover point for the Outlook sending part.

Everything before this (reading the CV, filling the mail, the visit date, the attachments
and the preview) is done elsewhere. This file only receives a finished mail and sends it.

    send_mail(to, subject, html, text, attachments)  ->  {"how": ..., "file": ..., "message_id": ...}

Today it does not send anything: it saves the mail as an .eml file in data/outbox, so the
whole flow can be tested. Double-click the file to open it in Mail or Outlook.

To send through Outlook, replace the body of send_mail() (or call your Outlook code from it)
and keep the same arguments and the same kind of answer. Raise MailError with a short,
plain message if sending fails; the HR officer sees that message and the mail stays unsent.

Note for Microsoft Graph: a single request may not be larger than 4 MB. The attachments grow
by about a third when encoded, so with the user manual (about 3 MB) the mail is too big for one
request. Create the mail as a draft, add each attachment (an upload session for files over 3 MB)
and then send the draft. Office 365 SMTP does not have this limit.
"""
from __future__ import annotations

import mimetypes
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from . import db


class MailError(RuntimeError):
    """The mail could not be sent. The message is shown to the HR officer."""


def build_message(sender: str, to: str, subject: str, html: str, text: str,
                  attachments: list[Path]) -> EmailMessage:
    """One email with a plain-text part, an HTML part and the attached files."""
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="hr-hiring.local")
    msg["X-Unsent"] = "1"                         # Outlook opens it as a mail ready to send
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for path in attachments:
        kind, _ = mimetypes.guess_type(path.name)
        main, sub = (kind or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
    return msg


def send_mail(to: str, subject: str, html: str, text: str, attachments: list[Path],
              sender: str = "", name_hint: str = "mail") -> dict:
    """Sends one finished mail. Today: saved as an .eml file in data/outbox (nothing is sent)."""
    missing = [p.name for p in attachments if not p.exists()]
    if missing:
        raise MailError(f"An attachment is missing: {', '.join(missing)}. Upload it again in Mail settings.")
    msg = build_message(sender, to, subject, html, text, attachments)
    fname = re.sub(r"[^A-Za-z0-9_-]", "", name_hint)[:60] + ".eml"
    (db.paths.outbox / fname).write_bytes(msg.as_bytes())
    return {"how": "eml", "file": fname, "message_id": msg["Message-ID"]}
