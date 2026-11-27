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
import socket
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

VALID_MODES = ("auto", "outlook_app", "eml", "graph", "graph_draft", "graph_send", "smtp")
# Mail-only mode: which mail service the sender's address belongs to. Any email domain works.
PROVIDERS = ("microsoft", "google", "custom", "desktop")
SMTP_SECURITY = ("starttls", "ssl", "none")
SMTP_PRESETS = {
    "microsoft": {"smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_security": "starttls"},
    "google": {"smtp_host": "smtp.gmail.com", "smtp_port": 587, "smtp_security": "starttls"},
}
PROVIDER_NAMES = {"microsoft": "Microsoft", "google": "Google", "custom": "Your mail server", "desktop": "Your mail app"}
# Largest whole mail (attachments after email encoding) each service accepts.
MAIL_SIZE_LIMITS = {"microsoft": 35 * 1024 * 1024, "google": 35 * 1024 * 1024, "custom": 25 * 1024 * 1024,
                    "desktop": 35 * 1024 * 1024}
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
_mx_resolver: Callable[[str], list[str]] | None = None
_secret_store: Any = None          # tests inject an in-memory store; otherwise keyring when installed
_secret_cache: dict[str, str] = {}

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


def set_mx_resolver(fn: Callable[[str], list[str]] | None) -> None:
    """Inject an MX lookup stub in tests: ``fn(domain) -> [mail server host names]``."""
    global _mx_resolver
    _mx_resolver = fn


def set_secret_store(store: Any) -> None:
    """Inject a password store in tests (an object with get/set/delete), or None for the default."""
    global _secret_store
    _secret_store = store
    _secret_cache.clear()


class _KeyringStore:
    """The system keychain (macOS Keychain, Windows Credential Manager) through the keyring package."""

    def __init__(self, keyring_mod: Any):
        self._k = keyring_mod
        self._service = "HR Hiring mail"

    def _account(self, key: str) -> str:
        return f"{key}:{db.paths.data.resolve()}"

    def get(self, key: str) -> str | None:
        return self._k.get_password(self._service, self._account(key))

    def set(self, key: str, value: str) -> None:
        self._k.set_password(self._service, self._account(key), value)

    def delete(self, key: str) -> None:
        try:
            self._k.delete_password(self._service, self._account(key))
        except Exception:
            pass


def _secrets() -> Any:
    """The password store to use, or None to keep the password in the local database."""
    if _secret_store is not None:
        return _secret_store
    if config.SECRET_STORE == "db":
        return None
    try:
        import keyring  # optional dependency
        from keyring.backends import fail

        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return _KeyringStore(keyring)
    except Exception:
        return None


def _read_secret(key: str) -> str:
    if key in _secret_cache:
        return _secret_cache[key]
    store = _secrets()
    value = ""
    if store is not None:
        try:
            value = store.get(key) or ""
        except Exception:
            value = ""
    _secret_cache[key] = value
    return value


def _store_password(password: str) -> None:
    """Keep the SMTP password in the system keychain when possible, otherwise in the database."""
    _secret_cache.pop("smtp_password", None)
    store = _secrets()
    if password and store is not None:
        try:
            store.set("smtp_password", password)
            db.set_setting("outlook_smtp_password", "")
            db.set_setting("outlook_smtp_password_store", "keychain")
            _secret_cache["smtp_password"] = password
            return
        except Exception:
            pass
    if store is not None and not password:
        try:
            store.delete("smtp_password")
        except Exception:
            pass
    db.set_setting("outlook_smtp_password", password)
    db.set_setting("outlook_smtp_password_store", "database" if password else "")


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
    saved_sender = db.get_setting("outlook_sender_email").strip() or config.OUTLOOK_SENDER_EMAIL
    smtp_user = db.get_setting("outlook_smtp_user").strip() or config.OUTLOOK_SMTP_USER
    sender_email = saved_sender or smtp_user
    if sender_email and not smtp_user:
        smtp_user = sender_email
    password_store = db.get_setting("outlook_smtp_password_store")
    if password_store == "keychain":
        smtp_password = _read_secret("smtp_password") or config.OUTLOOK_SMTP_PASSWORD
    else:
        smtp_password = db.get_setting("outlook_smtp_password") or config.OUTLOOK_SMTP_PASSWORD
    smtp_use_tls = _parse_bool(db.get_setting("outlook_smtp_use_tls"), config.OUTLOOK_SMTP_USE_TLS)
    smtp_fallback = _parse_bool(db.get_setting("outlook_smtp_fallback"), config.OUTLOOK_SMTP_FALLBACK)
    smtp_security = db.get_setting("outlook_smtp_security").strip().lower()
    if smtp_security not in SMTP_SECURITY:
        smtp_security = "ssl" if smtp_port == 465 else ("starttls" if smtp_use_tls else "none")
    provider = db.get_setting("outlook_provider").strip().lower()
    provider_chosen = provider in PROVIDERS        # picked on the Mail settings page (mail-only mode)
    if not provider_chosen:
        provider = _provider_from_host(smtp_host)
    return {
        "provider": provider,
        "provider_chosen": provider_chosen,
        "smtp_security": smtp_security,
        "password_store": password_store if smtp_password else "",
        "mode": mode,
        "client_id": client_id,
        "tenant_id": tenant_id,
        "from_name": from_name,
        "sender_email": sender_email,
        "test_recipient": test_recipient,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "smtp_use_tls": smtp_use_tls,
        "smtp_fallback": smtp_fallback,
    }


