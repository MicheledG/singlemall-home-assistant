"""Diagnostics: what helps debug the integration, and no token or list words."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from yarl import URL

from . import const
from .coordinator import SingleMallConfigEntry

TO_REDACT = {
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "unique_id",
    "title",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SingleMallConfigEntry
) -> dict[str, Any]:
    """Counts and state only: the lines' words stay private."""
    coordinator = entry.runtime_data
    lines = coordinator.data or []
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "api_host": URL(const.api_url()).host,
        "missing_constants": const.missing_constants(),
        "last_update_success": coordinator.last_update_success,
        "has_etag": coordinator.etag is not None,
        "lines": {
            "total": len(lines),
            "ticked": sum(1 for line in lines if line.ticked),
            "typed": sum(1 for line in lines if line.typed),
            "with_note": sum(1 for line in lines if line.note),
        },
    }
