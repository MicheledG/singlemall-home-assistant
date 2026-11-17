"""The one repair the mirror raises: an emptied list, confirmed or left alone."""

from __future__ import annotations

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN
from .mirror import MIRRORS


class MirrorEmptiedFlow(ConfirmRepairFlow):
    """Asks whether the emptied list is meant, and applies it when it is."""

    def __init__(self, entry_id: str) -> None:
        """Remember which entry's mirror to release."""
        self._entry_id = entry_id

    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> FlowResult:
        """Let the held run through."""
        if user_input is not None:
            mirror = self.hass.data.get(DOMAIN, {}).get(MIRRORS, {}).get(self._entry_id)
            if mirror is not None:
                mirror.apply_anyway()
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm")


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """The flow behind the Fix button of the mirror's repair."""
    return MirrorEmptiedFlow(str((data or {}).get("entry_id", "")))
