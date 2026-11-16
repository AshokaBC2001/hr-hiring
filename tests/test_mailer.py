"""Tests for live SMTP email sender, settings persistence, HTML rendering, and demo override."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hiring import config, db, mailer  # noqa: E402


@pytest.fixture()
def isolated_mailer(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SMTP_ENABLED", False)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USER", "hr@slt.lk")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-pass")
    monkeypatch.setattr(config, "SMTP_TEST_RECIPIENT", "")
    db.setup(tmp_path / "data")
    yield
    mailer.set_backend(None)


def test_settings_mask_password_and_persist_in_db(isolated_mailer):
    pub = mailer.public_settings()
    assert pub["enabled"] is False
    assert pub["host"] == "smtp.example.test"
    assert pub["has_password"] is True
    assert "password" not in pub

    updated = mailer.save_settings({
        "enabled": True,
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "team@slt.lk",
        "from_name": "SLT Hiring",
        "from_email": "team@slt.lk",
        "use_tls": True,
        "use_ssl": False,
        "test_recipient": "demo.inbox@gmail.com",
    })
    assert updated["enabled"] is True
    assert updated["host"] == "smtp.gmail.com"
    assert updated["test_recipient"] == "demo.inbox@gmail.com"
    assert updated["has_password"] is True


def test_send_email_disabled_returns_outbox_only(isolated_mailer):
    res = mailer.send_email("arjun@example.com", "Arjun Mehta", "Offer - AI/ML", "Congratulations!")
    assert res["status"] == "outbox_only"
    assert res["ok"] is True


def test_send_email_uses_multipart_html_and_demo_override(isolated_mailer):
    captured = []

    def fake_send(msg, cfg):
        captured.append((msg, cfg))
        return msg["Message-ID"]

    mailer.set_backend(fake_send)
    mailer.save_settings({
        "enabled": True,
        "from_name": "SLT Talent Acquisition",
        "from_email": "hr@slt.lk",
        "test_recipient": "supervisor@slt.lk",
    })

    res = mailer.send_email(
        "arjun.mehta@example.com",
        "Arjun Mehta",
        "Offer - AI/ML",
        "Dear Arjun,\n\nYour work ID is 1001.",
        meta={"app_no": "APP-0001", "kind": "acceptance", "work_id": 1001},
    )
    assert res["ok"] is True
    assert res["status"] == "sent"
    assert res["sent_to"] == "supervisor@slt.lk"
    assert res["message_id"]

    msg, _ = captured[0]
    assert "supervisor@slt.lk" in msg["To"]
    assert "[DEMO]" in msg["Subject"]
    assert msg.is_multipart()
    html_part = msg.get_body(preferencelist=("html",)).get_content()
    assert "APP-0001" in html_part
    assert "1001" in html_part
    assert "arjun.mehta@example.com" in html_part


def test_send_email_captures_smtp_errors_without_crashing(isolated_mailer):
    def broken_send(msg, cfg):
        raise OSError("535 Authentication failed")

    mailer.set_backend(broken_send)
    mailer.save_settings({"enabled": True, "from_email": "hr@slt.lk"})

    res = mailer.send_email("real@domain.org", "Real Candidate", "Hello", "Body text")
    assert res["ok"] is False
    assert res["status"] == "failed"
    assert "Authentication failed" in res["error"]
