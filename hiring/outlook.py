"""Microsoft Outlook email integration using MSAL + Microsoft Graph REST API v1.0.

Combines four Outlook workflows for an HR team and supervisor on Microsoft 365 / Outlook:
1. Microsoft Graph Drafts (`mode="graph_draft"`): Authenticates via Microsoft's official
   `AzureAD/microsoft-authentication-library-for-python` (`msal`, MIT) using OAuth2 Device Code Flow
   with persistent token refresh, and creates a real draft in the user's Outlook Drafts folder
   (`POST /v1.0/me/messages`, attaching any Placement Letter PDF) returning its `webLink`.
2. Microsoft Graph Direct Send (`mode="graph_send"`): Sends directly from the signed-in Outlook
   account (`POST /v1.0/me/sendMail`) with `saveToSentItems: true` and inline PDF attachments.
3. Office 365 SMTP (`mode="smtp"`): Optional `smtp.office365.com:587` STARTTLS fallback.
4. Zero-Setup Outlook Desktop & Web (`mode="eml"`, always saved on release): Writes an RFC 5322
   `.eml` file (`X-Unsent: 1`, with PDF attached) that opens ready-to-send in Outlook Desktop, and
   generates `outlook.office.com` / `outlook.live.com` compose deep links.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import quote, urlencode

import certifi
import httpx
import msal

from . import checks, config, db

VALID_MODES = ("eml", "outlook_app", "graph", "graph_draft", "graph_send", "smtp")
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send"]
MAX_GRAPH_ATTACHMENT_BYTES = 3 * 1024 * 1024
UPLOAD_SESSION_MIN_BYTES = 3 * 1024 * 1024     # 3 MiB Graph minimum for createUploadSession
UPLOAD_CHUNK_BYTES = 10 * 327_680              # 3,276,800 bytes (exact multiple of 320 KiB)
MAX_UPLOAD_SESSION_BYTES = 150 * 1024 * 1024   # 150 MiB Graph maximum per attachment
_URL_BODY_MAX = 1500
_TENANT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class OutlookError(Exception):
    """Raised when Outlook configuration, authentication, or Graph delivery fails."""


class OutlookAuthRequired(OutlookError):
    """Raised when Microsoft Graph delivery is selected without an active Microsoft sign-in."""


class _GraphPayloadTooLarge(OutlookError):
    """Raised on HTTP 413 so inline attachment requests can fall back where allowed."""


_http_lock = threading.Lock()
_http_client: httpx.Client | None = None
_graph_backend: Callable[[str, str, dict[str, Any], str, dict[str, Any]], dict[str, Any]] | None = None
_smtp_backend: Callable[[EmailMessage, dict[str, Any]], str] | None = None
_msal_factory: Callable[[str, str, msal.SerializableTokenCache], Any] | None = None
_desktop_launcher: Callable[..., str] | None = None

_auth_lock = threading.Lock()
_auth_state: dict[str, Any] = {
    "status": "idle",        # idle / pending / signed_in / error
    "user_code": "",
    "verification_uri": "",
    "message": "",
    "expires_at": 0.0,
    "error": "",
    "account": None,
}


def set_graph_backend(
    fn: Callable[[str, str, dict[str, Any], str, dict[str, Any]], dict[str, Any]] | None,
) -> None:
    """Inject a Graph transport stub in tests: ``fn(method, url, payload, token, cfg) -> dict``."""
    global _graph_backend
    _graph_backend = fn


def set_smtp_backend(fn: Callable[[EmailMessage, dict[str, Any]], str] | None) -> None:
    """Inject an SMTP transport stub in tests: ``fn(msg, cfg) -> message_id``."""
    global _smtp_backend
    _smtp_backend = fn


def set_msal_factory(fn: Callable[[str, str, msal.SerializableTokenCache], Any] | None) -> None:
    """Inject a custom MSAL PublicClientApplication factory in tests."""
    global _msal_factory
    _msal_factory = fn


def set_desktop_launcher(fn: Callable[..., str] | None) -> None:
    """Inject a desktop mail launcher stub in tests: ``fn(eml_path, **kwargs) -> app_name``."""
    global _desktop_launcher
    _desktop_launcher = fn


def reset_auth_state() -> None:
    """Reset in-memory device flow tracking (useful when changing apps or in test cleanup)."""
    with _auth_lock:
        _auth_state.update(
            status="idle",
            user_code="",
            verification_uri="",
            message="",
            expires_at=0.0,
            error="",
            account=None,
        )


def close() -> None:
    """Close shared Graph HTTP client pool."""
    global _http_client
    with _http_lock:
        if _http_client is not None:
            _http_client.close()
        _http_client = None


def _get_http_client() -> httpx.Client:
    global _http_client
    with _http_lock:
        if _http_client is None:
            _http_client = httpx.Client(
                timeout=httpx.Timeout(config.OUTLOOK_TIMEOUT, connect=5.0),
                limits=httpx.Limits(max_connections=6, max_keepalive_connections=4),
            )
        return _http_client


def _safe_header(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()


def _parse_bool(val: str | bool | None, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if val is None or val == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


# ---- Settings stored in SQLite (overriding config.py defaults) ----------------------
def raw_settings() -> dict[str, Any]:
    """Return full Outlook settings (including secrets for internal use only)."""
    mode = (db.get_setting("outlook_mode") or config.OUTLOOK_MODE or "eml").strip().lower()
    if mode not in VALID_MODES:
        mode = "eml"
    port_raw = db.get_setting("outlook_smtp_port")
    try:
        smtp_port = int(port_raw) if port_raw else int(config.OUTLOOK_SMTP_PORT)
    except (TypeError, ValueError):
        smtp_port = 587
    client_id = db.get_setting("outlook_client_id").strip() or config.OUTLOOK_CLIENT_ID
    tenant_id = db.get_setting("outlook_tenant_id").strip() or config.OUTLOOK_TENANT_ID or "common"
    from_name = db.get_setting("outlook_from_name").strip() or config.OUTLOOK_FROM_NAME
    test_recipient = db.get_setting("outlook_test_recipient").strip() or config.OUTLOOK_TEST_RECIPIENT
    smtp_host = db.get_setting("outlook_smtp_host").strip() or config.OUTLOOK_SMTP_HOST
    smtp_user = db.get_setting("outlook_smtp_user").strip() or config.OUTLOOK_SMTP_USER
    smtp_password = db.get_setting("outlook_smtp_password") or config.OUTLOOK_SMTP_PASSWORD
    smtp_use_tls = _parse_bool(db.get_setting("outlook_smtp_use_tls"), config.OUTLOOK_SMTP_USE_TLS)
    smtp_fallback = _parse_bool(db.get_setting("outlook_smtp_fallback"), config.OUTLOOK_SMTP_FALLBACK)
    return {
        "mode": mode,
        "client_id": client_id,
        "tenant_id": tenant_id,
        "from_name": from_name,
        "test_recipient": test_recipient,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "smtp_use_tls": smtp_use_tls,
        "smtp_fallback": smtp_fallback,
    }


def public_settings() -> dict[str, Any]:
    """Return UI-safe Outlook settings without secrets or cached tokens."""
    cfg = raw_settings()
    acct = get_signed_in_account(cfg)
    with _auth_lock:
        auth_view = dict(_auth_state)
        if acct:
            auth_view["status"] = "signed_in"
            auth_view["account"] = acct
    return {
        "mode": cfg["mode"],
        "client_id": cfg["client_id"],
        "has_client_id": bool(cfg["client_id"]),
        "tenant_id": cfg["tenant_id"],
        "from_name": cfg["from_name"],
        "test_recipient": cfg["test_recipient"],
        "smtp_host": cfg["smtp_host"],
        "smtp_port": cfg["smtp_port"],
        "smtp_user": cfg["smtp_user"],
        "has_smtp_password": bool(cfg["smtp_password"]),
        "smtp_use_tls": cfg["smtp_use_tls"],
        "smtp_fallback": cfg["smtp_fallback"],
        "signed_in": acct is not None,
        "account": acct,
        "auth": auth_view,
    }


def save_settings(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist Outlook configuration in SQLite settings."""
    if not isinstance(payload, dict):
        raise ValueError("Invalid settings payload.")
    cur = raw_settings()

    mode = str(payload.get("mode", cur["mode"])).strip().lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported Outlook mode '{mode}'. Choose one of: {', '.join(VALID_MODES)}.")

    client_id = str(payload.get("client_id", cur["client_id"])).strip()[:120]
    tenant_id = str(payload.get("tenant_id", cur["tenant_id"])).strip()[:120] or "common"
    if not _TENANT_RE.fullmatch(tenant_id):
        raise ValueError("Tenant ID must contain only letters, numbers, dots, or hyphens (e.g. 'common' or directory GUID).")

    from_name = str(payload.get("from_name", cur["from_name"])).strip()[:120] or "HR Hiring Team"
    test_recipient = str(payload.get("test_recipient", cur["test_recipient"])).strip()[:200]
    if test_recipient and not checks.EMAIL_RE.fullmatch(test_recipient):
        raise ValueError(f'Demo redirect email "{test_recipient}" is not a valid email address.')

    smtp_host = str(payload.get("smtp_host", cur["smtp_host"])).strip()[:120] or "smtp.office365.com"
    smtp_port_raw = payload.get("smtp_port", cur["smtp_port"])
    try:
        smtp_port = int(smtp_port_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP port must be a valid integer (usually 587).") from exc
    if not 1 <= smtp_port <= 65535:
        raise ValueError("SMTP port must be between 1 and 65535.")

    smtp_user = str(payload.get("smtp_user", cur["smtp_user"])).strip()[:200]
    if smtp_user and not checks.EMAIL_RE.fullmatch(smtp_user):
        raise ValueError(f'Outlook SMTP username "{smtp_user}" must be a valid email address.')

    if "smtp_password" in payload and payload["smtp_password"] is not None:
        new_pw = str(payload["smtp_password"])
        smtp_password = new_pw if new_pw else cur["smtp_password"]
    else:
        smtp_password = cur["smtp_password"]
    if payload.get("clear_smtp_password"):
        smtp_password = ""

    smtp_use_tls = _parse_bool(payload.get("smtp_use_tls"), cur["smtp_use_tls"])
    smtp_fallback = _parse_bool(payload.get("smtp_fallback"), cur["smtp_fallback"])

    if mode in ("graph", "graph_draft", "graph_send") and not client_id:
        raise ValueError("Enter the Azure / Entra Application (Client) ID before choosing Microsoft Graph mode.")
    if mode == "smtp" and (not smtp_user or not smtp_password):
        raise ValueError("Enter your Outlook email and password/app password before enabling SMTP mode.")

    if client_id != cur["client_id"] or tenant_id != cur["tenant_id"]:
        reset_auth_state()

    db.set_setting("outlook_mode", mode)
    db.set_setting("outlook_client_id", client_id)
    db.set_setting("outlook_tenant_id", tenant_id)
    db.set_setting("outlook_from_name", from_name)
    db.set_setting("outlook_test_recipient", test_recipient)
    db.set_setting("outlook_smtp_host", smtp_host)
    db.set_setting("outlook_smtp_port", str(smtp_port))
    db.set_setting("outlook_smtp_user", smtp_user)
    db.set_setting("outlook_smtp_password", smtp_password)
    db.set_setting("outlook_smtp_use_tls", "1" if smtp_use_tls else "0")
    db.set_setting("outlook_smtp_fallback", "1" if smtp_fallback else "0")
    return public_settings()


# ---- MSAL Token Cache & Device Code Flow --------------------------------------------
def _cache_path() -> Path:
    return db.paths.data / "msal_token_cache.json"


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    path = _cache_path()
    if path.is_file():
        try:
            cache.deserialize(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if not cache.has_state_changed:
        return
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache.serialize(), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _build_msal_app(cfg: dict[str, Any], cache: msal.SerializableTokenCache) -> Any:
    client_id = cfg["client_id"]
    if not client_id:
        raise OutlookError("Set your Microsoft Entra Application (Client) ID in Outlook settings first.")
    authority = f"https://login.microsoftonline.com/{cfg['tenant_id'] or 'common'}"
    if _msal_factory is not None:
        return _msal_factory(client_id, authority, cache)
    return msal.PublicClientApplication(client_id=client_id, authority=authority, token_cache=cache)


def get_signed_in_account(cfg: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Inspect cached MSAL accounts without network calls."""
    cfg = cfg or raw_settings()
    if not cfg["client_id"]:
        return None
    path = _cache_path()
    if not path.is_file() and _msal_factory is None:
        return None
    cache = _load_cache()
    try:
        app = _build_msal_app(cfg, cache)
        accounts = app.get_accounts()
    except Exception:
        return None
    if not accounts:
        return None
    acct = accounts[0]
    return {
        "username": str(acct.get("username") or ""),
        "name": str(acct.get("name") or acct.get("username") or "Microsoft Account"),
    }


def sign_out() -> None:
    """Remove persisted MSAL token cache and reset auth status."""
    try:
        cfg = raw_settings()
        if cfg["client_id"]:
            cache = _load_cache()
            app = _build_msal_app(cfg, cache)
            for acct in app.get_accounts():
                if hasattr(app, "remove_account"):
                    app.remove_account(acct)
    except Exception:
        pass
    path = _cache_path()
    if path.exists():
        path.unlink()
    reset_auth_state()


def start_device_flow() -> dict[str, Any]:
    """Start Microsoft OAuth2 Device Code Flow and poll in a background thread."""
    cfg = raw_settings()
    cache = _load_cache()
    app = _build_msal_app(cfg, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        msg = flow.get("error_description") or flow.get("error") or "Microsoft rejected the device code request."
        raise OutlookError(f"Could not start Microsoft sign-in: {msg}")

    expires_in = int(flow.get("expires_in", 900))
    ver_uri = str(flow.get("verification_uri") or "https://microsoft.com/devicelogin")
    user_code = str(flow["user_code"])
    human_msg = str(flow.get("message") or f"Open {ver_uri} and enter code {user_code}.")

    with _auth_lock:
        _auth_state.update(
            status="pending",
            user_code=user_code,
            verification_uri=ver_uri,
            message=human_msg,
            expires_at=time.time() + expires_in,
            error="",
            account=None,
        )

    def _poll() -> None:
        try:
            result = app.acquire_token_by_device_flow(flow)
            _save_cache(cache)
            if "access_token" in result:
                claims = result.get("id_token_claims") or {}
                accts = app.get_accounts()
                username = str((accts[0].get("username") if accts else "") or claims.get("preferred_username") or "")
                name = str(claims.get("name") or username or "Microsoft Account")
                with _auth_lock:
                    _auth_state.update(
                        status="signed_in",
                        user_code="",
                        error="",
                        message=f"Signed in as {username or name}.",
                        account={"username": username, "name": name},
                    )
            else:
                err = str(result.get("error_description") or result.get("error") or "Sign-in was cancelled or timed out.")
                with _auth_lock:
                    _auth_state.update(status="error", error=err, message=err)
        except Exception as exc:
            with _auth_lock:
                _auth_state.update(status="error", error=str(exc), message=str(exc))

    threading.Thread(target=_poll, daemon=True).start()
    return auth_status()


def auth_status() -> dict[str, Any]:
    """Return current Microsoft sign-in state for UI polling."""
    cfg = raw_settings()
    acct = get_signed_in_account(cfg)
    with _auth_lock:
        out = dict(_auth_state)
        if acct is not None:
            out["status"] = "signed_in"
            out["account"] = acct
            out["error"] = ""
        elif out["status"] == "pending" and out["expires_at"] and time.time() > out["expires_at"]:
            out["status"] = "error"
            out["error"] = "Device code expired. Start sign-in again."
        out["signed_in"] = acct is not None
        return out


def acquire_token_silent(cfg: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return ``(access_token, account_email)`` silently from MSAL token cache."""
    cfg = cfg or raw_settings()
    cache = _load_cache()
    app = _build_msal_app(cfg, cache)
    accounts = app.get_accounts()
    if not accounts:
        raise OutlookAuthRequired("Not signed in to Microsoft Outlook yet. Open Outlook settings to sign in.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    if not result or "access_token" not in result:
        err = (result or {}).get("error_description") or "Session expired."
        raise OutlookAuthRequired(f"Outlook token expired or unavailable ({err}). Sign in again in Outlook settings.")
    return str(result["access_token"]), str(accounts[0].get("username") or "")


# ---- Message Formatting & Microsoft Graph Mail Delivery ------------------------------
def _plain_to_html(body: str) -> str:
    """Convert plain-text draft email into clean, readable HTML for Outlook."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", (body or "").strip()) if p.strip()]
    blocks = []
    for p in paragraphs:
        escaped = "<br>".join(html.escape(line) for line in p.splitlines())
        blocks.append(f'<p style="margin:0 0 12px 0;font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;line-height:1.5;color:#1b1b1b;">{escaped}</p>')
    return "\n".join(blocks)


def _graph_message_payload(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    attachment: tuple[str, bytes] | None = None,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "subject": _safe_header(subject),
        "body": {
            "contentType": "HTML",
            "content": _plain_to_html(body),
        },
        "toRecipients": [
            {
                "emailAddress": {
                    "address": _safe_header(to_email),
                    **({"name": _safe_header(to_name)} if _safe_header(to_name) else {}),
                }
            }
        ],
    }
    if attachment is not None:
        fname, data = attachment
        if len(data) > MAX_GRAPH_ATTACHMENT_BYTES:
            raise OutlookError(f"Attachment {fname} exceeds Microsoft Graph 3 MB inline limit.")
        msg["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": _safe_header(fname),
                "contentType": "application/pdf",
                "contentBytes": base64.b64encode(data).decode("ascii"),
            }
        ]
    return msg


def _graph_post(path: str, payload: dict[str, Any], token: str, cfg: dict[str, Any]) -> dict[str, Any]:
    url = f"{GRAPH_BASE}{path}"
    if _graph_backend is not None:
        return _graph_backend("POST", url, payload, token, cfg)
    client = _get_http_client()
    resp = client.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=config.OUTLOOK_TIMEOUT,
    )
    if resp.status_code == 401:
        raise OutlookAuthRequired("Microsoft Graph rejected the token (401). Sign in again in Outlook settings.")
    if resp.status_code == 413:
        raise _GraphPayloadTooLarge("Microsoft Graph request exceeded 4 MB single-request limit (413).")
    if resp.status_code >= 400:
        detail = ""
        try:
            err_obj = resp.json().get("error") or {}
            detail = err_obj.get("message") or resp.text[:300]
        except Exception:
            detail = resp.text[:300]
        raise OutlookError(f"Microsoft Graph error ({resp.status_code}): {detail}")
    return resp.json() if resp.content else {}


def _graph_delete(path: str, token: str, cfg: dict[str, Any]) -> None:
    """Best-effort cleanup of an incomplete Graph draft so partial drafts do not pile up."""
    url = f"{GRAPH_BASE}{path}"
    try:
        if _graph_backend is not None:
            _graph_backend("DELETE", url, {}, token, cfg)
            return
        _get_http_client().delete(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=config.OUTLOOK_TIMEOUT,
        )
    except Exception:
        pass


def _upload_large_attachment(draft_id: str, path: Path, size: int, token: str, cfg: dict[str, Any]) -> None:
    """Upload a 3 MB - 150 MB attachment via Microsoft Graph createUploadSession in 320 KiB-aligned chunks."""
    session = _graph_post(
        f"/me/messages/{draft_id}/attachments/createUploadSession",
        {
            "AttachmentItem": {
                "attachmentType": "file",
                "name": _safe_header(path.name),
                "size": size,
            }
        },
        token,
        cfg,
    )
    upload_url = str(session.get("uploadUrl") or "")
    if not upload_url:
        raise OutlookError(f"Microsoft Graph did not return an upload URL for {path.name}.")

    client = None if _graph_backend is not None else _get_http_client()
    offset = 0
    with path.open("rb") as fh:
        while offset < size:
            chunk = fh.read(UPLOAD_CHUNK_BYTES)
            if not chunk:
                break
            end = offset + len(chunk) - 1
            content_range = f"bytes {offset}-{end}/{size}"
            if _graph_backend is not None:
                _graph_backend(
                    "PUT",
                    upload_url,
                    {"content_range": content_range, "content_length": len(chunk), "chunk": chunk},
                    "",
                    cfg,
                )
            else:
                assert client is not None
                resp = client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": content_range,
                    },
                    timeout=max(config.OUTLOOK_TIMEOUT, 120.0),
                )
                if resp.status_code >= 400:
                    raise OutlookError(
                        f"Chunk upload failed for {path.name} ({resp.status_code}): {resp.text[:300]}"
                    )
            offset += len(chunk)


def send_graph_mail_stepwise(
    to_email: str,
    to_name: str,
    subject: str,
    html_body: str,
    attachments: list[Path],
    *,
    send: bool = True,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Graph draft, upload every attachment (using upload sessions when >= 3 MiB), and send it."""
    cfg = cfg or raw_settings()
    recipient = _safe_header(cfg.get("test_recipient") or to_email)
    if not checks.EMAIL_RE.fullmatch(recipient):
        raise ValueError(f'Recipient email "{recipient}" is not valid.')

    draft_id = ""
    token = ""
    try:
        token, acct_user = acquire_token_silent(cfg)
        clean_name = _safe_header(to_name)
        draft_payload = {
            "subject": _safe_header(subject),
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": recipient,
                        **({"name": clean_name} if clean_name else {}),
                    }
                }
            ],
        }
        created = _graph_post("/me/messages", draft_payload, token, cfg)
        draft_id = str(created.get("id") or "")
        web_link = str(created.get("webLink") or "")
        if not draft_id:
            raise OutlookError("Microsoft Graph did not return a draft message ID.")

        for path in attachments:
            size = path.stat().st_size
            if size > MAX_UPLOAD_SESSION_BYTES:
                raise OutlookError(f"Attachment {path.name} exceeds Microsoft Graph 150 MB limit.")
            if size >= UPLOAD_SESSION_MIN_BYTES:
                _upload_large_attachment(draft_id, path, size, token, cfg)
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            att_payload = {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": _safe_header(path.name),
                "contentType": mime,
                "contentBytes": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
            _graph_post(f"/me/messages/{draft_id}/attachments", att_payload, token, cfg)

        if send:
            _graph_post(f"/me/messages/{draft_id}/send", {}, token, cfg)
            return {
                "ok": True,
                "how": "graph",
                "delivery_status": "sent",
                "message_id": draft_id,
                "web_link": web_link,
                "sender": acct_user,
                "sent_to": recipient,
                "error": "",
            }
        return {
            "ok": True,
            "how": "graph_draft",
            "delivery_status": "outlook_draft",
            "message_id": draft_id,
            "web_link": web_link,
            "sender": acct_user,
            "sent_to": recipient,
            "error": "",
        }
    except Exception as exc:
        if draft_id and send and token:
            _graph_delete(f"/me/messages/{draft_id}", token, cfg)
        return {
            "ok": False,
            "how": "graph" if send else "graph_draft",
            "delivery_status": "failed",
            "message_id": "",
            "web_link": "",
            "sender": "",
            "sent_to": recipient,
            "error": str(exc),
        }


def create_outlook_draft(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    *,
    attachment: tuple[str, bytes] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save a formatted draft directly in the signed-in user's Outlook Drafts folder."""
    cfg = cfg or raw_settings()
    recipient = _safe_header(cfg.get("test_recipient") or to_email)
    if not checks.EMAIL_RE.fullmatch(recipient):
        raise ValueError(f'Recipient email "{recipient}" is not valid.')
    try:
        token, acct_user = acquire_token_silent(cfg)
        payload = _graph_message_payload(recipient, to_name, subject, body, attachment=attachment)
        data = _graph_post("/me/messages", payload, token, cfg)
        return {
            "ok": True,
            "delivery_status": "outlook_draft",
            "sent_to": recipient,
            "account": acct_user,
            "message_id": str(data.get("id") or ""),
            "web_link": str(data.get("webLink") or ""),
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "delivery_status": "failed",
            "sent_to": recipient,
            "account": "",
            "message_id": "",
            "web_link": "",
            "error": str(exc),
        }


def send_outlook_mail(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    *,
    attachment: tuple[str, bytes] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send an email directly via Microsoft Graph POST /me/sendMail."""
    cfg = cfg or raw_settings()
    recipient = _safe_header(cfg.get("test_recipient") or to_email)
    if not checks.EMAIL_RE.fullmatch(recipient):
        raise ValueError(f'Recipient email "{recipient}" is not valid.')
    try:
        token, acct_user = acquire_token_silent(cfg)
        message = _graph_message_payload(recipient, to_name, subject, body, attachment=attachment)
        _graph_post("/me/sendMail", {"message": message, "saveToSentItems": True}, token, cfg)
        return {
            "ok": True,
            "delivery_status": "sent",
            "sent_to": recipient,
            "account": acct_user,
            "message_id": make_msgid(domain="graph.microsoft.com"),
            "web_link": "",
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "delivery_status": "failed",
            "sent_to": recipient,
            "account": "",
            "message_id": "",
            "web_link": "",
            "error": str(exc),
        }


def send_smtp_email(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    *,
    attachment: tuple[str, bytes] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send via Office 365 / Outlook SMTP AUTH (STARTTLS port 587)."""
    cfg = cfg or raw_settings()
    recipient = _safe_header(cfg.get("test_recipient") or to_email)
    if not checks.EMAIL_RE.fullmatch(recipient):
        raise ValueError(f'Recipient email "{recipient}" is not valid.')

    sender_email = _safe_header(cfg.get("smtp_user") or "")
    if not sender_email:
        return {
            "ok": False,
            "delivery_status": "failed",
            "sent_to": recipient,
            "message_id": "",
            "web_link": "",
            "error": "Office 365 SMTP username is empty.",
        }

    msg = EmailMessage()
    msg["From"] = formataddr((_safe_header(cfg.get("from_name") or "HR Hiring Team"), sender_email))
    msg["To"] = formataddr((_safe_header(to_name), recipient))
    msg["Subject"] = _safe_header(subject)
    msg["Date"] = formatdate(localtime=True)
    msg_id = make_msgid(domain=sender_email.split("@")[-1])
    msg["Message-ID"] = msg_id
    msg.set_content(body)
    msg.add_alternative(_plain_to_html(body), subtype="html")
    if attachment is not None:
        fname, data = attachment
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=_safe_header(fname))

    try:
        if _smtp_backend is not None:
            out_id = _smtp_backend(msg, cfg) or msg_id
        else:
            with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=config.OUTLOOK_TIMEOUT) as srv:
                srv.ehlo()
                if cfg.get("smtp_use_tls", True):
                    srv.starttls(context=_tls_context())
                    srv.ehlo()
                if cfg.get("smtp_user") and cfg.get("smtp_password"):
                    srv.login(cfg["smtp_user"], cfg["smtp_password"])
                srv.send_message(msg)
            out_id = msg_id
        return {
            "ok": True,
            "delivery_status": "sent",
            "sent_to": recipient,
            "message_id": out_id,
            "web_link": "",
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "delivery_status": "failed",
            "sent_to": recipient,
            "message_id": "",
            "web_link": "",
            "error": str(exc),
        }


def send_smtp_mime_message(
    msg: EmailMessage,
    recipient: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a complete multi-attachment EmailMessage via Office 365 / Outlook SMTP AUTH."""
    cfg = cfg or raw_settings()
    clean_recipient = _safe_header(recipient)
    sender_email = _safe_header(cfg.get("smtp_user") or "")
    msg_id = str(msg.get("Message-ID") or make_msgid(domain="hr-hiring.local"))
    if not sender_email:
        return {
            "ok": False,
            "delivery_status": "failed",
            "sent_to": clean_recipient,
            "message_id": "",
            "web_link": "",
            "error": "Office 365 SMTP username is empty.",
        }
    try:
        if _smtp_backend is not None:
            out_id = _smtp_backend(msg, cfg) or msg_id
        else:
            with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_port"]), timeout=config.OUTLOOK_TIMEOUT) as srv:
                srv.ehlo()
                if cfg.get("smtp_use_tls", True):
                    srv.starttls(context=_tls_context())
                    srv.ehlo()
                if cfg.get("smtp_user") and cfg.get("smtp_password"):
                    srv.login(cfg["smtp_user"], cfg["smtp_password"])
                srv.send_message(msg)
            out_id = msg_id
        return {
            "ok": True,
            "delivery_status": "sent",
            "sent_to": clean_recipient,
            "message_id": out_id,
            "web_link": "",
            "error": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "delivery_status": "failed",
            "sent_to": clean_recipient,
            "message_id": "",
            "web_link": "",
            "error": str(exc),
        }


def deliver_draft(
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    *,
    attachment: tuple[str, bytes] | None = None,
    eml_path: Path | None = None,
    attachment_paths: Sequence[Path] | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch an email using the configured Outlook mode ('eml', 'outlook_app', 'graph', 'graph_draft', 'graph_send', 'smtp')."""
    cfg = cfg or raw_settings()
    mode = cfg.get("mode", "eml")
    if mode == "graph_draft":
        return create_outlook_draft(to_email, to_name, subject, body, attachment=attachment, cfg=cfg)
    if mode in ("graph", "graph_send"):
        return send_outlook_mail(to_email, to_name, subject, body, attachment=attachment, cfg=cfg)
    if mode == "smtp":
        return send_smtp_email(to_email, to_name, subject, body, attachment=attachment, cfg=cfg)
    recipient = _safe_header(cfg.get("test_recipient") or to_email)
    if mode == "outlook_app":
        target_eml = eml_path
        if target_eml is None:
            sender_email = _safe_header(cfg.get("smtp_user") or "hiring@example.com")
            msg = EmailMessage()
            msg["From"] = formataddr((_safe_header(cfg.get("from_name") or "HR Hiring Team"), sender_email))
            msg["To"] = formataddr((_safe_header(to_name), recipient)) if to_name else recipient
            msg["Subject"] = _safe_header(subject)
            msg["Date"] = formatdate(localtime=True)
            msg["X-Unsent"] = "1"
            msg["X-Uniform-Type-Identifier"] = "com.apple.mail-draft"
            msg.set_content(body)
            msg.add_alternative(_plain_to_html(body), subtype="html")
            if attachment is not None:
                fname, data = attachment
                msg.add_attachment(data, maintype="application", subtype="pdf", filename=_safe_header(fname))
            target_eml = db.paths.outbox / "outlook_test.eml"
            target_eml.write_bytes(msg.as_bytes())
        try:
            app_name = open_eml_in_desktop_app(
                target_eml,
                to_email=recipient,
                to_name=_safe_header(to_name),
                subject=_safe_header(subject),
                html=_plain_to_html(body),
                text=body,
                attachments=attachment_paths,
            )
            return {
                "ok": True,
                "delivery_status": "opened_app",
                "opened_in": app_name,
                "sent_to": recipient,
                "message_id": "",
                "web_link": "",
                "error": "",
            }
        except OutlookError as exc:
            return {
                "ok": False,
                "delivery_status": "failed",
                "sent_to": recipient,
                "message_id": "",
                "web_link": "",
                "error": str(exc),
            }
    return {
        "ok": True,
        "delivery_status": "eml_only",
        "sent_to": recipient,
        "message_id": "",
        "web_link": "",
        "error": "",
    }


def send_test(target: str) -> dict[str, Any]:
    """Test active Outlook configuration by creating a test draft or sending a test email."""
    cfg = raw_settings()
    clean_target = _safe_header(target or cfg.get("test_recipient") or cfg.get("smtp_user") or "")
    if not checks.EMAIL_RE.fullmatch(clean_target):
        raise ValueError("Provide a valid target email address to test Outlook delivery.")
    subject = "HR Hiring — Outlook Integration Test"
    body = (
        "Hello,\n\n"
        "This test confirms your HR Hiring system is connected to Microsoft Outlook.\n\n"
        f"Configured delivery mode: {cfg['mode']}\n\n"
        "Kind regards,\nHiring Team"
    )
    if cfg["mode"] == "eml":
        acct = get_signed_in_account(cfg)
        if acct is not None:
            return create_outlook_draft(clean_target, "HR Test", subject, body, cfg=cfg)
        raise ValueError("Currently in .eml file mode. Choose Open in Outlook App, Microsoft Graph, or Office 365 SMTP to test Outlook delivery.")
    return deliver_draft(clean_target, "HR Test", subject, body, cfg=cfg)


def compose_links(to_email: str, subject: str, body: str, test_recipient: str = "") -> dict[str, str]:
    """Build zero-setup Microsoft 365 Outlook Web, Outlook.com, and desktop mailto links."""
    target = _safe_header(test_recipient or to_email)
    if not target:
        return {}
    clean_subj = _safe_header(subject)[:200]
    clean_body = (body or "").strip()
    if len(clean_body) > _URL_BODY_MAX:
        clean_body = clean_body[:_URL_BODY_MAX].rstrip() + "\n\n[Truncated — download .eml for full text & PDF]"
    params = urlencode({"to": target, "subject": clean_subj, "body": clean_body}, quote_via=quote)
    return {
        "outlook_office": f"https://outlook.office.com/mail/deeplink/compose?{params}",
        "outlook_live": f"https://outlook.live.com/mail/0/deeplink/compose?{params}",
        "mailto": f"mailto:{quote(target)}?{urlencode({'subject': clean_subj, 'body': clean_body}, quote_via=quote)}",
    }


_CLASSIC_OUTLOOK_SCRIPT = """on run argv
    set subj to item 1 of argv
    set bodyTxt to item 2 of argv
    set toName to item 3 of argv
    set toAddr to item 4 of argv
    tell application "Microsoft Outlook"
        set msg to make new outgoing message with properties {subject:subj, content:bodyTxt}
        tell msg
            if toAddr is not "" then
                if toName is not "" then
                    make new recipient at it with properties {type:to recipient type, email address:{name:toName, address:toAddr}}
                else
                    make new recipient at it with properties {type:to recipient type, email address:{address:toAddr}}
                end if
            end if
            repeat with i from 5 to (count of argv)
                set attPath to item i of argv
                if attPath is not "" then
                    set attAlias to (POSIX file attPath) as alias
                    make new attachment at it with properties {file:attAlias}
                end if
            end repeat
        end tell
        open msg
        activate
    end tell
end run"""


def _open_classic_outlook_mac(
    to_email: str,
    to_name: str,
    subject: str,
    body_content: str,
    attachments: Sequence[Path],
) -> bool:
    cmd = ["osascript", "-", subject, body_content, to_name, to_email, *(str(p) for p in attachments if p.is_file())]
    res = subprocess.run(cmd, input=_CLASSIC_OUTLOOK_SCRIPT, capture_output=True, text=True, check=False, timeout=12)
    return res.returncode == 0


def open_eml_in_desktop_app(
    eml_path: Path,
    *,
    to_email: str = "",
    to_name: str = "",
    subject: str = "",
    html: str = "",
    text: str = "",
    attachments: Sequence[Path] | None = None,
) -> str:
    """Open a generated .eml draft in Microsoft Outlook Desktop (or default Mail app)."""
    if not eml_path.is_file():
        raise OutlookError(f"Draft file not found: {eml_path.name}")
    att_list = [p for p in (attachments or []) if p.is_file()]
    if _desktop_launcher is not None:
        return _desktop_launcher(
            eml_path,
            to_email=to_email,
            to_name=to_name,
            subject=subject,
            html=html,
            text=text,
            attachments=att_list,
        )

    path_str = str(eml_path)
    if sys.platform == "darwin":
        has_outlook = subprocess.run(["open", "-Ra", "Microsoft Outlook"], capture_output=True, check=False, timeout=5).returncode == 0
        if has_outlook:
            if to_email and subject and _open_classic_outlook_mac(to_email, to_name, subject, html or text, att_list):
                return "Microsoft Outlook"
            res = subprocess.run(["open", "-a", "Microsoft Outlook", path_str], capture_output=True, text=True, check=False, timeout=12)
            if res.returncode == 0:
                return "Microsoft Outlook"
        res_mail = subprocess.run(["open", "-a", "Mail", path_str], capture_output=True, text=True, check=False, timeout=12)
        if res_mail.returncode == 0:
            return "Apple Mail"
        res_any = subprocess.run(["open", path_str], capture_output=True, text=True, check=False, timeout=12)
        if res_any.returncode == 0:
            return "Mail"
        raise OutlookError((res_any.stderr or res_mail.stderr or "Could not open Mail app on macOS.").strip())

    if sys.platform == "win32":
        try:
            os.startfile(path_str)  # type: ignore[attr-defined]
            return "Outlook Desktop"
        except OSError as exc:
            raise OutlookError(f"Could not open desktop Outlook/Mail app: {exc}") from exc

    opener = shutil.which("xdg-open")
    if not opener:
        raise OutlookError("No desktop mail launcher found. Download the .eml file instead.")
    subprocess.Popen([opener, path_str], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "Desktop Mail"