def _provider_from_host(host: str) -> str:
    h = (host or "").strip().lower()
    if not h or h.endswith("office365.com") or h.endswith("outlook.com"):
        return "microsoft"
    if h.endswith("gmail.com") or h.endswith("google.com") or h.endswith("googlemail.com"):
        return "google"
    return "custom"


def effective_mode(cfg: dict[str, Any] | None = None, *, account: dict[str, str] | None = None) -> str:
    """Resolve 'auto' into 'graph', 'smtp', or 'outlook_app' based on available credentials."""
    cfg = cfg or raw_settings()
    mode = cfg.get("mode", "eml")
    if mode != "auto":
        return mode
    provider = cfg.get("provider", "microsoft") if cfg.get("provider_chosen") else "microsoft"
    if provider == "desktop":
        return "outlook_app"
    # Once a Google or other mail server is chosen, the Microsoft sign-in (Graph) is not used.
    acct = (account if account is not None else get_signed_in_account(cfg)) if provider == "microsoft" else None
    if acct is not None:
        return "graph"
    if cfg.get("smtp_user") and cfg.get("smtp_password"):
        return "smtp"
    return "outlook_app"


def public_settings() -> dict[str, Any]:
    """Return UI-safe Outlook settings without secrets or cached tokens."""
    cfg = raw_settings()
    acct = get_signed_in_account(cfg)
    eff = effective_mode(cfg, account=acct)
    with _auth_lock:
        auth_view = dict(_auth_state)
        if acct:
            auth_view["status"] = "signed_in"
            auth_view["account"] = acct
    return {
        "mode": cfg["mode"],
        "effective_mode": eff,
        "provider": cfg["provider"],
        "provider_chosen": cfg["provider_chosen"],
        "smtp_security": cfg["smtp_security"],
        "password_store": cfg["password_store"],
        "size_limit": MAIL_SIZE_LIMITS.get(cfg["provider"], MAIL_SIZE_LIMITS["custom"]),
        "client_id": cfg["client_id"],
        "has_client_id": bool(cfg["client_id"]),
        "tenant_id": cfg["tenant_id"],
        "from_name": cfg["from_name"],
        "sender_email": cfg["sender_email"],
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
    sender_email = str(payload.get("sender_email", cur["sender_email"])).strip()[:200]
    if sender_email and not checks.EMAIL_RE.fullmatch(sender_email):
        raise ValueError(f'Your Outlook email "{sender_email}" must be a valid email address.')

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

    raw_smtp_user = str(payload.get("smtp_user", "")).strip()[:200] if "smtp_user" in payload else cur["smtp_user"]
    smtp_user = raw_smtp_user or sender_email
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

    # Mail-only mode sends the provider (Microsoft, Google, other server or the mail app) and the
    # SMTP security. The full-mode Outlook dialog does not, so its settings are kept as they were.
    provider = cur["provider"] if cur.get("provider_chosen") else ""
    if "provider" in payload:
        provider = str(payload.get("provider") or "").strip().lower()
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown email provider '{provider}'. Choose one of: {', '.join(PROVIDERS)}.")
    smtp_security = cur["smtp_security"]
    if "smtp_security" in payload:
        smtp_security = str(payload.get("smtp_security") or "").strip().lower()
        if smtp_security not in SMTP_SECURITY:
            raise ValueError("SMTP security must be STARTTLS, SSL or none.")
    elif "smtp_use_tls" in payload and smtp_security != "ssl":
        smtp_security = "starttls" if smtp_use_tls else "none"
    if provider in SMTP_PRESETS:
        preset = SMTP_PRESETS[provider]
        smtp_host = preset["smtp_host"]
        if provider != cur["provider"] or not cur.get("provider_chosen"):
            smtp_port, smtp_security = preset["smtp_port"], preset["smtp_security"]
    if provider == "desktop":
        mode = "outlook_app"
    elif "provider" in payload and mode == "outlook_app" and cur["provider"] == "desktop":
        mode = "auto"
    if "provider" in payload or "smtp_security" in payload:
        smtp_use_tls = smtp_security == "starttls"
    if provider == "custom" and smtp_password and "smtp_host" in payload and not str(payload.get("smtp_host") or "").strip():
        raise ValueError("Enter the SMTP server name of your mail service.")

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
    db.set_setting("outlook_sender_email", sender_email)
    db.set_setting("outlook_test_recipient", test_recipient)
    db.set_setting("outlook_smtp_host", smtp_host)
    db.set_setting("outlook_smtp_port", str(smtp_port))
    db.set_setting("outlook_smtp_user", smtp_user)
    _store_password(smtp_password)
    db.set_setting("outlook_smtp_use_tls", "1" if smtp_use_tls else "0")
    db.set_setting("outlook_smtp_fallback", "1" if smtp_fallback else "0")
    if provider:
        db.set_setting("outlook_provider", provider)
    if "provider" in payload or "smtp_security" in payload:
        db.set_setting("outlook_smtp_security", smtp_security)
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


def start_interactive_login() -> dict[str, Any]:
    """Sign in via 1-click browser window (MSAL interactive flow) with Device Code fallback."""
    cfg = raw_settings()
    cache = _load_cache()
    app = _build_msal_app(cfg, cache)
    if not hasattr(app, "acquire_token_interactive"):
        return start_device_flow()

    with _auth_lock:
        _auth_state.update(
            status="pending",
            user_code="",
            verification_uri="",
            message="Complete sign-in in the Microsoft browser window that just opened...",
            expires_at=time.time() + 180,
            error="",
            account=None,
        )

    def _run_interactive() -> None:
        try:
            result = app.acquire_token_interactive(
                scopes=SCOPES,
                prompt="select_account",
                login_hint=(cfg.get("sender_email") or None),
                timeout=180,
            )
            _save_cache(cache)
            if isinstance(result, dict) and "access_token" in result:
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
                err = str((result or {}).get("error_description") or (result or {}).get("error") or "Sign-in was cancelled or timed out.")
                with _auth_lock:
                    _auth_state.update(status="error", error=err, message=err)
        except Exception as exc:
            msg = str(exc)
            # Fall back to device code flow if a local browser window cannot be opened
            if "browser" in msg.lower() or "display" in msg.lower():
                try:
                    start_device_flow()
                    return
                except Exception:
                    pass
            with _auth_lock:
                _auth_state.update(status="error", error=msg, message=msg)

    threading.Thread(target=_run_interactive, daemon=True).start()
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


def smtp_open(cfg: dict[str, Any]) -> smtplib.SMTP:
    """Connect to the SMTP server with the chosen security: STARTTLS (587), SSL (465) or none."""
    host, port = cfg["smtp_host"], int(cfg["smtp_port"])
    security = cfg.get("smtp_security") or ("starttls" if cfg.get("smtp_use_tls", True) else "none")
    if security == "ssl":
        srv: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=config.OUTLOOK_TIMEOUT, context=_tls_context())
        srv.ehlo()
        return srv
    srv = smtplib.SMTP(host, port, timeout=config.OUTLOOK_TIMEOUT)
    srv.ehlo()
    if security == "starttls":
        srv.starttls(context=_tls_context())
        srv.ehlo()
    return srv


