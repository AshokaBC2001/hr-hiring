"""Tests for mail-only mode: the invitation mail, its attachments and the intern register."""
import email
import email.policy
import io
import sys
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_model import FakeModel  # noqa: E402
from hiring import actions, config, db, flows, invitation, llm, tools  # noqa: E402
from hiring.server import app  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
HR = {"X-Role": "hr"}
PDF = b"%PDF-1.4\n% made-up test file\n"
REQUIRED = ["Hospital_system.pdf", "individual user manual.pdf", "POS Task Backlog.xlsx", "Trainee_Guidelines_Agreement.pdf"]


@pytest.fixture()
def mail(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", False)
    monkeypatch.setattr(config, "MODE", "mail")
    db.setup(tmp_path / "data")
    llm.set_backend(FakeModel())
    flows.NOTICE.update(text="", at="")
    with TestClient(app) as c:
        yield c
    llm.set_backend(None)


def add(name: str, source: str = "") -> str:
    p = SAMPLES / name
    r = actions.add_cv(p.name, p.read_bytes(), "", source)
    assert r["ok"], r
    return r["app_no"]


def read_all() -> None:
    flows.run_employee("recruiter", limit=20, wait=True)


# ---- reading the CV ---------------------------------------------------------------------------
def test_the_university_is_read_from_the_education_lines():
    f = tools.find_university
    assert f(["B.Tech in Data Science, Indian Institute of Technology Madras (2021 - 2025)"]) == \
        "Indian Institute of Technology Madras"
    assert f(["BSc (Hons) in Software Engineering - SLIIT, 2022-2026"]) == "SLIIT"
    assert f(["Diploma in Information Technology at ESOFT Metro Campus"]) == "ESOFT Metro Campus"
    assert f(["BSc IT at SLIIT"]) == "SLIIT"
    assert f(["Advanced Level (Physical Science)"]) == ""


def test_source_and_university_are_saved_with_the_cv(mail):
    no = add("arjun_mehta.txt", "Mr.Dinesh")
    read_all()
    c = db.get_candidate(no)
    assert c.source == "Mr.Dinesh" and c.university == "Vellore Institute of Technology"
    row = invitation.row(c)
    assert row["technology"] == "AI/ML" and row["status"] == "Not mailed" and row["name"] == "Arjun Mehta"


# ---- the mail -----------------------------------------------------------------------------------
def test_the_visit_date_is_the_next_tuesday():
    assert invitation.next_visit_day(date(2026, 11, 25)) == date(2026, 12, 1)     # Wednesday
    assert invitation.next_visit_day(date(2026, 12, 1)) == date(2026, 12, 8)      # a Tuesday: the next one
    assert invitation.visit_text(date(2026, 12, 29)) == "29-DEC-2026  (Tuesday)"


def test_a_new_mail_is_filled_from_the_template(mail):
    invitation.add_attachment("Trainee_Guidelines_Agreement.pdf", PDF)
    no = add("arjun_mehta.txt")
    read_all()
    d = invitation.draft(db.get_candidate(no))
    assert d["status"] == "new" and d["subject"].startswith("Software internship (six months)")
    assert "Dear Intern" in d["body"] and "{visit_date}" not in d["body"]
    assert invitation.visit_text(date.fromisoformat(d["visit_date"])) in d["body"]
    assert "https://sltinternguide.vercel.app" in d["body"] and "Best regards\nGiridaran" in d["body"]
    assert d["attachments"] == REQUIRED                                  # the required files come with it


def test_the_mail_can_be_edited_and_is_checked(mail):
    no = add("arjun_mehta.txt")
    read_all()
    with pytest.raises(invitation.MailProblem, match="subject"):
        invitation.save_draft(no, "", "Text", "2026-12-29", [])
    with pytest.raises(invitation.MailProblem, match="Pick the visit date"):
        invitation.save_draft(no, "Subject", "Date -{visit_date}", "2026-12-29", [])
    with pytest.raises(invitation.MailProblem, match="not valid"):
        invitation.save_draft(no, "Subject", "Text", "29/12/2026", [])
    saved = invitation.save_draft(no, "My subject 29-DEC", "My own text", "2026-12-29", ["not-uploaded.pdf"])
    assert saved["attachments"] == []                                   # only uploaded files can be attached
    assert invitation.draft(db.get_candidate(no))["body"] == "My own text"


def test_sending_saves_the_mail_with_its_attachments_and_fills_the_register(mail):
    invitation.add_attachment("individual user manual.pdf", PDF)
    invitation.add_attachment("POS Task Backlog.xlsx", b"PK\x03\x04 made-up")
    no = add("arjun_mehta.txt", "Mr.Dinesh")
    with pytest.raises(invitation.MailProblem, match="still reading"):
        invitation.send(no)
    read_all()
    sent = invitation.send(no)
    assert sent["status"] == "sent" and sent["how"] == "eml"
    msg = email.message_from_bytes((db.paths.outbox / sent["file"]).read_bytes(), policy=email.policy.default)
    assert msg["To"] == "Arjun Mehta <arjun.mehta@example.com>" and msg["Subject"].startswith("Software internship")
    html = msg.get_body(("html",)).get_content()
    assert '<a href="https://ternboard.slt.com.lk/">' in html and "CONFIDENTIALITY NOTICE" in html
    assert sorted(p.get_filename() for p in msg.iter_attachments()) == sorted(REQUIRED)
    sent_manual = [p for p in msg.iter_attachments() if p.get_filename() == "individual user manual.pdf"][0]
    assert sent_manual.get_content() == PDF                              # the uploaded newer version is used
    row = invitation.row(db.get_candidate(no))
    assert row["mail_date"] == date.today().isoformat() and row["status"] == "Mail sent"
    with pytest.raises(invitation.MailProblem, match="already sent"):
        invitation.save_draft(no, "x", "y", "", [])


def test_a_mail_needs_an_email_address(mail):
    no = add("arjun_mehta.txt")
    read_all()
    db.update_candidate(db.get_candidate(no).id, email="")
    with pytest.raises(invitation.MailProblem, match="email address"):
        invitation.send(no)


def test_attachments_are_checked(mail):
    assert not invitation.add_attachment("virus.exe", b"x")["ok"]
    assert not invitation.add_attachment("empty.pdf", b"")["ok"]
    assert not invitation.add_attachment("big.pdf", b"x" * (config.MAIL_ATTACH_MAX_BYTES + 1))["ok"]
    assert invitation.add_attachment("../../sneaky.pdf", PDF)["file"] == "sneaky.pdf"
    assert [a["name"] for a in invitation.attachments()] == REQUIRED + ["sneaky.pdf"]
    invitation.remove_attachment("sneaky.pdf")
    assert [a["name"] for a in invitation.attachments()] == REQUIRED


def test_the_required_files_come_with_the_template(mail):
    files = {a["name"]: a for a in invitation.attachments()}
    assert list(files) == REQUIRED and all(a["standard"] and a["size"] > 0 for a in files.values())
    with pytest.raises(invitation.MailProblem, match="comes with the mail template"):
        invitation.remove_attachment("Hospital_system.pdf")
    invitation.add_attachment("Hospital_system.pdf", PDF)                # a newer version
    assert invitation.attachment_path("Hospital_system.pdf").read_bytes() == PDF
    assert [a for a in invitation.attachments() if a["name"] == "Hospital_system.pdf"][0]["replaced"]
    invitation.remove_attachment("Hospital_system.pdf")                  # back to the original
    assert invitation.attachment_path("Hospital_system.pdf").read_bytes()[:5] == b"%PDF-"
    assert invitation.attachment_path("Hospital_system.pdf").read_bytes() != PDF


def test_the_template_can_be_changed_and_reset(mail):
    invitation.save_settings("New subject", "Hello\nDate -{visit_date}")
    assert invitation.settings()["subject"] == "New subject" and invitation.template().startswith("Hello")
    invitation.reset_settings()
    assert invitation.template().startswith("Dear Intern")


# ---- the register -------------------------------------------------------------------------------
def test_the_next_reference_number_is_suggested(mail):
    a, b = add("arjun_mehta.txt"), add("rahul_nair.txt")
    read_all()
    assert invitation.suggest_ref() == ""
    invitation.save_row(a, {"ref_no": "Refno :SLTDPDS-2026/11/03/06", "nic": "200012345678", "tel": "0771234567",
                            "laptop": "SERIAL01", "status": "Replied"})
    assert invitation.suggest_ref() == "Refno :SLTDPDS-2026/11/03/07"
    with pytest.raises(invitation.MailProblem, match="already used"):
        invitation.save_row(b, {"ref_no": "Refno :SLTDPDS-2026/11/03/06"})
    with pytest.raises(invitation.MailProblem, match="status"):
        invitation.save_row(b, {"status": "Maybe"})
    row = invitation.row(db.get_candidate(a))
    assert (row["nic"], row["tel"], row["laptop"], row["status"]) == ("200012345678", "0771234567", "SERIAL01", "Replied")


def test_the_register_exports_to_excel_in_the_sheet_order(mail):
    from openpyxl import load_workbook
    no = add("arjun_mehta.txt", "Mr.Dinesh")
    read_all()
    invitation.send(no)
    ws = load_workbook(io.BytesIO(invitation.export_xlsx(invitation.rows()))).active
    assert [c.value for c in ws[1]] == ["No", "Mail date", "Source", "University", "Technology", "Name", "NIC / PP",
                                        "Address / Company", "Vehicle No", "Tel No", "Laptop Serial No", "Status"]
    assert ws["F2"].value == "Arjun Mehta" and ws["C2"].value == "Mr.Dinesh" and ws["B2"].number_format == "d-mmm-yy"


# ---- the web routes -----------------------------------------------------------------------------
def test_mail_routes(mail):
    assert mail.get("/api/state").json()["mode"] == "mail"
    r = mail.post("/api/cvs", headers=HR, data={"source": "liyan@example.com"},
                  files=[("files", ("arjun_mehta.txt", (SAMPLES / "arjun_mehta.txt").read_bytes()))])
    no = r.json()["results"][0]["app_no"]
    read_all()
    r = mail.post("/api/mail/attachments", headers=HR, files=[("files", ("guide.pdf", PDF))])
    assert [a["name"] for a in r.json()["attachments"]] == REQUIRED + ["guide.pdf"]
    d = mail.get(f"/api/candidates/{no}").json()
    assert d["source"] == "liyan@example.com" and d["invite"]["attachments"] == REQUIRED + ["guide.pdf"]
    body = {"subject": "Subject", "body": "Dear Intern\nhttps://example.com", "visit_date": "2026-12-29",
            "attachments": ["guide.pdf"]}
    assert mail.post(f"/api/candidates/{no}/mail", headers={"X-Role": "interviewer"}, json=body).status_code == 400   # HR only
    assert mail.post(f"/api/candidates/{no}/mail", headers=HR, json=body).json()["invite"]["status"] == "draft"
    preview = mail.get(f"/api/candidates/{no}/mail/preview").text
    assert '<a href="https://example.com">' in preview and "guide.pdf" in preview
    assert mail.post(f"/api/candidates/{no}/mail/send", headers=HR, json=body).json()["invite"]["status"] == "sent"
    reg = mail.get("/api/register?show=sent").json()
    assert reg["counts"]["sent"] == 1 and reg["rows"][0]["status"] == "Mail sent"
    assert mail.post(f"/api/candidates/{no}/register", headers=HR, json={"tel": "0771234567"}).json()["register"]["tel"] == "0771234567"
    x = mail.get("/api/register.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert [a["name"] for a in mail.post("/api/mail/attachments/guide.pdf/remove", headers=HR).json()["attachments"]] == REQUIRED
    assert mail.post("/api/mail/attachments/Hospital_system.pdf/remove", headers=HR).status_code == 400


def test_full_mode_is_unchanged(mail, monkeypatch):
    monkeypatch.setattr(config, "MODE", "full")
    no = add("arjun_mehta.txt")
    read_all()
    d = mail.get(f"/api/candidates/{no}").json()
    assert mail.get("/api/state").json()["mode"] == "full" and d["invite"] is None and d["register"] is None


# ---- Outlook Graph multi-step sending, >3 MB upload session & SMTP fallback -------------------
def test_graph_send_with_all_four_required_attachments(mail):
    from hiring import outlook

    outlook.save_settings({"mode": "graph", "client_id": "11111111-2222-3333-4444-555555555555"})

    class _FakeApp:
        def get_accounts(self):
            return [{"username": "hr.sender@slt.com.lk", "name": "HR Sender"}]

        def acquire_token_silent(self, scopes, account=None):
            return {"access_token": "graph-token-123"}

    requests_seen: list[tuple[str, str, dict]] = []

    def fake_graph(method, url, payload, token, cfg):
        requests_seen.append((method, url, payload))
        if url.endswith("/me/messages"):
            return {"id": "draft-all-4", "webLink": "https://outlook.office.com/mail/id/draft-all-4"}
        return {}

    outlook.set_msal_factory(lambda cid, auth, cache: _FakeApp())
    outlook.set_graph_backend(fake_graph)

    no = add("arjun_mehta.txt", "Mr.Dinesh")
    read_all()
    sent = invitation.send(no)
    assert sent["how"] == "graph"
    assert sent["sender"] == "hr.sender@slt.com.lk"
    assert sent["file"].endswith(".eml") and (db.paths.outbox / sent["file"]).is_file()

    # Exact sequence: 1 create draft + 4 separate attachments + 1 send
    assert requests_seen[0][0] == "POST" and requests_seen[0][1].endswith("/me/messages")
    att_calls = [c for c in requests_seen if "/me/messages/draft-all-4/attachments" in c[1]]
    assert len(att_calls) == 4
    assert sorted(c[2]["name"] for c in att_calls) == sorted(REQUIRED)
    assert requests_seen[-1][1].endswith("/me/messages/draft-all-4/send")


def test_graph_large_attachment_uses_upload_session_in_320kib_chunks(mail):
    from hiring import outlook

    outlook.save_settings({"mode": "graph", "client_id": "11111111-2222-3333-4444-555555555555"})

    class _FakeApp:
        def get_accounts(self):
            return [{"username": "hr.sender@slt.com.lk", "name": "HR Sender"}]

        def acquire_token_silent(self, scopes, account=None):
            return {"access_token": "graph-token-big"}

    large_size = 3_600_000
    large_bytes = b"%PDF-1.4\n" + (b"X" * (large_size - 9))
    assert invitation.add_attachment("large_manual.pdf", large_bytes)["ok"] is True

    requests_seen: list[tuple[str, str, dict]] = []
    uploaded_chunks = bytearray()

    def fake_graph(method, url, payload, token, cfg):
        requests_seen.append((method, url, payload))
        if url.endswith("/me/messages"):
            return {"id": "draft-big-1", "webLink": "https://outlook.office.com/mail/id/draft-big-1"}
        if url.endswith("/attachments/createUploadSession"):
            assert payload["AttachmentItem"] == {"attachmentType": "file", "name": "large_manual.pdf", "size": large_size}
            return {"uploadUrl": "https://outlook.office.com/api/upload/session-abc"}
        if method == "PUT" and url == "https://outlook.office.com/api/upload/session-abc":
            uploaded_chunks.extend(payload["chunk"])
            return {}
        return {}

    outlook.set_msal_factory(lambda cid, auth, cache: _FakeApp())
    outlook.set_graph_backend(fake_graph)

    no = add("arjun_mehta.txt")
    read_all()
    invitation.save_draft(no, "Large attachment test", "Hello\n\nPlease see manual.", "2026-12-29", ["large_manual.pdf"])
    sent = invitation.send(no)
    assert sent["how"] == "graph"
    assert bytes(uploaded_chunks) == large_bytes

    put_calls = [c for c in requests_seen if c[0] == "PUT"]
    assert len(put_calls) == 2
    assert put_calls[0][2]["content_range"] == f"bytes 0-{outlook.UPLOAD_CHUNK_BYTES - 1}/{large_size}"
    assert put_calls[0][2]["content_length"] == outlook.UPLOAD_CHUNK_BYTES
    assert put_calls[0][2]["content_length"] % 327_680 == 0
    assert put_calls[1][2]["content_range"] == f"bytes {outlook.UPLOAD_CHUNK_BYTES}-{large_size - 1}/{large_size}"
    assert requests_seen[-1][1].endswith("/me/messages/draft-big-1/send")


def test_graph_failure_triggers_office365_smtp_fallback_and_cleans_draft(mail):
    from hiring import outlook

    outlook.save_settings({
        "mode": "graph",
        "client_id": "11111111-2222-3333-4444-555555555555",
        "smtp_user": "hr@slt.com.lk",
        "smtp_password": "app-password",
        "smtp_fallback": True,
    })

    class _FakeApp:
        def get_accounts(self):
            return [{"username": "hr.sender@slt.com.lk", "name": "HR Sender"}]

        def acquire_token_silent(self, scopes, account=None):
            return {"access_token": "graph-token-fail"}

    graph_calls: list[tuple[str, str]] = []
    smtp_msgs = []

    def failing_graph(method, url, payload, token, cfg):
        graph_calls.append((method, url))
        if method == "POST" and url.endswith("/me/messages"):
            return {"id": "draft-orphan-1", "webLink": ""}
        if method == "POST" and "/attachments" in url:
            raise outlook.OutlookError("Graph throttled (503)")
        return {}

    def fake_smtp(msg, cfg):
        smtp_msgs.append((msg, cfg))
        return "<smtp-fallback-1@slt.com.lk>"

    outlook.set_msal_factory(lambda cid, auth, cache: _FakeApp())
    outlook.set_graph_backend(failing_graph)
    outlook.set_smtp_backend(fake_smtp)

    no = add("arjun_mehta.txt")
    read_all()
    sent = invitation.send(no)
    assert sent["how"] == "smtp"
    assert sent["sender"] == "hr@slt.com.lk"
    assert ("DELETE", "https://graph.microsoft.com/v1.0/me/messages/draft-orphan-1") in graph_calls
    assert len(smtp_msgs) == 1
    msg, _ = smtp_msgs[0]
    assert "X-Unsent" not in msg
    assert "hr@slt.com.lk" in msg["From"]
    assert sorted(p.get_filename() for p in msg.iter_attachments()) == sorted(REQUIRED)


def test_send_failure_without_fallback_leaves_candidate_unsent(mail):
    from hiring import outlook

    outlook.save_settings({
        "mode": "graph",
        "client_id": "11111111-2222-3333-4444-555555555555",
        "smtp_fallback": False,
    })
    no = add("arjun_mehta.txt")
    read_all()
    with pytest.raises(invitation.MailProblem, match="sign in"):
        invitation.send(no)
    c = db.get_candidate(no)
    assert (c.invite or {}).get("status") != "sent"
    assert invitation.row(c)["status"] == "Not mailed"


def test_mail_test_endpoint_eml_download_and_university_faculty_edge_case(mail):
    assert tools.find_university(["BSc (Hons), Faculty of Information Technology, University of Moratuwa (2022 - 2026)"]) == \
        "University of Moratuwa"

    r = mail.post("/api/mail/test", headers=HR, json={"to": "supervisor@outlook.com"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True and res["how"] == "eml"
    assert sorted(res["attachments"]) == sorted(REQUIRED)
    assert res["total_bytes"] > 3_000_000

    no = add("arjun_mehta.txt")
    read_all()
    assert mail.post(f"/api/candidates/{no}/mail/send", headers=HR, json={}).status_code == 200
    eml = mail.get(f"/api/candidates/{no}/mail/eml")
    assert eml.status_code == 200 and eml.headers["content-type"].startswith("message/rfc822")
    assert eml.content[:40]


def test_outlook_app_mode_in_mail_only_and_open_route(mail, monkeypatch):
    from hiring import outlook

    monkeypatch.setattr(config, "OUTLOOK_MODE", "outlook_app")
    launched: list[tuple[Path, dict]] = []

    def fake_launcher(eml_path: Path, **kwargs) -> str:
        launched.append((eml_path, kwargs))
        return "Apple Mail"

    outlook.set_desktop_launcher(fake_launcher)
    assert outlook.raw_settings()["mode"] == "outlook_app"

    # Test mail with all 4 attachments opens via desktop launcher
    r = mail.post("/api/mail/test", headers=HR, json={"to": "supervisor@outlook.com"})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["how"] == "outlook_app" and res["opened_in"] == "Apple Mail"
    assert sorted(res["attachments"]) == sorted(REQUIRED)
    assert len(launched) == 1 and launched[0][0].is_file()
    parsed = email.message_from_bytes(launched[0][0].read_bytes(), policy=email.policy.default)
    assert parsed["X-Unsent"] == "1"
    assert parsed["X-Uniform-Type-Identifier"] == "com.apple.mail-draft"

    # Send to candidate & re-open via /api/candidates/{app_no}/mail/open
    no = add("arjun_mehta.txt", "Mr.Dinesh")
    read_all()
    sent = mail.post(f"/api/candidates/{no}/mail/send", headers=HR, json={})
    assert sent.status_code == 200, sent.text
    inv = sent.json()["invite"]
    assert inv["how"] == "outlook_app" and inv["opened_in"] == "Apple Mail" and inv["status"] == "sent"
    assert len(launched) == 2 and sorted(p.name for p in launched[1][1]["attachments"]) == sorted(REQUIRED)

    reopen = mail.post(f"/api/candidates/{no}/mail/open", headers=HR)
    assert reopen.status_code == 200, reopen.text
    assert reopen.json()["opened_in"] == "Apple Mail" and len(launched) == 3

    # Failing launcher surfaces clean MailProblem (HTTP 400) without marking candidate sent
    def broken_launcher(*args, **kwargs):
        raise outlook.OutlookError("No desktop mail app installed")

    outlook.set_desktop_launcher(broken_launcher)
    no2 = add("rahul_nair.txt")
    read_all()
    fail = mail.post(f"/api/candidates/{no2}/mail/send", headers=HR, json={})
    assert fail.status_code == 400 and "No desktop mail app" in fail.json()["detail"]
    assert (db.get_candidate(no2).invite or {}).get("status") != "sent"

