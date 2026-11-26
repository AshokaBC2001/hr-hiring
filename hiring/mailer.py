"""Sending a finished mail through Microsoft Outlook (Graph API, Office 365 SMTP, or .eml archive).

Everything before this (reading the CV, filling the mail, the visit date, the attachments
and the preview) is done elsewhere. This file receives a finished mail and dispatches it
according to active Outlook settings in ``hiring.outlook``:

- ``eml`` (default): archives a ready-to-open ``.eml`` (``X-Unsent: 1``) in ``data/outbox/``.
- ``graph`` / ``graph_send``: creates an Outlook draft, uploads every attachment one by one
  (using ``createUploadSession`` in 320 KiB-aligned chunks for files >= 3 MiB), and sends it.
  If ``smtp_fallback`` is enabled with Office 365 SMTP credentials, falls back automatically.
- ``graph_draft``: creates the multi-attachment draft in the user's Outlook Drafts folder.
- ``smtp``: sends directly over Office 365 STARTTLS SMTP (``smtp.office365.com:587``).

In all non-``.eml`` modes, sending is still archived locally as an ``.eml`` file on success,
while any failure raises ``MailError`` so the candidate register stays unsent.
"""
from __future__ import annotations

import email.policy
import mimetypes
import re
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path

from . import checks, db, outlook

# RFC 5322 permits 998-char header lines; avoids Python's 78-char folder adding a leading space on long subjects.
_EMAIL_POLICY = email.policy.default.clone(max_line_length=998)


class MailError(RuntimeError):
    """The mail could not be sent. The message is shown to the HR officer."""


def _safe_header(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def _format_address(raw: str) -> str:
    """Parse and safely format a 'Display Name <addr@host>' header value."""
    name, addr = parseaddr(_safe_header(raw))
    if not addr:
        return _safe_header(raw)
    return formataddr((name.strip(), addr.strip())) if name.strip() else addr.strip()


def build_message(
    sender: str,
    to: str,
    subject: str,
    html: str,
    text: str,
    attachments: list[Path],
    *,
    unsent: bool = True,
) -> EmailMessage:
    """One email with a plain-text part, an HTML part and the attached files."""
    msg = EmailMessage(policy=_EMAIL_POLICY)
    if sender:
        msg["From"] = _format_address(sender)
    msg["To"] = _format_address(to)
    msg["Subject"] = _safe_header(subject)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="hr-hiring.local")
    if unsent:
        msg["X-Unsent"] = "1"                     # Outlook opens it as a mail ready to send
        msg["X-Uniform-Type-Identifier"] = "com.apple.mail-draft"
    msg.set_content(text or "")
    msg.add_alternative(html or "", subtype="html")
    for path in attachments:
        kind, _ = mimetypes.guess_type(path.name)
        main, sub = (kind or "application/octet-stream").split("/", 1)
        msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=_safe_header(path.name))
    return msg


def _save_eml(
    sender: str,
    to_header: str,
    subject: str,
    html: str,
    text: str,
    attachments: list[Path],
    name_hint: str,
) -> tuple[str, str]:
    msg = build_message(sender, to_header, subject, html, text, attachments, unsent=True)
    fname = (re.sub(r"[^A-Za-z0-9_-]", "", name_hint)[:60] or "mail") + ".eml"
    (db.paths.outbox / fname).write_bytes(msg.as_bytes())
    return fname, str(msg["Message-ID"])


def _send_via_smtp(
    sender_display: str,
    to_name: str,
    recipient_email: str,
    subject: str,
    html: str,
    text: str,
    attachments: list[Path],
    cfg: dict,
) -> dict:
    smtp_sender = cfg.get("smtp_user") or ""
    if not smtp_sender or not cfg.get("smtp_password"):
        raise MailError("Enter your Office 365 email and password/app password in Outlook settings first.")
    from_hdr = formataddr((_safe_header(sender_display or cfg.get("from_name") or "HR Hiring Team"), smtp_sender))
    to_hdr = formataddr((to_name, recipient_email)) if to_name else recipient_email
    msg = build_message(from_hdr, to_hdr, subject, html, text, attachments, unsent=False)
    return outlook.send_smtp_mime_message(msg, recipient_email, cfg)


