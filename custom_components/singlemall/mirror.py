"""Keeps the shopping list and another to-do list in step.

The other list is any to-do entity the person picks in the options (the Alexa
shopping list, say). Three things ask for a run: a state change of the other
entity (it changes when the number of open lines does, which covers an
addition, a removal and a tick), an update of our own list, and a timer
every few minutes, which is the only way an edit in place (a retyped name, a
changed note) is ever seen. A run compares each list with what both looked
like at the end of the previous run, kept in storage, so that "added here"
and "removed there" can be told apart. SingleMall's version wins when both
lists changed the same line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.components.todo import (
    DOMAIN as TODO_DOMAIN,
    TodoItemStatus,
    TodoListEntityFeature,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store

from .api import Line, NotFoundError, SingleMallError
from .const import DOMAIN, QUANTITY_SIGN
from .coordinator import SingleMallConfigEntry
from .todo import describe_parts, read_description

_LOGGER = logging.getLogger(__name__)

CONF_ENTITY = "mirror_entity"
CONF_INTERVAL = "mirror_interval"
DEFAULT_INTERVAL = 15
OWN_ENTITY_ID = "todo.singlemall_shopping_list"

ISSUE_MASS_REMOVAL = "mirror_mass_removal"
# A run removing this many mirrored lines, and all of them, is not applied.
MASS_REMOVAL_MIN = 3

STORAGE_VERSION = 1

# "milk ×3" in a list that has no description to put the quantity in.
_TRAILING_QUANTITY = re.compile(rf"^(.*?)\s*{QUANTITY_SIGN}\s*(\d+)\s*$", re.DOTALL)


def storage_key(entry_id: str) -> str:
    """Where one entry's snapshot lives."""
    return f"{DOMAIN}.mirror.{entry_id}"


def norm(name: str) -> str:
    """The words as the app compares them: case and spaces do not count."""
    return "".join(name.lower().split())


@dataclass(frozen=True, slots=True)
class View:
    """One line as either list shows it."""

    name: str
    quantity: int
    ticked: bool
    note: str | None

    def key(self) -> tuple[str, int, bool, str]:
        """What a change is judged on."""
        return (norm(self.name), self.quantity, self.ticked, self.note or "")


