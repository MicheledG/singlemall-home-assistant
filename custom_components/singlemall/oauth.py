"""SingleMall's sign-in: Home Assistant's own OAuth2 flow with PKCE.

The client is public (no secret), so PKCE is what protects the code
exchange; its id is public configuration hard-coded in ``const.py``. No
application credentials are asked of the person.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_entry_oauth2_flow
import jwt

from . import const
from .const import DOMAIN, SCOPES


class SingleMallOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    """The hosted sign-in, with the scopes and the one registered callback."""

    @property
    def name(self) -> str:
        """Shown when Home Assistant has to name the sign-in method."""
        return "SingleMall"

    @property
    def redirect_uri(self) -> str:
        """Always the my.home-assistant.io callback.

        It is the only callback the app client allows, and it reaches the
        person's own Home Assistant whether or not the ``my`` integration is
        loaded there.
        """
        return config_entry_oauth2_flow.MY_AUTH_CALLBACK_PATH

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Ask for the scopes the app client allows, plus the PKCE challenge."""
        return {"scope": " ".join(SCOPES), **super().extra_authorize_data}


@callback
def async_register_implementation(hass: HomeAssistant) -> SingleMallOAuth2Implementation:
    """Register a fresh implementation, with a fresh PKCE verifier.

    A flow keeps a reference to the implementation it started with, so
    registering a new one for each sign-in never disturbs a flow in progress
    or a set-up entry's refreshes (a refresh does not use the verifier).
    """
    implementation = SingleMallOAuth2Implementation(
        hass,
        DOMAIN,
        const.client_id(),
        const.authorize_url(),
        const.token_url(),
    )
    config_entry_oauth2_flow.async_register_implementation(hass, DOMAIN, implementation)
    return implementation


def token_claims(token: dict[str, Any]) -> dict[str, Any]:
    """The claims of the tokens just received from the token endpoint.

    The tokens came straight from Cognito over TLS in answer to this flow's
    own code, so their signature is not re-verified here; the API verifies
    the access token on every request.
    """
    for key in ("id_token", "access_token"):
        raw = token.get(key)
        if not isinstance(raw, str):
            continue
        try:
            claims = jwt.decode(raw, options={"verify_signature": False})
        except jwt.PyJWTError:
            continue
        if isinstance(claims, dict) and isinstance(claims.get("sub"), str):
            return claims
    return {}
