"""Shared pytest fixtures to keep Outlook integration isolated in tests."""

import pytest

from hiring import config, outlook


@pytest.fixture(autouse=True)
def _isolate_outlook(monkeypatch: pytest.MonkeyPatch):
    """Ensure tests default to offline `.eml` mode with no live credentials or backends."""
    monkeypatch.setenv("HR_OUTLOOK_MODE", "eml")
    monkeypatch.setenv("HR_OUTLOOK_CLIENT_ID", "")
    monkeypatch.setenv("HR_OUTLOOK_TEST_RECIPIENT", "")
    monkeypatch.setenv("HR_OUTLOOK_SMTP_USER", "")
    monkeypatch.setenv("HR_OUTLOOK_SMTP_PASSWORD", "")

    monkeypatch.setattr(config, "OUTLOOK_MODE", "eml")
    monkeypatch.setattr(config, "OUTLOOK_CLIENT_ID", "")
    monkeypatch.setattr(config, "OUTLOOK_TEST_RECIPIENT", "")
    monkeypatch.setattr(config, "OUTLOOK_SMTP_USER", "")
    monkeypatch.setattr(config, "OUTLOOK_SMTP_PASSWORD", "")

    outlook.set_graph_backend(None)
    outlook.set_smtp_backend(None)
    outlook.set_msal_factory(None)
    outlook.set_desktop_launcher(None)
    outlook.reset_auth_state()
    yield
    outlook.set_graph_backend(None)
    outlook.set_smtp_backend(None)
    outlook.set_msal_factory(None)
    outlook.set_desktop_launcher(None)
    outlook.reset_auth_state()
