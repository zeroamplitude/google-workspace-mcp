"""A token granted before a scope was added keeps refreshing (0.13.0 fix).

Loading it with the current SCOPES made the refresh request scopes the user
never granted, and Google rejected the whole refresh with invalid_scope —
every tool for that account failed, not just the ones needing the new scope.
"""

from __future__ import annotations

import json

import pytest
from google.oauth2.credentials import Credentials

from google_workspace_mcp import auth

OLD_SCOPES = auth.SCOPES[:4]  # what a pre-0.12.0 consent granted


@pytest.fixture
def old_token(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "TOKENS_DIR", tmp_path)
    (tmp_path / "work.json").write_text(json.dumps({
        "token": "stale",
        "refresh_token": "r",
        "client_id": "c",
        "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": OLD_SCOPES,
        "expiry": "2000-01-01T00:00:00Z",
    }))
    asked: list[list[str]] = []

    def fake_refresh(self, request):
        asked.append(list(self.scopes or []))
        self.token = "fresh"
        self.expiry = None

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    return asked


def test_refresh_asks_only_for_the_granted_scopes(old_token):
    creds = auth._load_credentials("work")
    assert old_token == [OLD_SCOPES]
    assert creds.token == "fresh"


def test_status_reports_missing_scopes_instead_of_revoked(old_token):
    out = auth.token_status("work")
    assert out["authorized"] is True
    assert out["status"] == "missing_scopes"
    assert out["missing_scopes"] == auth.SCOPES[4:]
