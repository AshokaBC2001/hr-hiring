"""Tests for the mail-only mode improvements: confirming sent mails, the test inbox, retry and resend,
mail providers (Microsoft, Google, any SMTP server), the size check, register checks and exports,
reading replies, bulk invitations, shrinking the outbox and adding students by hand."""
import email
import email.policy
import io
import json
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_mail import HR, REQUIRED, add, mail, read_all  # noqa: E402,F401  (mail is the fixture)
from hiring import config, db, invitation, llm, mailer, outlook  # noqa: E402


def _smtp_on(captured: list, provider: str = "microsoft") -> None:
    outlook.set_smtp_backend(lambda msg, cfg: (captured.append((msg, cfg)), "<id@test>")[1])
    outlook.save_settings({"mode": "smtp", "provider": provider, "sender_email": "hr@example.org",
                           "smtp_password": "pw"})


def _ready(name: str = "arjun_mehta.txt") -> str:
    no = add(name)
    read_all()
    return no


# ---- 1. "Mail sent" only after the mail really went out ------------------------------------------
def test_an_opened_mail_waits_for_i_sent_it_and_keeps_a_history(mail):
    no = _ready()
    opened = invitation.send(no)                                  # .eml mode in the tests
    assert opened["status"] == "opened" and opened["opened_at"]
    row = invitation.row(db.get_candidate(no))
    assert row["status"] == "Opened, not confirmed" and row["mail_date"] == "" and not row["can_mail"]
    assert invitation.register_data("waiting")[1]["waiting"] == 1

    back = invitation.back_to_draft(no)                           # it was not sent after all
    assert back["status"] == "draft" and back["history"][-1]["status"] == "not sent"
    invitation.save_draft(no, back["subject"], back["body"] + "\nPS", back["visit_date"], back["attachments"])
    assert len(invitation.draft(db.get_candidate(no))["history"]) == 1    # editing keeps the history

    invitation.send(no)
    sent = invitation.confirm_sent(no)
    assert sent["status"] == "sent" and sent["confirmed_at"]
    row = invitation.row(db.get_candidate(no))
    assert row["status"] == "Mail sent" and row["mail_date"] == date.today().isoformat()

    again = invitation.resend(no)
    assert again["status"] == "draft" and again["resend_of"] == sent["sent_at"]
    assert invitation.row(db.get_candidate(no))["status"] == "Mail sent"     # the register keeps the first send
    with pytest.raises(invitation.MailProblem, match="no opened mail"):
        invitation.confirm_sent(no)


def test_smtp_sends_count_as_sent_at_once(mail):
    captured: list = []
    _smtp_on(captured)
    no = _ready()
    sent = invitation.send(no)
    assert sent["status"] == "sent" and sent["how"] == "smtp" and len(captured) == 1
    assert invitation.row(db.get_candidate(no))["status"] == "Mail sent"


def test_classic_outlook_sending_by_itself_counts_as_sent(mail, monkeypatch):
    monkeypatch.setattr(config, "OUTLOOK_MODE", "outlook_app")
    outlook.set_desktop_launcher(lambda p, **kw: {"app": "Microsoft Outlook", "sent": True})
    no = _ready()
    sent = invitation.send(no)
    assert sent["status"] == "sent" and sent["opened_in"] == "Microsoft Outlook"
    assert invitation.row(db.get_candidate(no))["mail_date"] == date.today().isoformat()


def test_when_no_mail_app_can_open_it_the_eml_is_offered(mail, monkeypatch):
    monkeypatch.setattr(config, "OUTLOOK_MODE", "outlook_app")
    outlook.set_desktop_launcher(lambda p, **kw: {"app": "", "saved_only": True, "note": "New Outlook cannot open it."})
    no = _ready()
    inv = invitation.send(no)
    assert inv["status"] == "opened" and inv["saved_only"] and inv["note"] == "New Outlook cannot open it."
    with pytest.raises(invitation.MailProblem, match="New Outlook"):
        invitation.open_sent_eml(no)


def test_opening_again_never_sends_by_itself(mail, monkeypatch):
    monkeypatch.setattr(config, "OUTLOOK_MODE", "outlook_app")
    calls: list = []
    outlook.set_desktop_launcher(lambda p, **kw: (calls.append(kw), "Apple Mail")[1])
    no = _ready()
    invitation.send(no)
    assert invitation.open_sent_eml(no)["opened_in"] == "Apple Mail" and len(calls) == 2