def smtp_error_text(exc: Exception, cfg: dict[str, Any]) -> str:
    """A plain explanation of an SMTP failure, written for the person sending the mail."""
    provider = cfg.get("provider", "custom")
    host, port = cfg.get("smtp_host", ""), cfg.get("smtp_port", "")
    detail = re.sub(r"\s+", " ", str(exc)).strip()[:200]
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        if provider == "google":
            text = ("Google did not accept the email and App Password. Use a 16-letter App Password "
                    "(Google Account > Security > 2-Step Verification > App passwords), not your normal password. "
                    "Your Google Workspace admin may have turned App Passwords off.")
        elif provider == "microsoft":
            text = ("Microsoft did not accept the email and password. Your organisation may have switched off "
                    "SMTP sign-in (SMTP AUTH) for your account. Choose 'Outlook or Mail app on this computer' "
                    "instead, or ask IT to allow SMTP AUTH.")
        else:
            text = "The mail server did not accept the email and password. Check them with your IT team."
        return f"{text} (Server said: {detail})"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (f"The mail server refused the sender address {exc.sender!s}. The From address must be your own "
                f"account, or an alias your account may send as. (Server said: {detail})")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"The mail server refused the student's address. Check the email address. (Server said: {detail})"
    if isinstance(exc, smtplib.SMTPDataError) and exc.smtp_code == 552:
        return f"The mail is too big for the mail server. Untick an attachment and try again. (Server said: {detail})"
    if isinstance(exc, socket.gaierror):
        return f"Could not find the mail server '{host}'. Check the server name."
    if isinstance(exc, ssl.SSLError) or isinstance(exc, smtplib.SMTPNotSupportedError):
        return ("The secure connection to the mail server failed. Use STARTTLS with port 587, "
                f"or SSL with port 465. (Details: {detail})")
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, smtplib.SMTPServerDisconnected)):
        return (f"Could not connect to {host}:{port}. Check the port and security "
                f"(587 = STARTTLS, 465 = SSL) and your internet connection. (Details: {detail})")
    return detail or "The mail could not be sent."


