"""The shopping list, read with ``If-None-Match`` when the doorbell rings and every 30 minutes."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, ForbiddenError, Line, SingleMallApi, SingleMallError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

type SingleMallConfigEntry = ConfigEntry[SingleMallCoordinator]


class SingleMallCoordinator(DataUpdateCoordinator[list[Line]]):
    """Holds the last lines served and the ETag they were served with."""

    config_entry: SingleMallConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: SingleMallConfigEntry, api: SingleMallApi
    ) -> None:
        """Read the list for one config entry."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.etag: str | None = None

    async def _async_update_data(self) -> list[Line]:
        """Read the list; a 304 keeps what is already held."""
        # Without lines held there is nothing a 304 could keep.
        etag = self.etag if self.data is not None else None
        try:
            snapshot = await self.api.async_get_lines(etag)
        except (AuthError, ForbiddenError) as err:
            # A refused refresh, a token the API no longer accepts, or a
            # person no longer allowed to use SingleMall: only signing in
            # again can change that, so Home Assistant asks for it.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        except SingleMallError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"code": err.code},
            ) from err
        self.etag = snapshot.etag
        if snapshot.lines is None:
            return self.data
        return snapshot.lines

    @callback
    def async_apply_line(self, line: Line) -> None:
        """Show a line the API just returned, before the next poll confirms it."""
        lines = list(self.data or [])
        for index, held in enumerate(lines):
            if held.id == line.id:
                lines[index] = line
                break
        else:
            lines.append(line)
        self.async_set_updated_data(lines)

    @callback
    def async_drop_lines(self, line_ids: set[str]) -> None:
        """Stop showing lines just removed, before the next poll confirms it."""
        self.async_set_updated_data([line for line in self.data or [] if line.id not in line_ids])
