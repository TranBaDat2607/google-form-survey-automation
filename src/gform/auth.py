"""OAuth credential management for the official Google Forms API.

The interactive browser flow is deliberately kept OUT of the MCP server
process: a stdio MCP server must never block on a browser login or write to
stdout. Run ``gform-auth`` once in a terminal to create the cached token; the
server then only loads (and silently refreshes) it, raising AuthError with
instructions when that is not possible.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# forms.body covers create/get/batchUpdate/setPublishSettings; reading
# collected answers needs the separate responses scope.
SCOPES = [
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]

FORMS_DISCOVERY_URL = "https://forms.googleapis.com/$discovery/rest?version=v1"

_SETUP_HINT = (
    "Create OAuth credentials in Google Cloud Console (APIs & Services -> "
    "Credentials -> Create OAuth client ID -> Desktop app), enable the Google "
    "Forms API, and save the downloaded file to {path}."
)


class AuthError(RuntimeError):
    """Raised when no usable Google credentials are available."""


def gform_home() -> Path:
    return Path(os.environ.get("GFORM_HOME") or Path.home() / ".gform")


def credentials_path() -> Path:
    return Path(os.environ.get("GFORM_CREDENTIALS") or gform_home() / "credentials.json")


def token_path() -> Path:
    return Path(os.environ.get("GFORM_TOKEN") or gform_home() / "token.json")


def logs_dir() -> Path:
    return gform_home() / "logs"


def _load_cached_token() -> Credentials | None:
    path = token_path()
    if not path.exists():
        return None
    try:
        stored_scopes = set(json.loads(path.read_text(encoding="utf-8")).get("scopes") or [])
        if not set(SCOPES) <= stored_scopes:
            # Token from an older scope set: force a re-auth rather than
            # failing later with a confusing 403.
            return None
        return Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, KeyError):
        return None


def _save_token(creds: Credentials) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")


def get_credentials(interactive: bool = False) -> Credentials:
    """Return valid credentials, refreshing or (if interactive) logging in.

    Non-interactive callers (the MCP server) get an AuthError telling the
    user to run ``gform-auth`` instead of a hung browser flow.
    """
    creds = _load_cached_token()

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except RefreshError:
            creds = None

    if not interactive:
        raise AuthError(
            f"No valid Google token at {token_path()}. Run `gform-auth` in a "
            f"terminal to sign in (one-time browser login). "
            + _SETUP_HINT.format(path=credentials_path())
        )

    cred_file = credentials_path()
    if not cred_file.exists():
        raise AuthError(
            f"OAuth client file not found at {cred_file}. "
            + _SETUP_HINT.format(path=cred_file)
        )

    # Imported lazily: only the gform-auth process ever needs the flow.
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_file), SCOPES)
    # Wait up to 5 minutes for the browser redirect so the unverified-app
    # consent screens can be clicked through without the flow timing out.
    creds = flow.run_local_server(port=0, timeout_seconds=300)
    _save_token(creds)
    return creds


def build_forms_service():
    """Build the Forms v1 discovery client with cached (non-interactive) creds."""
    from googleapiclient.discovery import build

    return build(
        "forms",
        "v1",
        credentials=get_credentials(interactive=False),
        discoveryServiceUrl=FORMS_DISCOVERY_URL,
        static_discovery=False,
    )


def main() -> None:
    """Console entry point: one-time interactive Google login (``gform-auth``)."""
    parser = argparse.ArgumentParser(
        prog="gform-auth",
        description="Authorize gform with your Google account (caches a token "
        "for the gform-mcp server).",
    )
    parser.add_argument(
        "--credentials",
        help="Path to the OAuth client credentials.json (default: "
        "%GFORM_CREDENTIALS% or ~/.gform/credentials.json)",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Discard the cached token and sign in again.",
    )
    args = parser.parse_args()

    if args.credentials:
        os.environ["GFORM_CREDENTIALS"] = args.credentials
    if args.reauth and token_path().exists():
        token_path().unlink()

    try:
        creds = get_credentials(interactive=True)
    except AuthError as exc:
        raise SystemExit(f"error: {exc}")

    # This is a standalone terminal command, never the MCP stdio channel, so
    # printing here is fine.
    print(f"Authorized. Token cached at {token_path()}")
    print(f"Scopes: {', '.join(creds.scopes or SCOPES)}")


if __name__ == "__main__":
    main()