# ---- 2. the test inbox ------------------------------------------------------------------------------
def test_a_mail_to_the_test_inbox_does_not_change_the_register(mail):
    captured: list = []
    _smtp_on(captured)
    outlook.save_settings({"test_recipient": "tester@example.org"})
    no = _ready()
    res = invitation.send(no)
    assert res["test_only"] and res["status"] == "draft" and res["last_test"]["sent_to"] == "tester@example.org"
    assert captured[0][0]["To"].endswith("<tester@example.org>")
    row = invitation.row(db.get_candidate(no))
    assert row["status"] == "Not mailed" and row["mail_date"] == "" and row["test_sent"] and row["can_mail"]
    outlook.save_settings({"test_recipient": ""})                  # now for real
    assert invitation.send(no)["status"] == "sent"
    assert invitation.row(db.get_candidate(no))["status"] == "Mail sent"


# ---- 3. a failed try is kept so it can be tried again -----------------------------------------------
def test_a_failed_send_is_kept_with_its_error_and_can_be_retried(mail, monkeypatch):
    monkeypatch.setattr(config, "OUTLOOK_MODE", "outlook_app")

    def broken(*a, **kw):
        raise outlook.OutlookError("Outlook is not installed")

    outlook.set_desktop_launcher(broken)
    no = _ready()
    with pytest.raises(invitation.MailProblem, match="not installed"):
        invitation.send(no)
    inv = invitation.draft(db.get_candidate(no))
    assert inv["status"] == "draft" and "not installed" in inv["last_error"] and inv["history"][-1]["how"] == "failed"
    outlook.set_desktop_launcher(lambda p, **kw: "Apple Mail")
    ok = invitation.send(no)
    assert ok["status"] == "opened" and ok["last_error"] == "" and len(ok["history"]) == 2


# ---- 4. the password is kept in the keychain ---------------------------------------------------------
def test_the_password_goes_to_the_keychain_not_the_database(mail):
    outlook.save_settings({"mode": "smtp", "sender_email": "hr@example.org", "smtp_password": "s3cret"})
    assert db.get_setting("outlook_smtp_password") == ""
    assert db.get_setting("outlook_smtp_password_store") == "keychain"
    outlook._secret_cache.clear()
    assert outlook.raw_settings()["smtp_password"] == "s3cret"
    assert outlook.public_settings()["password_store"] == "keychain"
    outlook.save_settings({"mode": "auto", "clear_smtp_password": True})
    outlook._secret_cache.clear()
    assert outlook.raw_settings()["smtp_password"] == "" and outlook._secret_store.data == {}


def test_without_a_keychain_the_password_stays_in_the_database(mail, monkeypatch):
    outlook.set_secret_store(None)
    monkeypatch.setattr(config, "SECRET_STORE", "db")
    outlook.save_settings({"mode": "smtp", "sender_email": "hr@example.org", "smtp_password": "s3cret"})
    assert db.get_setting("outlook_smtp_password") == "s3cret"
    assert outlook.public_settings()["password_store"] == "database"