def send_smtp_mime_message(
    msg: EmailMessage,
    recipient: str,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send a complete multi-attachment EmailMessage over SMTP (Microsoft, Google or another server)."""
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
            "error": "The SMTP username (your email address) is empty.",
        }
    try:
        if _smtp_backend is not None:
            out_id = _smtp_backend(msg, cfg) or msg_id
        else:
            with smtp_open(cfg) as srv:
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
            "error": smtp_error_text(exc, cfg),
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
    """Dispatch an email using the configured Outlook mode ('auto', 'eml', 'outlook_app', 'graph', 'graph_draft', 'graph_send', 'smtp')."""
    cfg = cfg or raw_settings()
    mode = effective_mode(cfg)
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
                sender_email=_safe_header(cfg.get("sender_email") or cfg.get("smtp_user") or ""),
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
    eff_mode = effective_mode(cfg)
    body = (
        "Hello,\n\n"
        "This test confirms your HR Hiring system is connected to Microsoft Outlook.\n\n"
        f"Configured delivery mode: {eff_mode}\n\n"
        "Kind regards,\nHiring Team"
    )
    if eff_mode == "eml":
        acct = get_signed_in_account(cfg)
        if acct is not None:
            return create_outlook_draft(clean_target, "HR Test", subject, body, cfg=cfg)
        raise ValueError("Currently in .eml file mode. Choose Automatic, Open in Outlook App, Microsoft Graph, or Office 365 SMTP to test Outlook delivery.")
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
        "gmail": "https://mail.google.com/mail/?" + urlencode(
            {"view": "cm", "fs": "1", "to": target, "su": clean_subj, "body": clean_body}, quote_via=quote),
    }


_CLASSIC_OUTLOOK_SCRIPT = """on run argv
    set subj to item 1 of argv
    set bodyTxt to item 2 of argv
    set toName to item 3 of argv
    set toAddr to item 4 of argv
    set senderEmail to item 5 of argv
    tell application "Microsoft Outlook"
        set matchedAcc to missing value
        if senderEmail is not "" then
            try
                repeat with a in (every exchange account)
                    if (email address of a) is senderEmail then
                        set matchedAcc to a
                        exit repeat
                    end if
                end repeat
            end try
            if matchedAcc is missing value then
                try
                    repeat with a in (every imap account)
                        if (email address of a) is senderEmail then
                            set matchedAcc to a
                            exit repeat
                        end if
                    end repeat
                end try
            end if
            if matchedAcc is missing value then
                try
                    repeat with a in (every pop account)
                        if (email address of a) is senderEmail then
                            set matchedAcc to a
                            exit repeat
                        end if
                    end repeat
                end try
            end if
        end if
        if matchedAcc is not missing value then
            set msg to make new outgoing message with properties {account:matchedAcc, subject:subj, content:bodyTxt}
        else
            set msg to make new outgoing message with properties {subject:subj, content:bodyTxt}
        end if
        tell msg
            if toAddr is not "" then
                if toName is not "" then
                    make new recipient at it with properties {type:to recipient type, email address:{name:toName, address:toAddr}}
                else
                    make new recipient at it with properties {type:to recipient type, email address:{address:toAddr}}
                end if
            end if
            repeat with i from 6 to (count of argv)
                set attPath to item i of argv
                if attPath is not "" then
                    set attAlias to (POSIX file attPath) as alias
                    make new attachment at it with properties {file:attAlias}
                end if
            end repeat
        end tell
        if matchedAcc is not missing value then
            send msg
        else
            open msg
            activate
        end if
    end tell
end run"""


_WINDOWS_OUTLOOK_PS = """
$ErrorActionPreference = 'Stop'
$outlook = New-Object -ComObject Outlook.Application
$mail = $outlook.CreateItem(0)
$mail.To = $env:HR_PS_TO
$mail.Subject = $env:HR_PS_SUBJ
if ($env:HR_PS_HTML) { $mail.HTMLBody = $env:HR_PS_HTML } else { $mail.Body = $env:HR_PS_TEXT }
if ($env:HR_PS_SENDER) {
    foreach ($acc in $outlook.Session.Accounts) {
        if ($acc.SmtpAddress -and ($acc.SmtpAddress.ToLower() -eq $env:HR_PS_SENDER.ToLower())) {
            $mail.SendUsingAccount = $acc
            break
        }
    }
}
if ($env:HR_PS_ATTACHMENTS) {
    foreach ($p in ($env:HR_PS_ATTACHMENTS -split "`n")) {
        if ($p -and (Test-Path -LiteralPath $p)) {
            [void]$mail.Attachments.Add($p)
        }
    }
}
$mail.Send()
[void][System.Runtime.Interopservices.Marshal]::ReleaseComObject($outlook)
"""


def _open_classic_outlook_mac(
    sender_email: str,
    to_email: str,
    to_name: str,
    subject: str,
    body_content: str,
    attachments: Sequence[Path],
) -> bool:
    cmd = ["osascript", "-", subject, body_content, to_name, to_email, sender_email, *(str(p) for p in attachments if p.is_file())]
    res = subprocess.run(cmd, input=_CLASSIC_OUTLOOK_SCRIPT, capture_output=True, text=True, check=False, timeout=12)
    return res.returncode == 0


def _open_windows_outlook_com(
    sender_email: str,
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str,
    attachments: Sequence[Path],
) -> bool:
    ps_exe = shutil.which("powershell") or shutil.which("pwsh")
    if not ps_exe:
        return False
    env = dict(os.environ)
    env["HR_PS_SENDER"] = sender_email or ""
    env["HR_PS_TO"] = to_email or ""
    env["HR_PS_SUBJ"] = subject or ""
    env["HR_PS_HTML"] = html_body or ""
    env["HR_PS_TEXT"] = text_body or ""
    env["HR_PS_ATTACHMENTS"] = "\n".join(str(p) for p in attachments if p.is_file())
    res = subprocess.run([ps_exe, "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_OUTLOOK_PS], env=env, capture_output=True, text=True, check=False, timeout=15)
    return res.returncode == 0


def open_eml_in_desktop_app(
    eml_path: Path,
    *,
    sender_email: str = "",
    to_email: str = "",
    to_name: str = "",
    subject: str = "",
    html: str = "",
    text: str = "",
    attachments: Sequence[Path] | None = None,
) -> str:
    """Open or send a generated .eml draft via Microsoft Outlook Desktop (or default Mail app)."""
    if not eml_path.is_file():
        raise OutlookError(f"Draft file not found: {eml_path.name}")
    att_list = [p for p in (attachments or []) if p.is_file()]
    if _desktop_launcher is not None:
        return _desktop_launcher(
            eml_path,
            sender_email=sender_email,
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
            if to_email and subject and _open_classic_outlook_mac(sender_email, to_email, to_name, subject, html or text, att_list):
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
        if to_email and subject and _open_windows_outlook_com(sender_email, to_email, subject, html, text, att_list):
            return "Outlook Desktop"
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


# ---- Mail-only mode: desktop mail app, New Outlook and provider detection -----------------
# The classic Outlook script, but it reports back whether Outlook sent the mail ("sent") or only
# opened it for the person to send ("opened"). With auto_send off it always only opens the mail.
_MAIL_OUTLOOK_SCRIPT = _CLASSIC_OUTLOOK_SCRIPT.replace(
    "        if matchedAcc is not missing value then\n            send msg\n        else\n            open msg\n            activate\n        end if\n",
    "        if matchedAcc is not missing value and autoSend is \"1\" then\n            send msg\n            return \"sent\"\n"
    "        else\n            open msg\n            activate\n            return \"opened\"\n        end if\n",
).replace(
    "    set senderEmail to item 5 of argv\n",
    "    set senderEmail to item 5 of argv\n    set autoSend to item 6 of argv\n",
).replace("repeat with i from 6 to (count of argv)", "repeat with i from 7 to (count of argv)")
_MAIL_SCRIPT_OK = 'return "sent"' in _MAIL_OUTLOOK_SCRIPT and "item 6 of argv" in _MAIL_OUTLOOK_SCRIPT

_WINDOWS_OUTLOOK_OPEN_PS = _WINDOWS_OUTLOOK_PS.replace("$mail.Send()", "$mail.Display()")


def _is_new_outlook() -> bool:
    """True when the computer uses 'New Outlook', which cannot be scripted or open ready-to-send .eml drafts."""
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["defaults", "read", "com.microsoft.Outlook", "IsRunningNewOutlook"],
                               capture_output=True, text=True, check=False, timeout=5)
            return r.returncode == 0 and r.stdout.strip() == "1"
        if sys.platform == "win32":
            r = subprocess.run(["reg", "query", r"HKCU\Software\Microsoft\Office\16.0\Outlook\Preferences",
                                "/v", "UseNewOutlook"], capture_output=True, text=True, check=False, timeout=5)
            return r.returncode == 0 and bool(re.search(r"UseNewOutlook\s+REG_DWORD\s+0x1\b", r.stdout))
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def launch_desktop_mail(
    eml_path: Path,
    *,
    sender_email: str = "",
    to_email: str = "",
    to_name: str = "",
    subject: str = "",
    html: str = "",
    text: str = "",
    attachments: Sequence[Path] | None = None,
    auto_send: bool = True,
) -> dict[str, Any]:
    """Hand a mail to the computer's mail app (mail-only mode).

    Returns ``{"app", "sent", "saved_only", "note"}``: ``sent`` is True only when classic Outlook sent
    the mail itself; otherwise the mail is open for the person to click Send. ``saved_only`` means no
    app could open it (for example New Outlook on Windows), so the person downloads the .eml instead.
    """
    if not eml_path.is_file():
        raise OutlookError(f"Draft file not found: {eml_path.name}")
    att_list = [p for p in (attachments or []) if p.is_file()]
    if _desktop_launcher is not None:
        r = _desktop_launcher(eml_path, sender_email=sender_email, to_email=to_email, to_name=to_name,
                              subject=subject, html=html, text=text, attachments=att_list)
        if isinstance(r, dict):
            return {"app": str(r.get("app", "")), "sent": bool(r.get("sent")),
                    "saved_only": bool(r.get("saved_only")), "note": str(r.get("note", ""))}
        return {"app": str(r), "sent": False, "saved_only": False, "note": ""}

    path_str = str(eml_path)
    new_outlook = _is_new_outlook()
    if sys.platform == "darwin":
        has_outlook = subprocess.run(["open", "-Ra", "Microsoft Outlook"], capture_output=True, check=False,
                                     timeout=5).returncode == 0
        note = ""
        if has_outlook and not new_outlook:
            if to_email and subject and _MAIL_SCRIPT_OK:
                cmd = ["osascript", "-", subject, html or text, to_name, to_email, sender_email,
                       "1" if auto_send else "0", *(str(p) for p in att_list)]
                res = subprocess.run(cmd, input=_MAIL_OUTLOOK_SCRIPT, capture_output=True, text=True,
                                     check=False, timeout=20)
                if res.returncode == 0:
                    return {"app": "Microsoft Outlook", "sent": res.stdout.strip() == "sent",
                            "saved_only": False, "note": ""}
            res = subprocess.run(["open", "-a", "Microsoft Outlook", path_str], capture_output=True, text=True,
                                 check=False, timeout=12)
            if res.returncode == 0:
                return {"app": "Microsoft Outlook", "sent": False, "saved_only": False, "note": ""}
        elif has_outlook:
            note = "New Outlook cannot open ready-to-send drafts, so the mail was opened in Apple Mail instead."
        res_mail = subprocess.run(["open", "-a", "Mail", path_str], capture_output=True, text=True, check=False,
                                  timeout=12)
        if res_mail.returncode == 0:
            return {"app": "Apple Mail", "sent": False, "saved_only": False, "note": note}
        return {"app": "", "sent": False, "saved_only": True,
                "note": "No mail app could open the mail. Download the .eml file and open it in your mail app."}

    if sys.platform == "win32":
        if to_email and subject:
            script = _WINDOWS_OUTLOOK_PS if auto_send else _WINDOWS_OUTLOOK_OPEN_PS
            ps_exe = shutil.which("powershell") or shutil.which("pwsh")
            if ps_exe:
                env = dict(os.environ)
                env.update(HR_PS_SENDER=sender_email or "", HR_PS_TO=to_email or "", HR_PS_SUBJ=subject or "",
                           HR_PS_HTML=html or "", HR_PS_TEXT=text or "",
                           HR_PS_ATTACHMENTS="\n".join(str(p) for p in att_list))
                res = subprocess.run([ps_exe, "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                                     capture_output=True, text=True, check=False, timeout=20)
                if res.returncode == 0:
                    return {"app": "Outlook Desktop", "sent": auto_send, "saved_only": False, "note": ""}
        if new_outlook:
            return {"app": "", "sent": False, "saved_only": True,
                    "note": "New Outlook cannot open ready-to-send drafts. Download the .eml file, or choose "
                            "Microsoft sign-in or SMTP sending in Mail settings."}
        try:
            os.startfile(path_str)  # type: ignore[attr-defined]
            return {"app": "Outlook Desktop", "sent": False, "saved_only": False, "note": ""}
        except OSError as exc:
            raise OutlookError(f"Could not open desktop Outlook/Mail app: {exc}") from exc

    opener = shutil.which("xdg-open")
    if not opener:
        return {"app": "", "sent": False, "saved_only": True,
                "note": "No mail app was found on this computer. Download the .eml file instead."}
    subprocess.Popen([opener, path_str], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"app": "Desktop Mail", "sent": False, "saved_only": False, "note": ""}


_KNOWN_DOMAINS = {
    "gmail.com": "google", "googlemail.com": "google",
    "outlook.com": "microsoft", "hotmail.com": "microsoft", "live.com": "microsoft", "msn.com": "microsoft",
}
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+$")


def lookup_mx(domain: str) -> list[str]:
    """The mail servers (MX records) of a domain, using the system's nslookup. Empty when unknown."""
    if _mx_resolver is not None:
        return [h.rstrip(".").lower() for h in _mx_resolver(domain)]
    exe = shutil.which("nslookup")
    if not exe:
        return []
    try:
        res = subprocess.run([exe, "-type=mx", domain], capture_output=True, text=True, check=False, timeout=8)
    except (OSError, subprocess.SubprocessError):
        return []
    hosts = re.findall(r"mail exchanger\s*=\s*(?:\d+\s+)?([A-Za-z0-9.-]+)", res.stdout)
    return [h.rstrip(".").lower() for h in hosts]


def detect_provider(email_addr: str) -> dict[str, Any]:
    """Guess whether an address is on Google, Microsoft or another mail server, from its domain."""
    addr = _safe_header(email_addr).lower()
    if not checks.EMAIL_RE.fullmatch(addr):
        raise ValueError("Enter a valid email address first.")
    domain = addr.rsplit("@", 1)[1]
    if not _DOMAIN_RE.fullmatch(domain):
        raise ValueError("That email address has an unusual domain. Choose the provider yourself.")
    if domain in _KNOWN_DOMAINS:
        provider = _KNOWN_DOMAINS[domain]
        return {"provider": provider, "domain": domain, "mx": [], "found": True, **SMTP_PRESETS[provider]}
    mx = lookup_mx(domain)
    if not mx:
        return {"provider": "", "domain": domain, "mx": [], "found": False}
    if any(h.endswith(("google.com", "googlemail.com")) for h in mx):
        provider = "google"
    elif any(h.endswith(("outlook.com", "office365.com")) or "protection.outlook" in h for h in mx):
        provider = "microsoft"
    else:
        provider = "custom"
    out: dict[str, Any] = {"provider": provider, "domain": domain, "mx": mx[:5], "found": True}
    out.update(SMTP_PRESETS.get(provider, {}))
    return out
