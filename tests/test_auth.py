import json

import pytest
from google.oauth2.credentials import Credentials

from gform import auth
from gform.auth import AuthError, get_credentials, gform_home, token_path


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GFORM_HOME", str(tmp_path))
    monkeypatch.delenv("GFORM_CREDENTIALS", raising=False)
    monkeypatch.delenv("GFORM_TOKEN", raising=False)
    return tmp_path


def _write_token(tmp_path, scopes=None, expiry=None):
    data = {
        "type": "authorized_user",
        "client_id": "client",
        "client_secret": "secret",
        "refresh_token": "refresh",
        "token": "cached-token",
        "scopes": scopes if scopes is not None else auth.SCOPES,
    }
    if expiry:
        data["expiry"] = expiry
    (tmp_path / "token.json").write_text(json.dumps(data), encoding="utf-8")


def test_paths_follow_gform_home(tmp_path):
    assert gform_home() == tmp_path
    assert token_path() == tmp_path / "token.json"


def test_missing_token_raises_with_instructions(tmp_path):
    with pytest.raises(AuthError, match="gform-auth"):
        get_credentials(interactive=False)


def test_valid_cached_token_loads(tmp_path):
    # google-auth treats a missing expiry as already expired, so an unexpired
    # token needs an explicit future expiry.
    _write_token(tmp_path, expiry="2099-01-01T00:00:00Z")
    creds = get_credentials(interactive=False)
    assert creds.token == "cached-token"


def test_token_with_missing_scopes_treated_as_absent(tmp_path):
    _write_token(
        tmp_path,
        scopes=["https://www.googleapis.com/auth/forms.body"],
        expiry="2099-01-01T00:00:00Z",
    )
    with pytest.raises(AuthError, match="gform-auth"):
        get_credentials(interactive=False)


def test_expired_token_refreshes_and_resaves(tmp_path, monkeypatch):
    _write_token(tmp_path, expiry="2020-01-01T00:00:00Z")

    def fake_refresh(self, request):
        self.token = "fresh-token"
        self.expiry = None

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    creds = get_credentials(interactive=False)
    assert creds.token == "fresh-token"
    saved = json.loads((tmp_path / "token.json").read_text(encoding="utf-8"))
    assert saved["token"] == "fresh-token"


def test_interactive_without_client_file_raises(tmp_path):
    with pytest.raises(AuthError, match="credentials.json"):
        get_credentials(interactive=True)