# ---- 5. and 6. any email provider ----------------------------------------------------------------------
def test_choosing_google_fills_in_the_gmail_server_and_skips_microsoft_sign_in(mail):
    class _FakeApp:
        def get_accounts(self):
            return [{"username": "someone@example.org", "name": "Someone"}]

        def acquire_token_silent(self, scopes, account=None):
            return {"access_token": "tok"}

    outlook.set_msal_factory(lambda cid, auth, cache: _FakeApp())
    cfg = outlook.save_settings({"mode": "auto", "client_id": "11111111-2222-3333-4444-555555555555",
                                 "provider": "google", "sender_email": "me@school.example", "smtp_password": "abcd"})
    assert (cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_security"]) == ("smtp.gmail.com", 587, "starttls")
    assert cfg["provider"] == "google" and cfg["effective_mode"] == "smtp"      # not Graph, although signed in
    cfg = outlook.save_settings({"provider": "microsoft"})
    assert cfg["smtp_host"] == "smtp.office365.com" and cfg["effective_mode"] == "graph"


def test_other_servers_use_their_own_host_port_and_ssl(mail):
    cfg = outlook.save_settings({"mode": "auto", "provider": "custom", "sender_email": "me@uni.example",
                                 "smtp_host": "mail.uni.example", "smtp_port": 465, "smtp_security": "ssl",
                                 "smtp_password": "pw"})
    assert (cfg["smtp_host"], cfg["smtp_port"], cfg["smtp_security"], cfg["effective_mode"]) == \
        ("mail.uni.example", 465, "ssl", "smtp")
    with pytest.raises(ValueError, match="SMTP server name"):
        outlook.save_settings({"provider": "custom", "smtp_host": "", "smtp_password": "pw"})
    with pytest.raises(ValueError, match="security"):
        outlook.save_settings({"smtp_security": "magic"})
    with pytest.raises(ValueError, match="provider"):
        outlook.save_settings({"provider": "pigeon"})


def test_the_mail_app_provider_needs_no_password(mail):
    cfg = outlook.save_settings({"mode": "auto", "provider": "desktop", "sender_email": "me@any.example"})
    assert cfg["mode"] == "outlook_app" and cfg["effective_mode"] == "outlook_app"
    cfg = outlook.save_settings({"provider": "microsoft"})
    assert cfg["mode"] == "auto"


def test_the_full_mode_dialog_keeps_its_settings(mail):
    cfg = outlook.save_settings({"mode": "auto", "smtp_host": "smtp.office365.com", "sender_email": "a@b.example"})
    assert cfg["provider"] == "microsoft" and not cfg["provider_chosen"]
    assert db.get_setting("outlook_provider") == ""


def test_ssl_on_port_465_is_used_for_sending(mail, monkeypatch):
    opened: list = []

    class FakeSSL:
        def __init__(self, host, port, timeout=None, context=None):
            opened.append(("ssl", host, port, context is not None))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def login(self, user, pw):
            opened.append(("login", user))

        def send_message(self, msg):
            opened.append(("sent", msg["To"]))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    outlook.save_settings({"mode": "smtp", "provider": "custom", "sender_email": "me@uni.example",
                           "smtp_host": "mail.uni.example", "smtp_port": 465, "smtp_security": "ssl", "smtp_password": "pw"})
    no = _ready()
    assert invitation.send(no)["status"] == "sent"
    assert opened[0] == ("ssl", "mail.uni.example", 465, True) and opened[1] == ("login", "me@uni.example")


def test_login_errors_are_explained_for_each_provider(mail):
    err = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    assert "App Password" in outlook.smtp_error_text(err, {"provider": "google"})
    assert "SMTP AUTH" in outlook.smtp_error_text(err, {"provider": "microsoft"})
    assert "IT team" in outlook.smtp_error_text(err, {"provider": "custom"})
    import socket
    assert "server name" in outlook.smtp_error_text(socket.gaierror("nope"), {"smtp_host": "x.example"})

    def failing(msg, cfg):
        raise err

    outlook.set_smtp_backend(failing)
    outlook.save_settings({"mode": "smtp", "provider": "google", "sender_email": "me@gmail.com", "smtp_password": "x"})
    no = _ready()
    with pytest.raises(invitation.MailProblem, match="App Password"):
        invitation.send(no)


def test_detect_finds_the_provider_from_the_domain(mail):
    seen: list = []

    def mx(domain):
        seen.append(domain)
        return {"school.example": ["aspmx.l.google.com."], "corp.example": ["corp-example.mail.protection.outlook.com"],
                "own.example": ["mx1.own.example"]}.get(domain, [])

    outlook.set_mx_resolver(mx)
    assert outlook.detect_provider("a@gmail.com")["provider"] == "google" and seen == []   # known, no lookup
    g = outlook.detect_provider("a@school.example")
    assert g["provider"] == "google" and g["smtp_host"] == "smtp.gmail.com"
    assert outlook.detect_provider("a@corp.example")["provider"] == "microsoft"
    own = outlook.detect_provider("a@own.example")
    assert own["provider"] == "custom" and own["mx"] == ["mx1.own.example"]
    assert outlook.detect_provider("a@nowhere.example")["found"] is False
    with pytest.raises(ValueError):
        outlook.detect_provider("not-an-email")
    r = mail.post("/api/mail/detect", headers=HR, json={"email": "x@school.example"})
    assert r.status_code == 200 and r.json()["provider"] == "google"
    assert mail.post("/api/mail/detect", headers=HR, json={"email": "nope"}).status_code == 400


def test_nslookup_output_is_read_on_mac_and_windows(monkeypatch):
    class R:
        def __init__(self, out):
            self.stdout = out

    outlook.set_mx_resolver(None)
    monkeypatch.setattr(outlook.shutil, "which", lambda name: "/usr/bin/nslookup")
    monkeypatch.setattr(outlook.subprocess, "run", lambda *a, **k: R(
        "school.example\tmail exchanger = 10 aspmx.l.google.com.\n"
        "school.example\tMX preference = 20, mail exchanger = alt1.aspmx.l.google.com\n"))
    assert outlook.lookup_mx("school.example") == ["aspmx.l.google.com", "alt1.aspmx.l.google.com"]


# ---- 7. Gmail link, 8. size check --------------------------------------------------------------------
def test_the_candidate_page_has_gmail_and_outlook_web_links(mail):
    no = _ready()
    d = mail.get(f"/api/candidates/{no}").json()
    assert d["mail_links"]["gmail"].startswith("https://mail.google.com/mail/?view=cm")
    assert "arjun.mehta%40example.com" in d["mail_links"]["gmail"] and d["mail_links"]["outlook_office"]


def test_a_mail_that_is_too_big_is_stopped_before_sending(mail, monkeypatch):
    captured: list = []
    _smtp_on(captured, provider="google")
    monkeypatch.setitem(outlook.MAIL_SIZE_LIMITS, "google", 1024 * 1024)        # 1 MB for the test
    no = _ready()
    with pytest.raises(invitation.MailProblem, match="Google accepts mails up to about 1.0 MB"):
        invitation.send(no)
    assert captured == []
    assert outlook.public_settings()["size_limit"] == 1024 * 1024


# ---- 9. register checks --------------------------------------------------------------------------------
def test_register_fields_are_checked(mail):
    a, b = _ready("arjun_mehta.txt"), _ready("rahul_nair.txt")
    with pytest.raises(invitation.MailProblem, match="NIC"):
        invitation.save_row(a, {"nic": "12345"})
    with pytest.raises(invitation.MailProblem, match="Tel No"):
        invitation.save_row(a, {"tel": "12"})
    r = invitation.save_row(a, {"nic": "200112345678", "tel": "+94 77-123-4567", "vehicle": "wp cab 1234"})
    assert (r["nic"], r["tel"], r["vehicle"], r["warnings"]) == ("200112345678", "077 123 4567", "WP CAB 1234", [])
    assert r["nic_hint"] == "Birth year 2001 (from the NIC)"
    with pytest.raises(invitation.MailProblem, match="already saved for"):
        invitation.save_row(b, {"nic": "2001 1234 5678"})
    w = invitation.save_row(b, {"nic": "N1234567", "vehicle": "something odd"})
    assert w["nic"] == "N1234567" and any("number plate" in x for x in w["warnings"])
    assert invitation.save_row(b, {"nic": "AB12C345"})["warnings"][0].startswith('"AB12C345" was saved as a passport')
    assert invitation.save_row(b, {"tel": "0771234567, 0112345678"})["tel"] == "077 123 4567, 011 234 5678"


# ---- 10. register filters and exports ---------------------------------------------------------------
def test_register_filters_and_csv_export(mail):
    from openpyxl import load_workbook

    a, b = _ready("arjun_mehta.txt"), _ready("rahul_nair.txt")
    invitation.save_row(a, {"source": "Mr.Dinesh", "address": "=HYPERLINK(\"http://bad\")", "status": "Replied"})
    invitation.save_row(b, {"source": "Ms.Perera", "laptop": "+SUM(1)"})
    invitation.send(a)
    invitation.confirm_sent(a)
    today = date.today().isoformat()
    assert [r["app_no"] for r in invitation.rows(date_from=today)] == [a]
    assert invitation.rows(date_to=(date.today() - timedelta(days=1)).isoformat()) == []
    assert [r["app_no"] for r in invitation.rows(source="perera")] == [b]
    assert [r["app_no"] for r in invitation.rows(status="Replied")] == [a]     # set before the mail: kept
    with pytest.raises(invitation.MailProblem, match="not valid"):
        invitation.rows(date_from="yesterday")

    data = invitation.export_csv(invitation.rows())
    assert data.startswith("﻿".encode("utf-8"))
    text = data.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "'+SUM(1)" in text
    ws = load_workbook(io.BytesIO(invitation.export_xlsx(invitation.rows()))).active
    cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=HYPER")]
    assert cells and cells[0].data_type == "s"                        # text, not a formula

    r = mail.get("/api/register.csv?source=Dinesh")
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"] and "Arjun Mehta" in r.content.decode("utf-8-sig")
    reg = mail.get(f"/api/register?date_from={today}").json()
    assert len(reg["rows"]) == 1 and "Mr.Dinesh" in reg["sources"] and "Opened, not confirmed" in reg["statuses"]


# ---- 11. retry/resend routes --------------------------------------------------------------------------
def test_confirm_back_and_resend_routes(mail):
    no = _ready()
    assert mail.post(f"/api/candidates/{no}/mail/send", headers=HR, json={}).json()["invite"]["status"] == "opened"
    assert mail.post(f"/api/candidates/{no}/mail/back-to-draft", headers=HR).json()["invite"]["status"] == "draft"
    mail.post(f"/api/candidates/{no}/mail/send", headers=HR, json={})
    assert mail.post(f"/api/candidates/{no}/mail/confirm", headers=HR).json()["invite"]["status"] == "sent"
    assert mail.post(f"/api/candidates/{no}/mail/resend", headers=HR).json()["invite"]["status"] == "draft"
    assert mail.post(f"/api/candidates/{no}/mail/confirm", headers={"X-Role": "interviewer"}).status_code == 400


# ---- 12. reading the reply -------------------------------------------------------------------------
REPLY = """Dear Sir,

Thank you. My details are below.
NIC: 200112345678
Mobile - 077 123 4567
Laptop serial no: 5CD1234XYZ
I live at 12, Galle Road, Colombo 3.

Kind regards"""


def test_details_are_read_from_a_pasted_reply(mail):
    no = _ready()
    llm.set_backend(lambda messages, json_mode: json.dumps(
        {"address": "12, Galle Road, Colombo 3", "vehicle": "WP CAB-9999", "laptop": ""}))
    r = invitation.read_reply(no, REPLY)
    f = r["fields"]
    assert (f["nic"], f["tel"], f["laptop"], f["status"]) == ("200112345678", "077 123 4567", "5CD1234XYZ", "Replied")
    assert f["address"] == "12, Galle Road, Colombo 3" and r["found_by"]["address"] == "AI"
    assert "vehicle" not in f                                    # made up by the model: not in the reply
    assert invitation.row(db.get_candidate(no))["nic"] == ""       # nothing is saved until Save details
    with pytest.raises(invitation.MailProblem, match="Paste"):
        invitation.read_reply(no, "   ")
    assert mail.post(f"/api/candidates/{no}/reply", headers=HR, json={"text": REPLY}).json()["fields"]["nic"] == "200112345678"


# ---- 13. bulk invitations ------------------------------------------------------------------------------
def test_bulk_prepare_sets_one_visit_date_and_leaves_out_mailed_students(mail):
    a, b = _ready("arjun_mehta.txt"), _ready("rahul_nair.txt")
    c = add("daniel_kim.txt")                                       # still being read
    invitation.send(b)
    r = invitation.bulk_prepare([a, b, c, "APP-9999"], "2026-12-29", ["Hospital_system.pdf"])
    assert r["ready"] == [a] and not r["direct"]
    reasons = {x["app_no"]: x["reason"] for x in r["results"] if not x["ok"]}
    assert "confirm it first" in reasons[b] and "still reading" in reasons[c] and reasons["APP-9999"] == "Not found."
    d = invitation.draft(db.get_candidate(a))
    assert "29-DEC-2026  (Tuesday)" in d["body"] and d["attachments"] == ["Hospital_system.pdf"]
    assert invitation.bulk_prepare([a], "2027-01-05", REQUIRED)["ready"] == [a]
    d = invitation.draft(db.get_candidate(a))
    assert "05-JAN-2027  (Tuesday)" in d["body"] and "29-DEC-2026" not in d["body"]
    with pytest.raises(invitation.MailProblem, match="visit date"):
        invitation.bulk_prepare([a], "", [])
    with pytest.raises(invitation.MailProblem, match="Microsoft sign-in or an SMTP password"):
        invitation.bulk_start([a])


def test_bulk_sending_goes_one_by_one_and_reports_each(mail, monkeypatch):
    monkeypatch.setattr(config, "MAIL_BULK_DELAY_SECONDS", 0)
    captured: list = []
    _smtp_on(captured)
    a, b = _ready("arjun_mehta.txt"), _ready("rahul_nair.txt")
    db.update_candidate(db.get_candidate(b).id, email="")            # this one fails
    r = mail.post("/api/mail/bulk/prepare", headers=HR, json={"app_nos": [a, b], "visit_date": "2026-12-29",
                                                               "attachments": REQUIRED}).json()
    assert r["ready"] == [a] and r["direct"]
    db.update_candidate(db.get_candidate(b).id, email="rahul@example.org")
    job = mail.post("/api/mail/bulk/send", headers=HR, json={"app_nos": [a, b]}).json()
    for _ in range(100):
        job = mail.get(f"/api/mail/bulk/{job['id']}").json()
        if not job["running"]:
            break
        time.sleep(0.05)
    assert not job["running"] and job["sent"] == 2 and job["failed"] == 0 and len(captured) == 2
    assert all(invitation.row(db.get_candidate(x))["status"] == "Mail sent" for x in (a, b))
    assert mail.get("/api/mail/bulk/unknown").status_code == 400


# ---- 14. shrinking old copies ------------------------------------------------------------------------
def test_old_sent_copies_are_shrunk_but_waiting_ones_are_not(mail):
    a, b = _ready("arjun_mehta.txt"), _ready("rahul_nair.txt")
    invitation.send(a)
    invitation.confirm_sent(a)
    invitation.send(b)                                               # opened, not confirmed
    fa, fb = (invitation.draft(db.get_candidate(x))["file"] for x in (a, b))
    size_a, size_b = (db.paths.outbox / fa).stat().st_size, (db.paths.outbox / fb).stat().st_size
    assert invitation.shrink_outbox()["shrunk"] == 0                 # too new
    old = (datetime.now() - timedelta(days=200)).replace(microsecond=0).isoformat()
    c = db.get_candidate(a)
    inv = dict(c.invite, sent_at=old, history=[dict(h, confirmed_at=old) for h in c.invite["history"]])
    db.update_candidate(c.id, invite=inv)
    r = invitation.shrink_outbox()
    assert r["shrunk"] == 1 and r["saved_bytes"] > 0
    assert (db.paths.outbox / fa).stat().st_size < size_a / 4
    assert (db.paths.outbox / fb).stat().st_size == size_b
    msg = email.message_from_bytes((db.paths.outbox / fa).read_bytes(), policy=email.policy.default)
    assert msg["X-HR-Slimmed"] == "1" and not list(msg.iter_attachments())
    assert "Hospital_system.pdf" in msg.get_body(("plain",)).get_content()
    assert invitation.shrink_outbox(keep_days=0)["shrunk"] == 0      # already small; waiting one untouched
    assert mail.post("/api/mail/outbox/shrink", headers=HR).json()["files"] == 2


# ---- 15. adding a student by hand ------------------------------------------------------------------
def test_a_student_can_be_added_and_mailed_without_a_cv(mail):
    r = mail.post("/api/mail/students", headers=HR, json={"name": "Nimali Silva", "email": "nimali@example.org",
                                                          "university": "SLIIT", "technology": "Java", "source": "Ms.Perera"})
    assert r.status_code == 200, r.text
    no = r.json()["app_no"]
    row = invitation.row(db.get_candidate(no))
    assert (row["name"], row["university"], row["technology"], row["source"]) == ("Nimali Silva", "SLIIT", "Java", "Ms.Perera")
    assert invitation.send(no)["status"] == "opened"
    dup = mail.post("/api/mail/students", headers=HR, json={"name": "Again", "email": "NIMALI@example.org"})
    assert dup.status_code == 400 and "already in the register" in dup.json()["detail"]
    assert mail.post("/api/mail/students", headers=HR, json={"name": "", "email": "x@example.org"}).status_code == 400
    assert mail.get(f"/api/candidates/{no}/cv").status_code == 400


def test_an_unread_cv_can_be_typed_in_by_hand(mail):
    no = add("arjun_mehta.txt")                                      # the Recruiter has not read it
    c = db.get_candidate(no)
    assert invitation.email_in_cv(c) == "arjun.mehta@example.com"
    assert mail.get(f"/api/candidates/{no}").json()["email_in_cv"] == "arjun.mehta@example.com"
    r = mail.post(f"/api/candidates/{no}/by-hand", headers=HR, json={"name": "Arjun Mehta", "email": "arjun.mehta@example.com",
                                                                    "university": "IIT", "technology": "AI/ML"})
    assert r.status_code == 200, r.text
    c = db.get_candidate(no)
    assert c.status == "in_pool" and c.profile_source == "Typed by HR officer" and invitation.row(c)["technology"] == "AI/ML"
    read_all()                                                       # the Recruiter does not overwrite it
    assert db.get_candidate(no).profile_source == "Typed by HR officer"
    assert invitation.send(no)["status"] == "opened"
    with pytest.raises(invitation.MailProblem, match="already been read"):
        invitation.fill_by_hand(no, {"name": "X", "email": "x@example.org"})