def send_mail(
    to: str,
    subject: str,
    html: str,
    text: str,
    attachments: list[Path],
    sender: str = "",
    name_hint: str = "mail",
) -> dict:
    """Send a finished mail via active Outlook configuration and archive a local .eml copy."""
    missing = [p.name for p in attachments if not p.exists()]
    if missing:
        raise MailError(f"An attachment is missing: {', '.join(missing)}. Upload it again in Mail settings.")

    to_name, parsed_email = parseaddr(_safe_header(to))
    cfg = outlook.raw_settings()
    recipient_email = _safe_header(cfg.get("test_recipient") or parsed_email or to)
    if not checks.EMAIL_RE.fullmatch(recipient_email):
        raise MailError(f'Recipient email "{recipient_email}" is not a valid email address.')

    to_header = formataddr((to_name.strip(), recipient_email)) if to_name.strip() else recipient_email
    sender_name, _ = parseaddr(_safe_header(sender))
    mode = cfg.get("mode", "eml")

    if mode in ("eml", "outlook_app"):
        fname, mid = _save_eml(sender, to_header, subject, html, text, attachments, name_hint)
        if mode == "outlook_app":
            try:
                opened_in = outlook.open_eml_in_desktop_app(
                    db.paths.outbox / fname,
                    to_email=recipient_email,
                    to_name=to_name.strip(),
                    subject=_safe_header(subject),
                    html=html,
                    text=text,
                    attachments=attachments,
                )
            except outlook.OutlookError as exc:
                raise MailError(str(exc)) from exc
            return {
                "how": "outlook_app",
                "file": fname,
                "message_id": mid,
                "sent_to": recipient_email,
                "sender": sender,
                "opened_in": opened_in,
            }
        return {"how": "eml", "file": fname, "message_id": mid, "sent_to": recipient_email, "sender": sender}

    if mode in ("graph", "graph_send", "graph_draft"):
        send_now = mode != "graph_draft"
        res = outlook.send_graph_mail_stepwise(
            recipient_email,
            to_name,
            subject,
            html,
            attachments,
            send=send_now,
            cfg=cfg,
        )
        if res.get("ok"):
            acct = res.get("sender") or ""
            eml_sender = formataddr((sender_name or cfg.get("from_name") or "HR Hiring Team", acct)) if acct else sender
            fname, _ = _save_eml(eml_sender, to_header, subject, html, text, attachments, name_hint)
            return {
                "how": res["how"],
                "file": fname,
                "message_id": res.get("message_id", ""),
                "web_link": res.get("web_link", ""),
                "sender": acct,
                "sent_to": res.get("sent_to", recipient_email),
            }

        graph_err = res.get("error") or "Microsoft Graph mail delivery failed."
        if send_now and cfg.get("smtp_fallback") and cfg.get("smtp_user") and cfg.get("smtp_password"):
            smtp_res = _send_via_smtp(
                sender_name, to_name, recipient_email, subject, html, text, attachments, cfg
            )
            if smtp_res.get("ok"):
                eml_sender = formataddr((sender_name or cfg.get("from_name") or "HR Hiring Team", cfg["smtp_user"]))
                fname, _ = _save_eml(eml_sender, to_header, subject, html, text, attachments, name_hint)
                return {
                    "how": "smtp",
                    "file": fname,
                    "message_id": smtp_res.get("message_id", ""),
                    "sender": cfg["smtp_user"],
                    "sent_to": recipient_email,
                    "fallback_from": "graph",
                }
            raise MailError(
                f"Outlook Graph failed ({graph_err}) and Office 365 SMTP fallback failed ({smtp_res.get('error', '')})."
            )
        raise MailError(graph_err)

    if mode == "smtp":
        smtp_res = _send_via_smtp(sender_name, to_name, recipient_email, subject, html, text, attachments, cfg)
        if not smtp_res.get("ok"):
            raise MailError(smtp_res.get("error") or "Office 365 SMTP delivery failed.")
        eml_sender = formataddr((sender_name or cfg.get("from_name") or "HR Hiring Team", cfg["smtp_user"]))
        fname, _ = _save_eml(eml_sender, to_header, subject, html, text, attachments, name_hint)
        return {
            "how": "smtp",
            "file": fname,
            "message_id": smtp_res.get("message_id", ""),
            "sender": cfg["smtp_user"],
            "sent_to": recipient_email,
        }

    raise MailError(f"Unsupported Outlook mode '{mode}'.")
