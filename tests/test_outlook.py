"""Tests for Microsoft Outlook integration (MSAL + Graph API / .eml / SMTP) and performance paths."""

import base64
import email
import email.policy
import json
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_model import FakeModel  # noqa: E402
from hiring import actions, checks, config, db, flows, llm, outlook, server, tools  # noqa: E402


class _FakeMsalApp:
    """In-memory stand-in for msal.PublicClientApplication."""

    def __init__(self, *, signed_in: bool = True, token: str = "fake-graph-token", email_addr: str = "supervisor@contoso.com"):
        self.signed_in = signed_in
        self.token = token
        self.email_addr = email_addr
        self.flow_calls = 0
        self.proceed_event = threading.Event()
        if signed_in:
            self.proceed_event.set()

    def get_accounts(self):
        if not self.signed_in:
            return []
        return [{"username": self.email_addr, "name": "Supervisor Account"}]

    def remove_account(self, account):
        self.signed_in = False

    def acquire_token_silent(self, scopes, account=None):
        if not self.signed_in:
            return None
        return {"access_token": self.token}

    def initiate_device_flow(self, scopes):
        self.flow_calls += 1
        return {
            "user_code": "ABCD-1234",
            "verification_uri": "https://microsoft.com/devicelogin",
            "message": "Go to https://microsoft.com/devicelogin and enter ABCD-1234",
            "expires_in": 900,
            "interval": 0,
        }

    def acquire_token_by_device_flow(self, flow):
        self.proceed_event.wait(timeout=2.0)
        self.signed_in = True
        return {
            "access_token": self.token,
            "id_token_claims": {
                "preferred_username": self.email_addr,
                "name": "Supervisor Account",
            },
        }


@pytest.fixture()
def app_client(tmp_path):
    """Initialize isolated SQLite data dir + FakeModel + TestClient."""
    db.setup(tmp_path / "data")
    model = FakeModel()
    llm.set_backend(model)
    flows.NOTICE.update(text="", at="")
    client = TestClient(server.app)
    yield client
    llm.set_backend(None)


def _setup_msal_and_graph(*, signed_in: bool = True):
    fake_app = _FakeMsalApp(signed_in=signed_in)
    calls: list[dict] = []

    def msal_factory(client_id, authority, cache):
        return fake_app

    def graph_backend(method, url, payload, token, cfg):
        calls.append({"method": method, "url": url, "payload": payload, "token": token})
        if url.endswith("/me/messages"):
            return {"id": "graph-draft-42", "webLink": "https://outlook.office.com/mail/id/graph-draft-42"}
        return {"id": "graph-sent-99"}

    outlook.set_msal_factory(msal_factory)
    outlook.set_graph_backend(graph_backend)
    return fake_app, calls


# ------------------------------------------------------------------ unit: links & sanitization
def test_compose_links_and_crlf_sanitization():
    links = outlook.compose_links(
        "cand@example.com\r\nBcc: evil@attacker.com",
        "Interview Invite\r\nX-Injected: bad",
        "Hello Arjun,\nYour interview is confirmed.",
    )
    assert links["outlook_office"].startswith("https://outlook.office.com/mail/deeplink/compose?")
    assert links["outlook_live"].startswith("https://outlook.live.com/mail/0/deeplink/compose?")
    assert links["mailto"].startswith("mailto:")
    header_part = links["outlook_office"].split("&body=")[0]
    assert "%0D" not in header_part and "%0A" not in header_part

    # Demo redirect overrides recipient
    demo_links = outlook.compose_links(
        "cand@example.com",
        "Offer",
        "Welcome",
        test_recipient="supervisor@outlook.com",
    )
    assert "supervisor%40outlook.com" in demo_links["outlook_office"]
    assert "cand%40example.com" not in demo_links["outlook_office"]

    # Very long body is safely truncated for browser deeplinks
    long_links = outlook.compose_links("cand@example.com", "Offer", "A" * 5000)
    assert "Truncated" in long_links["outlook_office"]
    assert outlook.compose_links("", "Subj", "Body") == {}


