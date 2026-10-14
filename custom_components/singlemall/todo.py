"""The shopping list as one to-do list entity.

An item's ``uid`` is the line's ``id``; its summary is the line's name
alone, so Home Assistant's own list intents match it by name; its
description is the note, preceded by ``×N`` when the quantity is above 1;
it is ``completed`` when the line is ticked. An edited description is read
back the same way: its ``×N`` is the quantity, and a description without one
means a quantity of 1.
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AuthError, ForbiddenError, Line, NotFoundError, RequestError, SingleMallError
from .const import DOMAIN, QUANTITY_SIGN
from .coordinator import SingleMallConfigEntry, SingleMallCoordinator

PARALLEL_UPDATES = 1

# "×3" or "×3 — note" or "×3\nnote"; only the multiplication sign counts,
# so a note that happens to start with "x2" stays a note.
_QUANTITY_PREFIX = re.compile(
    rf"^\s*{QUANTITY_SIGN}\s*(\d+)(?:\s*(?:—|–|-|\n)\s*|\s+|$)(.*)$", re.DOTALL
)
_SEPARATOR = " — "


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SingleMallConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the shopping list."""
    async_add_entities([SingleMallShoppingList(entry.runtime_data)])


def describe(line: Line) -> str | None:
    """The item description: ``×N`` when N > 1, then the note."""
    quantity = f"{QUANTITY_SIGN}{line.quantity}" if line.quantity > 1 else None
    if quantity and line.note:
        return f"{quantity}{_SEPARATOR}{line.note}"
    return quantity or line.note


def read_description(description: str | None) -> tuple[int | None, str | None]:
    """Split a description back into a quantity (if it names one) and a note."""
    if description is None:
        return None, None
    match = _QUANTITY_PREFIX.match(description)
    if match is None:
        note = description.strip()
        return None, note or None
    note = match.group(2).strip()
    return int(match.group(1)), note or None


def to_item(line: Line) -> TodoItem:
    """One line as a to-do item."""
    return TodoItem(
        uid=line.id,
        summary=line.name,
        description=describe(line),
        status=TodoItemStatus.COMPLETED if line.ticked else TodoItemStatus.NEEDS_ACTION,
    )


class SingleMallShoppingList(CoordinatorEntity[SingleMallCoordinator], TodoListEntity):
    """The person's shopping list: the group's when they are in one."""

    _attr_has_entity_name = True
    _attr_translation_key = "shopping_list"
    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, coordinator: SingleMallCoordinator) -> None:
        """One list per signed-in person."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.unique_id}-shopping-list"
        # A stable id whatever language Home Assistant runs in, so voice
        # sentences and automations can name it: todo.singlemall_shopping_list.
        self.entity_id = "todo.singlemall_shopping_list"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(entry.unique_id))},
            name="SingleMall",
            manufacturer="SingleMall",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """The lines, in the order the list keeps them."""
        if self.coordinator.data is None:
            return None
        return [to_item(line) for line in self.coordinator.data]

    def _line(self, uid: str | None) -> Line:
        for line in self.coordinator.data or []:
            if line.id == uid:
                return line
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="item_not_found",
        )

    def _raise_for(self, err: SingleMallError) -> None:
        """Turn an API failure into the error Home Assistant shows."""
        if isinstance(err, (AuthError, ForbiddenError)):
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        if isinstance(err, NotFoundError):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="item_not_found"
            ) from err
        if isinstance(err, RequestError) and err.code in (
            "product_name",
            "invalid_body",
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key=err.code
            ) from err
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="request_failed",
            translation_placeholders={"code": err.code},
        ) from err

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add words to the list (or raise the line that already has them)."""
        name = (item.summary or "").strip()
        if not name:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_body")
        quantity, note = read_description(item.description)
        try:
            line = await self.coordinator.api.async_add_item(name, note=note, quantity=quantity)
        except SingleMallError as err:
            self._raise_for(err)
            return
        self.coordinator.async_apply_line(line)
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Retype, note, tick or untick: only what changed is sent."""
        current = self._line(item.uid)
        changes: dict[str, Any] = {}

        if item.summary is not None and item.summary.strip() != current.name:
            changes["name"] = item.summary.strip()
        if item.status is not None:
            ticked = item.status == TodoItemStatus.COMPLETED
            if ticked != current.ticked:
                changes["ticked"] = ticked
        if item.description != describe(current):
            quantity, note = read_description(item.description)
            if note != current.note:
                # None clears the note.
                changes["note"] = note
            if quantity is None:
                # The description names no "×N" any more — cleared whole, or
                # only its prefix removed: the quantity goes back to one, as
                # the description now shows it.
                quantity = 1
            if quantity is not None and quantity != current.quantity:
                changes["quantity"] = quantity

        if not changes:
            return
        try:
            line = await self.coordinator.api.async_update_item(current.id, changes)
        except SingleMallError as err:
            self._raise_for(err)
            return
        if line.id != current.id:
            # New words are another line, as on the phone: the old one is gone.
            self.coordinator.async_drop_lines({current.id})
        self.coordinator.async_apply_line(line)
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Remove lines; one already gone counts as removed."""
        removed: set[str] = set()
        try:
            for uid in uids:
                try:
                    await self.coordinator.api.async_delete_item(uid)
                except NotFoundError:
                    pass
                removed.add(uid)
        except SingleMallError as err:
            if removed:
                self.coordinator.async_drop_lines(removed)
            self._raise_for(err)
            return
        self.coordinator.async_drop_lines(removed)
        await self.coordinator.async_request_refresh()
