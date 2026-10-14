"""Config flow for SingleMall: add the integration, sign in, done."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import const
from .api import (
    AuthError,
    ConnectionFailed,
    ForbiddenError,
    SingleMallApi,
    SingleMallError,
    StaticTokenAuth,
)
from .const import DOMAIN
from .oauth import async_register_implementation, token_claims

_LOGGER = logging.getLogger(__name__)


class SingleMallFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Sign in with the SingleMall account used on the phone."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        """The flow's logger."""
        return _LOGGER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Start the sign-in, unless this build was never given its constants."""
        if missing := const.missing_constants():
            _LOGGER.error(
                "SingleMall is not configured: fill %s in "
                "custom_components/singlemall/const.py from the stack outputs",
                ", ".join(missing),
            )
            return self.async_abort(reason="not_configured")
        async_register_implementation(self.hass)
        return await super().async_step_user(user_input)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The sign-in stopped working: ask the person to sign in again."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm before leaving for the sign-in page."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Check the person may use SingleMall, then create or update the entry."""
        claims = token_claims(data["token"])
        subject = claims.get("sub")
        if not subject:
            _LOGGER.error("The sign-in returned no subject")
            return self.async_abort(reason="oauth_error")

        # Refused at sign-in time, not only on the first poll: a person who
        # is not allowed to use SingleMall gets no entry at all.
        api = SingleMallApi(
            async_get_clientsession(self.hass),
            StaticTokenAuth(str(data["token"]["access_token"])),
            const.api_url(),
        )
        try:
            await api.async_get_lines()
        except ForbiddenError:
            return self.async_abort(reason="not_allowed")
        except AuthError:
            return self.async_abort(reason="oauth_unauthorized")
        except ConnectionFailed:
            return self.async_abort(reason="cannot_connect")
        except SingleMallError:
            _LOGGER.exception("Unexpected answer from SingleMall")
            return self.async_abort(reason="unknown")

        await self.async_set_unique_id(subject)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()

        email = claims.get("email")
        title = email if isinstance(email, str) and email else "SingleMall"
        return self.async_create_entry(title=title, data=data)