# ------------------------------------------------------------------ unit: Graph Draft & Send
def test_graph_draft_and_send_with_pdf_attachment(tmp_path):
    db.setup(tmp_path / "data")
    outlook.save_settings({"mode": "graph_draft", "client_id": "11111111-2222-3333-4444-555555555555"})
    _, calls = _setup_msal_and_graph(signed_in=True)

    pdf_bytes = b"%PDF-1.4\n%FakePlacementLetter\n%%EOF"
    draft_res = outlook.create_outlook_draft(
        "arjun@example.com",
        "Arjun Mehta",
        "Placement Offer — AI/ML",
        "Dear Arjun,\n\nCongratulations on passing!\n",
        attachment=("Placement_Letter_APP-0001.pdf", pdf_bytes),
    )
    assert draft_res["ok"] is True
    assert draft_res["delivery_status"] == "outlook_draft"
    assert draft_res["message_id"] == "graph-draft-42"
    assert draft_res["web_link"] == "https://outlook.office.com/mail/id/graph-draft-42"
    assert len(calls) == 1
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/me/messages")
    assert calls[0]["token"] == "fake-graph-token"
    att = calls[0]["payload"]["attachments"][0]
    assert att["name"] == "Placement_Letter_APP-0001.pdf"
    assert base64.b64decode(att["contentBytes"]) == pdf_bytes

    # Switch to graph_send with demo redirect
    outlook.save_settings({
        "mode": "graph_send",
        "client_id": "11111111-2222-3333-4444-555555555555",
        "test_recipient": "supervisor@university.edu",
    })
    send_res = outlook.send_outlook_mail(
        "arjun@example.com",
        "Arjun Mehta",
        "Placement Offer — AI/ML",
        "Dear Arjun,\n\nPlease see your letter attached.\n",
        attachment=("Placement_Letter_APP-0001.pdf", pdf_bytes),
    )
    assert send_res["ok"] is True
    assert send_res["delivery_status"] == "sent"
    assert send_res["sent_to"] == "supervisor@university.edu"
    assert len(calls) == 2
    assert calls[1]["url"].endswith("/me/sendMail")
    assert calls[1]["payload"]["saveToSentItems"] is True
    assert calls[1]["payload"]["message"]["toRecipients"][0]["emailAddress"]["address"] == "supervisor@university.edu"


def test_graph_requires_sign_in(tmp_path):
    db.setup(tmp_path / "data")
    outlook.save_settings({"mode": "graph_draft", "client_id": "11111111-2222-3333-4444-555555555555"})
    _setup_msal_and_graph(signed_in=False)

    res = outlook.create_outlook_draft("a@example.com", "Alex", "Hi", "Body")
    assert res["ok"] is False
    assert res["delivery_status"] == "failed"
    assert "sign in" in res["error"].lower()


