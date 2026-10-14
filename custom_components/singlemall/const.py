"""Constants for the SingleMall integration.

The three deployment constants below are public configuration, not secrets:
a public Cognito app client id, the hosted sign-in origin and the API base
URL, all printed as outputs of the SingleMall stack. The app on the phone
carries the same kind of values.

They were obviously invalid placeholders until the first deploy; while
any of them is a placeholder, the config flow aborts and a config
entry refuses to set up, each with a log line naming the constant to fill.
"""

from __future__ import annotations

from datetime import timedelta
import sys

DOMAIN = "singlemall"

# Every placeholder carries this marker; `missing_constants` looks for it.
PLACEHOLDER_MARKER = "REPLACE-ME"

# ---------------------------------------------------------------------------
# Fill these three from the stack outputs after the first deploy.
#
# CLIENT_ID      <- output `HomeAssistantClientId`
# COGNITO_DOMAIN <- output `UserPoolSignInUrl` (the hosted sign-in origin,
#                   e.g. https://<prefix>.auth.<region>.amazoncognito.com,
#                   without a trailing slash)
# API_URL        <- output `HomeAssistantApiUrl` (without a trailing slash)
# ---------------------------------------------------------------------------
CLIENT_ID = "32vaagri89o9kg01cagftiqvah"
COGNITO_DOMAIN = "https://singlemall-347434961513-eu-south-1.auth.eu-south-1.amazoncognito.com"
API_URL = "https://z1iulx53x0.execute-api.eu-south-1.amazonaws.com"

SCOPES = ("openid", "email", "profile")

# How often the list is read; a 304 makes an unchanged poll cheap.
UPDATE_INTERVAL = timedelta(seconds=30)

# A single request's ceiling; the function itself times out at 10 seconds.
REQUEST_TIMEOUT_SECONDS = 15

# The prefix the item description gives a quantity above 1, e.g. "×2".
QUANTITY_SIGN = "×"


def _module_value(name: str) -> str:
    """Read a constant at call time, so tests (and nothing else) can patch it."""
    return str(getattr(sys.modules[__name__], name))


def client_id() -> str:
    """The public Cognito app client id."""
    return _module_value("CLIENT_ID")


def authorize_url() -> str:
    """The hosted sign-in page's authorize endpoint."""
    return f"{_module_value('COGNITO_DOMAIN').rstrip('/')}/oauth2/authorize"


def token_url() -> str:
    """The hosted sign-in's token endpoint."""
    return f"{_module_value('COGNITO_DOMAIN').rstrip('/')}/oauth2/token"


def revoke_url() -> str:
    """The hosted sign-in's revocation endpoint."""
    return f"{_module_value('COGNITO_DOMAIN').rstrip('/')}/oauth2/revoke"


def api_url() -> str:
    """The base URL of the Home Assistant API."""
    return _module_value("API_URL").rstrip("/")


def missing_constants() -> list[str]:
    """The names of the deployment constants still left as placeholders."""
    return [
        name
        for name in ("CLIENT_ID", "COGNITO_DOMAIN", "API_URL")
        if PLACEHOLDER_MARKER in _module_value(name) or not _module_value(name)
    ]