@dataclass(slots=True)
class Pair:
    """A line of ours matched with a line of the other list."""

    line_id: str
    other_uid: str
    view: View

    def to_json(self) -> dict[str, Any]:
        """The pair as stored."""
        return {
            "line_id": self.line_id,
            "other_uid": self.other_uid,
            "name": self.view.name,
            "quantity": self.view.quantity,
            "ticked": self.view.ticked,
            "note": self.view.note,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Pair:
        """Read a stored pair."""
        return cls(
            line_id=str(value["line_id"]),
            other_uid=str(value["other_uid"]),
            view=View(
                name=str(value["name"]),
                quantity=int(value["quantity"]),
                ticked=bool(value["ticked"]),
                note=value.get("note"),
            ),
        )


def our_view(line: Line) -> View:
    """One of our lines as a view."""
    return View(line.name, line.quantity, line.ticked, line.note)


def split_quantity(summary: str) -> tuple[str, int | None]:
    """A trailing ``×N`` in a name is a quantity; the rest is the words."""
    match = _TRAILING_QUANTITY.match(summary)
    if match is None or not match.group(1).strip():
        return summary.strip(), None
    return match.group(1).strip(), int(match.group(2))


class Mirror:
    """One entry's mirror with one other to-do list."""

    def __init__(
        self, hass: HomeAssistant, entry: SingleMallConfigEntry, other: str, interval: int
    ) -> None:
        """Mirror this entry's list with the to-do entity ``other``."""
        self.hass = hass
        self.entry = entry
        self.other = other
        self.interval = timedelta(minutes=interval)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._pairs: list[Pair] | None = None
        self._dirty = False
        self._running = False
        self._unsubscribe: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        """Load what the last run left and start listening."""
        stored = await self._store.async_load()
        if stored is not None:
            self._pairs = [Pair.from_json(item) for item in stored.get("pairs", [])]
        self._unsubscribe = [
            async_track_state_change_event(self.hass, [self.other], self._on_other_change),
            self.entry.runtime_data.async_add_listener(self.request),
            async_track_time_interval(self.hass, self._on_interval, self.interval),
        ]
        self.request()

    @callback
    def async_stop(self) -> None:
        """Stop listening; a run in flight finishes on its own."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []

    @callback
    def _on_other_change(self, event: Event[EventStateChangedData]) -> None:
        self.request()

    @callback
    def _on_interval(self, now: Any) -> None:
        self.request()

    @callback
    def request(self) -> None:
        """Ask for a run; asking while one is going makes it run once more.

        A flag, not the task: the task starts eagerly, so a request made while
        its first step runs would find no task yet and start a second run.
        """
        self._dirty = True
        if self._running:
            return
        self._running = True
        self.entry.async_create_task(self.hass, self._drain(), f"{DOMAIN} mirror")

    async def _drain(self) -> None:
        try:
            while self._dirty:
                self._dirty = False
                try:
                    await self.async_reconcile()
                except (HomeAssistantError, SingleMallError) as err:
                    _LOGGER.warning("SingleMall mirror run failed: %s", err)
        finally:
            self._running = False

    # -- the other list ---------------------------------------------------

    def _supports_description(self) -> bool:
        state = self.hass.states.get(self.other)
        features = int(state.attributes.get("supported_features", 0)) if state else 0
        return bool(features & TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM)

    async def _read_other(self, snapshot: dict[str, Pair]) -> dict[str, View] | None:
        """The other list's lines by uid, or ``None`` when it cannot be read now."""
        state = self.hass.states.get(self.other)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        result = await self.hass.services.async_call(
            TODO_DOMAIN,
            "get_items",
            {},
            target={ATTR_ENTITY_ID: self.other},
            blocking=True,
            return_response=True,
        )
        described = self._supports_description()
        views: dict[str, View] = {}
        for item in result[self.other]["items"]:
            name, name_quantity = split_quantity(str(item.get("summary") or ""))
            if not name:
                continue
            quantity: int | None = None
            note: str | None = None
            if described:
                quantity, note = read_description(item.get("description"))
            else:
                # Nothing to hold a note: keep the one we remember.
                known = snapshot.get(str(item["uid"]))
                note = known.view.note if known else None
            views[str(item["uid"])] = View(
                name=name,
                quantity=quantity or name_quantity or 1,
                ticked=item.get("status") == TodoItemStatus.COMPLETED,
                note=note,
            )
        return views

    def _summary(self, view: View) -> str:
        if view.quantity > 1 and not self._supports_description():
            return f"{view.name} {QUANTITY_SIGN}{view.quantity}"
        return view.name

    def _description(self, view: View) -> str:
        return describe_parts(view.quantity, view.note) or ""

    async def _call_other(self, service: str, data: dict[str, Any]) -> None:
        await self.hass.services.async_call(
            TODO_DOMAIN,
            service,
            data,
            target={ATTR_ENTITY_ID: self.other},
            blocking=True,
        )

    async def _other_update(self, uid: str, view: View) -> None:
        data: dict[str, Any] = {
            "item": uid,
            "rename": self._summary(view),
            "status": TodoItemStatus.COMPLETED if view.ticked else TodoItemStatus.NEEDS_ACTION,
        }
        if self._supports_description():
            data["description"] = self._description(view)
        await self._call_other("update_item", data)

    async def _other_add(self, view: View) -> None:
        data: dict[str, Any] = {"item": self._summary(view)}
        if self._supports_description() and (description := self._description(view)):
            data["description"] = description
        await self._call_other("add_item", data)

    # -- our list ---------------------------------------------------------

    async def _our_add(self, view: View) -> Line:
        coordinator = self.entry.runtime_data
        line = await coordinator.api.async_add_item(
            view.name, note=view.note, quantity=view.quantity if view.quantity > 1 else None
        )
        coordinator.async_apply_line(line)
        return line

    async def _our_update(self, line: Line, view: View) -> Line:
        coordinator = self.entry.runtime_data
        changes: dict[str, Any] = {}
        if norm(view.name) != norm(line.name):
            changes["name"] = view.name
        if view.ticked != line.ticked:
            changes["ticked"] = view.ticked
        if (view.note or None) != line.note:
            changes["note"] = view.note
        if view.quantity != line.quantity:
            changes["quantity"] = view.quantity
        if not changes:
            return line
        updated = await coordinator.api.async_update_item(line.id, changes)
        if updated.id != line.id:
            coordinator.async_drop_lines({line.id})
        coordinator.async_apply_line(updated)
        return updated

    async def _our_remove(self, line_id: str) -> None:
        coordinator = self.entry.runtime_data
        try:
            await coordinator.api.async_delete_item(line_id)
        except NotFoundError:
            pass
        coordinator.async_drop_lines({line_id})

    # -- the run ----------------------------------------------------------

    async def async_reconcile(self) -> None:
        """Bring the two lists in step, and remember how they ended."""
        coordinator = self.entry.runtime_data
        if coordinator.data is None or not coordinator.last_update_success:
            return
        pairs = list(self._pairs or [])
        theirs = await self._read_other({pair.other_uid: pair for pair in pairs})
        if theirs is None:
            return
        ours = {line.id: line for line in coordinator.data}
        self._repair_uids(pairs, theirs)

        if self._mass_removal(pairs, ours, theirs):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_MASS_REMOVAL}_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_MASS_REMOVAL,
            )
            return
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_MASS_REMOVAL}_{self.entry.entry_id}")

        try:
            await self._sync(pairs, ours, theirs)
        finally:
            self._pairs = pairs
            await self._store.async_save({"pairs": [pair.to_json() for pair in pairs]})

    @staticmethod
    def _repair_uids(pairs: list[Pair], theirs: dict[str, View]) -> None:
        """A line the other list gave a new uid (cleared, then filled again) is the same line."""
        known = {pair.other_uid for pair in pairs}
        loose: dict[str, str] = {}
        for uid, view in theirs.items():
            if uid not in known:
                loose.setdefault(norm(view.name), uid)
        for pair in pairs:
            if pair.other_uid not in theirs and (uid := loose.pop(norm(pair.view.name), None)):
                pair.other_uid = uid

    def _mass_removal(
        self, pairs: list[Pair], ours: dict[str, Line], theirs: dict[str, View]
    ) -> bool:
        """Would this run take every mirrored line off our list, three or more?"""
        held = [pair for pair in pairs if pair.line_id in ours]
        gone = [pair for pair in held if pair.other_uid not in theirs]
        return len(gone) >= MASS_REMOVAL_MIN and len(gone) == len(held)

    async def _sync(
        self,
        pairs: list[Pair],
        ours: dict[str, Line],
        theirs: dict[str, View],
    ) -> None:
        paired_lines = {pair.line_id for pair in pairs}
        paired_uids = {pair.other_uid for pair in pairs}

        # Lines already paired: removals, then changes.
        for pair in list(pairs):
            ours_line = ours.get(pair.line_id)
            theirs_view = theirs.get(pair.other_uid)
            if ours_line is None and theirs_view is None:
                pairs.remove(pair)
            elif ours_line is None:
                # Removed from our list: remove it there too.
                await self._call_other("remove_item", {"item": [pair.other_uid]})
                pairs.remove(pair)
            elif theirs_view is None:
                await self._our_remove(pair.line_id)
                pairs.remove(pair)
            else:
                mine = our_view(ours_line)
                if mine.key() != pair.view.key():
                    # Ours changed (and wins if theirs did too).
                    if mine.key() != theirs_view.key():
                        await self._other_update(pair.other_uid, mine)
                    pair.view = mine
                elif theirs_view.key() != pair.view.key():
                    updated = await self._our_update(ours_line, theirs_view)
                    pair.line_id = updated.id
                    pair.view = theirs_view

        # Lines on one list only: match by words, copy the rest across.
        new_ours = [line for line in ours.values() if line.id not in paired_lines]
        new_theirs = {uid: view for uid, view in theirs.items() if uid not in paired_uids}
        unmatched_ours: list[Line] = []
        for line in new_ours:
            uid = next((u for u, v in new_theirs.items() if norm(v.name) == norm(line.name)), None)
            if uid is None:
                unmatched_ours.append(line)
                continue
            pairs.append(Pair(line.id, uid, our_view(line)))
            if our_view(line).key() != new_theirs[uid].key():
                await self._other_update(uid, our_view(line))
            del new_theirs[uid]

        # Ours only: add to the other list, then find the uid it was given.
        if unmatched_ours:
            known = set(theirs)
            for line in unmatched_ours:
                await self._other_add(our_view(line))
            after = await self._read_other({}) or {}
            fresh = {uid: view for uid, view in after.items() if uid not in known}
            for line in unmatched_ours:
                uid = next((u for u, v in fresh.items() if norm(v.name) == norm(line.name)), None)
                if uid is None:
                    continue
                del fresh[uid]
                view = our_view(line)
                pairs.append(Pair(line.id, uid, view))
                if view.ticked:
                    await self._other_update(uid, view)

        # Theirs only: add to our list as words.
        for uid, view in new_theirs.items():
            line = await self._our_add(view)
            if any(pair.line_id == line.id for pair in pairs):
                # The words were already on the list: our line's quantity was
                # raised and the other list's duplicate has done its job.
                await self._call_other("remove_item", {"item": [uid]})
                continue
            if view.ticked:
                line = await self._our_update(line, view)
            pairs.append(Pair(line.id, uid, view))