def test_smtp_fallback_with_attachment(tmp_path):
    db.setup(tmp_path / "data")
    outlook.save_settings({
        "mode": "smtp",
        "smtp_host": "smtp.office365.com",
        "smtp_port": 587,
        "smtp_user": "hr@contoso.com",
        "smtp_password": "app-password-secret",
    })
    captured = []

    def fake_smtp(msg, cfg):
        captured.append((msg, cfg))
        return "<custom-smtp-id@contoso.com>"

    outlook.set_smtp_backend(fake_smtp)
    res = outlook.send_smtp_email(
        "candidate@example.com",
        "Candidate",
        "Interview Details",
        "Hello!\nPlease join at 10:00.",
        attachment=("Placement_Letter_APP-0009.pdf", b"%PDF-1.4\n%%EOF"),
    )
    assert res["ok"] is True
    assert res["delivery_status"] == "sent"
    assert res["message_id"] == "<custom-smtp-id@contoso.com>"
    assert len(captured) == 1
    msg, cfg = captured[0]
    assert msg["To"] == "Candidate <candidate@example.com>"
    assert cfg["smtp_host"] == "smtp.office365.com"
    attachments = list(msg.iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "Placement_Letter_APP-0009.pdf"


# ------------------------------------------------------------------ integration: API & UI workflow
def test_api_outlook_settings_auth_and_draft_delivery(app_client: TestClient):
    headers_hr = {"x-role": "hr"}
    headers_int = {"x-role": "interviewer"}

    # Initial settings in state omit secrets
    state = app_client.get("/api/state").json()
    assert state["outlook"]["mode"] == "eml"
    assert "smtp_password" not in state["outlook"]

    # Interviewer cannot change Outlook settings or test mail
    r = app_client.post("/api/outlook/settings", json={"mode": "eml"}, headers=headers_int)
    assert r.status_code in (400, 403)

    # Bad JSON returns 400
    r = app_client.post("/api/outlook/settings", content="{not-json", headers={**headers_hr, "content-type": "application/json"})
    assert r.status_code == 400

    # Configure Graph draft mode + start MSAL device flow
    fake_app, graph_calls = _setup_msal_and_graph(signed_in=False)
    r = app_client.post(
        "/api/outlook/settings",
        json={
            "mode": "graph_draft",
            "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "tenant_id": "common",
            "from_name": "Supervisor Hiring Desk",
            "test_recipient": "supervisor@outlook.com",
        },
        headers=headers_hr,
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "graph_draft"

    # Start Device Code Flow, verify pending user_code, then complete sign-in
    r = app_client.post("/api/outlook/auth/start", headers=headers_hr)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert r.json()["user_code"] == "ABCD-1234"
    fake_app.proceed_event.set()
    for _ in range(50):
        st = app_client.get("/api/outlook/auth/status").json()
        if st.get("signed_in"):
            break
    assert st.get("signed_in") is True
    assert st.get("status") == "signed_in"

    # Send test draft through /api/outlook/test
    r = app_client.post("/api/outlook/test", json={"to": "supervisor@outlook.com"}, headers=headers_hr)
    assert r.status_code == 200, r.text
    assert r.json()["delivery_status"] == "outlook_draft"
    assert len(graph_calls) == 1

    # Add Arjun Mehta, run employees, invite, release invitation, pass interview -> Acceptance + Placement Letter
    app_no = actions.add_cv("arjun_mehta.txt", (config.SAMPLES_DIR / "arjun_mehta.txt").read_bytes(), "")["app_no"]
    flows.run_all_once()
    actions.invite_from_pool(app_no, "AI/ML")
    flows.run_all_once()

    inv_drafts = [
        d for d in app_client.get("/api/drafts?state=waiting").json()
        if d["app_no"] == app_no and d["kind"] == "invitation"
    ]
    assert len(inv_drafts) == 1
    inv_rel = app_client.post(f"/api/drafts/{inv_drafts[0]['id']}/release", headers=headers_hr)
    assert inv_rel.status_code == 200, inv_rel.text
    assert inv_rel.json()["status"] == "awaiting_result"
    assert inv_rel.json()["delivery_status"] == "outlook_draft"

    actions.enter_result(app_no, "Pass", "Strong ML skills")
    flows.run_all_once()

    accept = [
        d for d in app_client.get("/api/drafts?state=waiting").json()
        if d["app_no"] == app_no and d["kind"] == "acceptance"
    ]
    assert len(accept) == 1
    draft_id = accept[0]["id"]

    # Create Outlook Draft on demand without releasing yet
    r = app_client.post(f"/api/drafts/{draft_id}/outlook-draft", headers=headers_hr)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["ok"] is True
    assert res["delivery_status"] == "outlook_draft"
    assert res["web_link"].startswith("https://outlook.office.com/")

    # Switch to graph_send and release the draft
    app_client.post("/api/outlook/settings", json={"mode": "graph_send"}, headers=headers_hr)
    r = app_client.post(f"/api/drafts/{draft_id}/release", headers=headers_hr)
    assert r.status_code == 200, r.text
    rel = r.json()
    assert rel["status"] == "closed"
    assert rel["delivery_status"] == "sent"
    assert rel["sent_to"] == "supervisor@outlook.com"

    # Download generated .eml and confirm X-Unsent + Placement_Letter PDF attachment
    r = app_client.get(f"/api/drafts/{draft_id}/eml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("message/rfc822")
    parsed = email.message_from_bytes(r.content, policy=email.policy.default)
    assert parsed["X-Unsent"] == "1"
    pdf_parts = [p for p in parsed.iter_attachments() if (p.get_filename() or "").endswith(".pdf")]
    assert len(pdf_parts) == 1 and pdf_parts[0].get_filename().startswith("Placement_Letter_")

    # Resend works cleanly on released draft
    r = app_client.post(f"/api/drafts/{draft_id}/resend", headers=headers_hr)
    assert r.status_code == 200, r.text
    assert r.json()["delivery_status"] == "sent"

    # Sign out clears cached account
    r = app_client.post("/api/outlook/auth/signout", headers=headers_hr)
    assert r.status_code == 200
    assert app_client.get("/api/outlook/auth/status").json()["signed_in"] is False


# ------------------------------------------------------------------ performance checks
def test_sqlite_wal_and_batched_queries(tmp_path):
    db.setup(tmp_path / "data")
    with db.Session() as s:
        mode = s.execute(text("PRAGMA journal_mode")).scalar()
        assert str(mode).lower() == "wal"

    a1 = actions.add_cv("arjun_mehta.txt", (config.SAMPLES_DIR / "arjun_mehta.txt").read_bytes(), "")["app_no"]
    a2 = actions.add_cv("priya_sharma.txt", (config.SAMPLES_DIR / "priya_sharma.txt").read_bytes(), "")["app_no"]
    by_app = db.get_candidates_by_app_nos([a1, a2, "APP-9999", a1])
    assert set(by_app.keys()) == {a1, a2}


def test_score_all_areas_matches_individual_score_cv(tmp_path):
    db.setup(tmp_path / "data")
    cv_text = (config.SAMPLES_DIR / "arjun_mehta.txt").read_text(encoding="utf-8")
    areas = db.active_areas()
    profile = tools.rule_profile(cv_text, tools.skill_vocabulary(areas))
    shared = tools.shared_terms(areas)
    batched = tools.score_all_areas(profile, cv_text, areas)
    assert set(batched.keys()) == {a.name for a in areas}

    for area in areas:
        single_score, single_parts, single_gaps = tools.score_cv(profile, cv_text, area, shared)
        assert batched[area.name]["score"] == single_score
        assert batched[area.name]["parts"] == single_parts
        assert batched[area.name]["gaps"] == single_gaps


def test_outlook_app_mode_release_open_and_test(app_client: TestClient):
    headers_hr = {"x-role": "hr"}
    opened: list[tuple[Path, dict]] = []

    def fake_launcher(eml_path: Path, **kwargs) -> str:
        opened.append((eml_path, kwargs))
        return "Microsoft Outlook"

    outlook.set_desktop_launcher(fake_launcher)

    r = app_client.post("/api/outlook/settings", json={"mode": "outlook_app"}, headers=headers_hr)
    assert r.status_code == 200 and r.json()["mode"] == "outlook_app"
    assert app_client.get("/api/state").json()["outlook"]["mode"] == "outlook_app"

    # Test Outlook button in settings modal triggers outlook_test.eml + launcher
    tr = app_client.post("/api/outlook/test", json={"to": "supervisor@outlook.com"}, headers=headers_hr)
    assert tr.status_code == 200, tr.text
    assert tr.json()["delivery_status"] == "opened_app"
    assert tr.json()["opened_in"] == "Microsoft Outlook"
    assert len(opened) == 1 and opened[0][0].name == "outlook_test.eml"
    parsed_test = email.message_from_bytes(opened[0][0].read_bytes(), policy=email.policy.default)
    assert parsed_test["X-Unsent"] == "1"
    assert parsed_test["X-Uniform-Type-Identifier"] == "com.apple.mail-draft"

    # Full candidate flow -> Release Acceptance letter in outlook_app mode + one-click re-open
    app_no = actions.add_cv("arjun_mehta.txt", (config.SAMPLES_DIR / "arjun_mehta.txt").read_bytes(), "")["app_no"]
    flows.run_all_once()
    actions.invite_from_pool(app_no, "AI/ML")
    flows.run_all_once()
    inv_id = [d["id"] for d in app_client.get("/api/drafts?state=waiting").json() if d["app_no"] == app_no][0]
    rel = app_client.post(f"/api/drafts/{inv_id}/release", headers=headers_hr)
    assert rel.status_code == 200
    assert rel.json()["delivery_status"] == "opened_app"

    reopen = app_client.post(f"/api/drafts/{inv_id}/open", headers=headers_hr)
    assert reopen.status_code == 200
    assert reopen.json()["ok"] is True and reopen.json()["opened_in"] == "Microsoft Outlook"
    assert len(opened) == 3


def test_auto_mode_resolution_and_interactive_login(app_client: TestClient):
    headers_hr = {"x-role": "hr"}
    app_client.post("/api/outlook/settings", json={"mode": "auto"}, headers=headers_hr)
    assert app_client.get("/api/outlook/settings").json()["effective_mode"] == "outlook_app"

    # Adding SMTP credentials while in 'auto' resolves effective_mode to 'smtp'
    app_client.post(
        "/api/outlook/settings",
        json={"mode": "auto", "smtp_user": "hr@slt.com.lk", "smtp_password": "app-pw"},
        headers=headers_hr,
    )
    assert app_client.get("/api/outlook/settings").json()["effective_mode"] == "smtp"

    # 1-click interactive login upgrades 'auto' to 'graph'
    fake_app = _FakeMsalApp(signed_in=False)

    def interactive(scopes, prompt=None, timeout=None, **kwargs):
        fake_app.signed_in = True
        return {
            "access_token": "interactive-token",
            "id_token_claims": {"preferred_username": fake_app.email_addr, "name": "Supervisor Account"},
        }

    fake_app.acquire_token_interactive = interactive
    outlook.set_msal_factory(lambda cid, auth, cache: fake_app)
    app_client.post(
        "/api/outlook/settings",
        json={"mode": "auto", "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},
        headers=headers_hr,
    )
    start = app_client.post("/api/outlook/auth/interactive", headers=headers_hr)
    assert start.status_code == 200, start.text
    for _ in range(50):
        st = app_client.get("/api/outlook/auth/status").json()
        if st.get("signed_in"):
            break
    assert st.get("signed_in") is True
    assert app_client.get("/api/outlook/settings").json()["effective_mode"] == "graph"
