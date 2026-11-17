"""Config flow for SingleMall: add the integration, sign in, done."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)
import voluptuous as vol

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
from .mirror import CONF_ENTITY, CONF_INTERVAL, DEFAULT_INTERVAL, OWN_ENTITY_ID
from .oauth import async_register_implementation, token_claims

_LOGGER = logging.getLogger(__name__)


class SingleMallFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Sign in with the SingleMall account used on the phone."""

    DOMAIN = DOMAIN
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SingleMallOptionsFlow:
        """The options: which other to-do list to mirror, and how often to check it."""
        return SingleMallOptionsFlow()

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


class SingleMallOptionsFlow(OptionsFlowWithReload):
    """Pick the to-do list to mirror; leaving it empty turns the mirror off."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """One form: the list and the check interval in minutes."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        schema = vol.Schema(
            {
                vol.Optional(CONF_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="todo", exclude_entities=[OWN_ENTITY_ID])
                ),
                vol.Optional(CONF_INTERVAL, default=DEFAULT_INTERVAL): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=1440,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, self.config_entry.options),
        )
