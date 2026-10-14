"""SingleMall: the shopping list as a Home Assistant to-do list.

One config entry per signed-in person, one to-do entity per entry, fed by a
coordinator that polls the list; the sign-in is Home Assistant's own OAuth2
flow with PKCE.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientError
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import const
from .api import AuthError, OAuth2SessionAuth, SingleMallApi, SingleMallError
from .const import DOMAIN, REQUEST_TIMEOUT_SECONDS
from .coordinator import SingleMallConfigEntry, SingleMallCoordinator
from .oauth import async_register_implementation

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.TODO]


async def async_setup_entry(hass: HomeAssistant, entry: SingleMallConfigEntry) -> bool:
    """Set up the shopping list for one signed-in person."""
    if missing := const.missing_constants():
        _LOGGER.error(
            "SingleMall is not configured: fill %s in "
            "custom_components/singlemall/const.py from the stack outputs",
            ", ".join(missing),
        )
        raise ConfigEntryError(translation_domain=DOMAIN, translation_key="not_configured")

    # The implementation lives in memory only; after a restart the entry
    # registers it again so its refresh token can be used.
    async_register_implementation(hass)
    try:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    except (ValueError, config_entry_oauth2_flow.ImplementationUnavailableError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="implementation_unavailable"
        ) from err

    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    auth = OAuth2SessionAuth(session)
    try:
        await auth.async_get_access_token()
    except AuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="auth_failed"
        ) from err
    except SingleMallError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="update_failed",
            translation_placeholders={"code": err.code},
        ) from err

    api = SingleMallApi(async_get_clientsession(hass), auth, const.api_url())
    coordinator = SingleMallCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SingleMallConfigEntry) -> bool:
    """Unload the to-do list."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: SingleMallConfigEntry) -> None:
    """Revoke the entry's refresh token when the integration is removed.

    The refresh token lasts a year and is not rotated, so without this a copy
    of it — in a backup of Home Assistant's storage, say — would keep signing
    in long after the integration was removed. The client is public, so the
    revocation needs only its id. Best effort: removal never fails because
    the sign-in service could not be reached. An access token already issued
    stays valid until it expires (one hour), because the API verifies tokens
    without asking the sign-in service.
    """
    token = entry.data.get("token") or {}
    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token or const.missing_constants():
        return
    try:
        async with (
            asyncio.timeout(REQUEST_TIMEOUT_SECONDS),
            async_get_clientsession(hass).post(
                const.revoke_url(),
                data={"token": refresh_token, "client_id": const.client_id()},
            ) as response,
        ):
            if response.status >= 400:
                _LOGGER.warning(
                    "SingleMall: the sign-in service refused to revoke the removed "
                    "sign-in (HTTP %s); it expires on its own within a year",
                    response.status,
                )
    except (ClientError, TimeoutError):
        _LOGGER.warning(
            "SingleMall: could not reach the sign-in service to revoke the removed "
            "sign-in; it expires on its own within a year"
        )
