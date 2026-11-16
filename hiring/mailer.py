"""Candidate email dispatcher: configurable SMTP sender with HTML/text templates and demo safety.

Adapts the open-source ATS SMTP notification pattern (OpenCATS / FastAPI-Mail) cleanly into
HR Hiring's human-in-the-loop release flow:
- Released emails are always archived in ``data/outbox/`` for auditability.
- When SMTP is disabled, ``send_email`` reports ``outbox_only`` without making network calls.
- When ``test_recipient`` is set (Demo Override), outbound mail is redirected to that inbox
  so sample CVs using ``@example.com`` can be safely delivered live during demonstrations.
"""
from __future__ import annotations

import html
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any, Callable

from . import checks, config, db

_backend: Callable[[EmailMessage, dict[str, Any]], str] | None = None

SETTING_KEYS = {
    "enabled": "smtp_enabled",
    "host": "smtp_host",
    "port": "smtp_port",
    "user": "smtp_user",
    "password": "smtp_password",
    "use_tls": "smtp_use_tls",
    "use_ssl": "smtp_use_ssl",
    "from_name": "smtp_from_name",
    "from_email": "smtp_from_email",
    "test_recipient": "smtp_test_recipient",
}


def set_backend(fn: Callable[[EmailMessage, dict[str, Any]], str] | None) -> None:
    """Inject a test transport ``fn(msg, settings) -> message_id`` (no network calls)."""
    global _backend
    _backend = fn


def _db_get(key: str) -> str:
    if db._engine is None:
        return ""
    return db.get_setting(key, "")


def _as_bool(raw: str, default: bool) -> bool:
    if not raw:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def raw_settings() -> dict[str, Any]:
    """Return effective SMTP settings merging environment defaults with persisted SQLite overrides."""
    port_raw = _db_get(SETTING_KEYS["port"])
    try:
        port = int(port_raw) if port_raw else int(config.SMTP_PORT)
    except ValueError:
        port = int(config.SMTP_PORT)
    user = (_db_get(SETTING_KEYS["user"]) or config.SMTP_USER).strip()
    from_email = (_db_get(SETTING_KEYS["from_email"]) or config.SMTP_FROM_EMAIL or user).strip()
    return {
        "enabled": _as_bool(_db_get(SETTING_KEYS["enabled"]), config.SMTP_ENABLED),
        "host": (_db_get(SETTING_KEYS["host"]) or config.SMTP_HOST).strip(),
        "port": max(1, min(port, 65535)),
        "user": user,
        "password": _db_get(SETTING_KEYS["password"]) or config.SMTP_PASSWORD,
        "use_tls": _as_bool(_db_get(SETTING_KEYS["use_tls"]), config.SMTP_USE_TLS),
        "use_ssl": _as_bool(_db_get(SETTING_KEYS["use_ssl"]), config.SMTP_USE_SSL),
        "from_name": (_db_get(SETTING_KEYS["from_name"]) or config.SMTP_FROM_NAME).strip() or "SLT Talent Acquisition",
        "from_email": from_email,
        "test_recipient": (_db_get(SETTING_KEYS["test_recipient"]) or config.SMTP_TEST_RECIPIENT).strip(),
    }


def public_settings() -> dict[str, Any]:
    """Return UI/API-safe SMTP configuration without exposing raw password secrets."""
    cfg = raw_settings()
    return {
        "enabled": cfg["enabled"],
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "has_password": bool(cfg["password"]),
        "use_tls": cfg["use_tls"],
        "use_ssl": cfg["use_ssl"],
        "from_name": cfg["from_name"],
        "from_email": cfg["from_email"],
        "test_recipient": cfg["test_recipient"],
        "mode": "smtp_demo_override" if (cfg["enabled"] and cfg["test_recipient"])
        else ("smtp_live" if cfg["enabled"] else "outbox_only"),
    }


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist SMTP settings in the SQLite settings table."""
    if "port" in payload and payload["port"] not in ("", None):
        try:
            port = int(payload["port"])
        except (TypeError, ValueError) as exc:
            raise ValueError("SMTP port must be a valid integer.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535.")
        db.set_setting(SETTING_KEYS["port"], str(port))

    for bool_field in ("enabled", "use_tls", "use_ssl"):
        if bool_field in payload:
            db.set_setting(SETTING_KEYS[bool_field], "1" if payload[bool_field] else "0")

    for str_field in ("host", "user", "from_name"):
        if str_field in payload and payload[str_field] is not None:
            db.set_setting(SETTING_KEYS[str_field], str(payload[str_field]).strip()[:200])

    for email_field in ("from_email", "test_recipient"):
        if email_field in payload and payload[email_field] is not None:
            val = str(payload[email_field]).strip()[:200]
            if val and not checks.EMAIL_RE.fullmatch(val):
                raise ValueError(f'"{val}" is not a valid email address.')
            db.set_setting(SETTING_KEYS[email_field], val)

    if payload.get("clear_password"):
        db.set_setting(SETTING_KEYS["password"], "")
    elif payload.get("password"):
        db.set_setting(SETTING_KEYS["password"], str(payload["password"]))

    return public_settings()


def _render_html(subject: str, body: str, meta: dict[str, Any], redirect_banner: str = "") -> str:
    paragraphs = "".join(
        f"<p style=\"margin:0 0 12px;line-height:1.6;\">{'<br>'.join(html.escape(line) for line in block.splitlines())}</p>"
        for block in body.strip().split("\n\n") if block.strip()
    )
    badges = []
    if meta.get("app_no"):
        badges.append(f"Ref: {html.escape(str(meta['app_no']))}")
    if meta.get("area"):
        badges.append(f"Area: {html.escape(str(meta['area']))}")
    if meta.get("work_id"):
        badges.append(f"Work ID: {html.escape(str(meta['work_id']))}")
    badge_html = (
        f"<div style=\"margin-bottom:14px;font-size:12px;color:#555;\">{' &bull; '.join(badges)}</div>"
        if badges else ""
    )
    banner_html = (
        f"<div style=\"background:#fff4d6;border:1px solid #e3b448;border-radius:4px;padding:8px 12px;"
        f"margin-bottom:14px;font-size:12.5px;color:#7a5300;\">{html.escape(redirect_banner)}</div>"
        if redirect_banner else ""
    )
    return (
        "<!doctype html><html><body style=\"margin:0;padding:20px;background:#f5f7fa;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#222;\">"
        "<div style=\"max-width:600px;margin:0 auto;background:#fff;border:1px solid #ddd;border-radius:6px;overflow:hidden;\">"
        "<div style=\"background:#1a5fb4;color:#fff;padding:14px 20px;font-weight:600;font-size:15px;\">"
        f"{html.escape(subject)}</div>"
        f"<div style=\"padding:20px;font-size:14px;\">{banner_html}{badge_html}{paragraphs}</div>"
        "<div style=\"background:#f7f7f7;border-top:1px solid #eee;padding:12px 20px;font-size:12px;color:#666;\">"
        "Sent by HR Hiring &mdash; Candidate Notification Pipeline"
        "</div></div></body></html>"
    )


def build_message(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    cfg: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> tuple[EmailMessage, str, str]:
    """Construct plain-text + HTML EmailMessage, applying Demo Override if configured."""
    meta = dict(meta or {})
    override = (cfg.get("test_recipient") or "").strip()
    actual_to = override if override else to_email.strip()
    redirect_banner = ""
    effective_subject = subject.strip()
    plain_body = body.strip()
    if override and override.lower() != to_email.strip().lower():
        redirect_banner = f"[DEMO OVERRIDE] Intended candidate recipient: {to_name} <{to_email}>"
        effective_subject = f"[DEMO] {effective_subject}"
        plain_body = f"{redirect_banner}\n\n{plain_body}"

    from_addr = cfg.get("from_email") or cfg.get("user") or "noreply@localhost"
    domain = from_addr.split("@", 1)[-1] if "@" in from_addr else "localhost"
    msg_id = make_msgid(domain=domain)

    msg = EmailMessage()
    msg["Subject"] = effective_subject
    msg["From"] = formataddr((cfg.get("from_name") or "SLT Talent Acquisition", from_addr))
    msg["To"] = formataddr((to_name or actual_to, actual_to))
    msg["Message-ID"] = msg_id
    msg.set_content(plain_body)
    msg.add_alternative(_render_html(subject.strip(), body, meta, redirect_banner), subtype="html")
    return msg, actual_to, msg_id


def _deliver_smtp(msg: EmailMessage, cfg: dict[str, Any]) -> str:
    if _backend is not None:
        return _backend(msg, cfg) or msg["Message-ID"]
    host = cfg["host"]
    port = int(cfg["port"])
    if not host:
        raise ValueError("SMTP host is not configured.")
    timeout = config.SMTP_TIMEOUT
    if cfg.get("use_ssl"):
        with smtplib.SMTP_SSL(host, port, timeout=timeout) as server:
            if cfg.get("user") and cfg.get("password"):
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            if cfg.get("use_tls"):
                server.starttls()
                server.ehlo()
            if cfg.get("user") and cfg.get("password"):
                server.login(cfg["user"], cfg["password"])
            server.send_message(msg)
    return msg["Message-ID"]


def send_email(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    meta: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Dispatch a candidate email via configured SMTP, or return ``outbox_only`` if disabled."""
    cfg = raw_settings()
    if not force and not cfg["enabled"]:
        return {"ok": True, "status": "outbox_only", "sent_to": "", "message_id": "", "error": ""}

    is_probe = (meta or {}).get("app_no") == "SMTP-TEST"
    if config.DEMO_EMAIL_LOCK and not is_probe and not (cfg.get("test_recipient") or "").strip():
        return {
            "ok": False,
            "status": "failed",
            "sent_to": to_email.strip(),
            "message_id": "",
            "error": "Demo email lock is active (HR_DEMO_EMAIL_LOCK=1): set Demo Recipient Override first.",
        }

    msg, actual_to, msg_id = build_message(to_email, to_name, subject, body, cfg, meta=meta)
    try:
        delivered_id = _deliver_smtp(msg, cfg)
        return {
            "ok": True,
            "status": "sent",
            "sent_to": actual_to,
            "message_id": delivered_id or msg_id,
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "sent_to": actual_to,
            "message_id": msg_id,
            "error": f"{type(exc).__name__}: {exc}"[:400],
        }


def send_test_email(to_email: str) -> dict[str, Any]:
    """Send a diagnostic verification email using the saved SMTP settings (even before global enable)."""
    target = (to_email or "").strip()
    if not checks.EMAIL_RE.fullmatch(target):
        raise ValueError("Provide a valid recipient email address for the test email.")
    return send_email(
        to_email=target,
        to_name="HR Reviewer",
        subject="HR Hiring SMTP Test - Candidate Email Pipeline Ready",
        body=(
            "Hello,\n\n"
            "This test confirms that HR Hiring's candidate email notification module can "
            "deliver live HTML and plain-text emails through your configured SMTP server.\n\n"
            "Kind regards,\nHiring Team"
        ),
        meta={"app_no": "SMTP-TEST", "area": "System Check"},
        force=True,
    )
