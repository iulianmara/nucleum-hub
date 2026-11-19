"""The Lists' memory in Home Assistant (D-104): the history, the removed items and the purchases of every to-do list,
kept on the server whether or not a screen is on, so the dashboard's Lists page can show them and bring a removed
item back, and the customer writes no automation for any of it.

The tracker reads a list's items (``todo.get_items``) once Home Assistant runs, and again a moment after the list's
entity changes (its state is the number of open items; Home Assistant writes it on every change of an item). Two
readings become events (lists_core.diff), kept in a store of this integration (lists_core.apply). The dashboard reads
them with ``wall_dashboard/lists/info`` and brings an item back with ``wall_dashboard/lists/restore`` (websocket.py).
The logic itself, pure, is lists_core.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from . import lists_core as core

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "wall_dashboard.lists"
STORE_VERSION = 1
# a list's entity changed: read it after this long, so a burst of changes is read once
READ_AFTER = 1.5
SAVE_DELAY = 10.0
TODO = "todo"


class ListsTracker:
    """The history, the removed items and the purchases of every to-do list of this Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.state: dict[str, Any] = core.empty()
        self.readings: dict[str, dict[str, dict[str, Any]]] = {}
        self._timers: dict[str, Any] = {}
        self._unsub: list[Any] = []
        self._running = False
        self._dirty = False
        # told what changed on a list after each reading: f(entity_id, events) (the notifications' digest, D-111)
        self.listeners: list[Any] = []

    async def async_start(self) -> None:
        """Load the memory, then read every list once Home Assistant runs and follow their changes."""
        try:
            self.state = core.valid(await self.store.async_load())
        except Exception:  # noqa: BLE001 (a damaged store starts empty; the lists themselves are untouched)
            _LOGGER.exception("The Lists' memory could not be read; it starts empty")
            self.state = core.empty()
        self._running = True
        self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed))
        if getattr(self.hass, "state", None) == CoreState.running:
            self._read_all()
        else:
            self._unsub.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, lambda _e: self._read_all()))

    async def async_stop(self) -> None:
        """Stop following the lists and save what is kept."""
        self._running = False
        for un in self._unsub:
            try:
                un()
            except Exception:  # noqa: BLE001
                pass
        self._unsub.clear()
        for t in self._timers.values():
            t()
        self._timers.clear()
        if not self._dirty:
            return   # nothing new since it was read: the store is left as it is
        try:
            await self.store.async_save(self.state)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("The Lists' memory could not be saved")

    # --- reading the lists ----------------------------------------------------------------------------------------
    @callback
    def _read_all(self) -> None:
        for entity_id in self.hass.states.async_entity_ids(TODO):
            self._soon(entity_id, 0)

    @callback
    def _changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if isinstance(entity_id, str) and entity_id.startswith(TODO + "."):
            if event.data.get("new_state") is None:
                self.readings.pop(entity_id, None)   # the list left Home Assistant
                return
            self._soon(entity_id, READ_AFTER)

    @callback
    def _soon(self, entity_id: str, delay: float) -> None:
        if not self._running:
            return
        old = self._timers.pop(entity_id, None)
        if old:
            old()

        @callback
        def _go(_now: Any) -> None:
            self._timers.pop(entity_id, None)
            self.hass.async_create_task(self.async_read(entity_id), f"wall_dashboard lists {entity_id}")

        self._timers[entity_id] = async_call_later(self.hass, delay, _go)

    async def async_read(self, entity_id: str) -> None:
        """Read one list and keep what happened since the last reading."""
        try:
            res = await self.hass.services.async_call(TODO, "get_items", {"entity_id": entity_id}, blocking=True,
                                                      return_response=True)
        except Exception:  # noqa: BLE001 (a list that cannot be read now is read again at its next change)
            _LOGGER.debug("The to-do list %s could not be read", entity_id, exc_info=True)
            return
        items = ((res or {}).get(entity_id) or {}).get("items")
        after = core.clean(items)
        before = self.readings.get(entity_id)
        self.readings[entity_id] = after
        now = time.time()
        events = core.diff(before, after, now)
        for f in list(self.listeners):
            try:
                f(entity_id, events)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("A listener of the Lists failed")
        if core.apply(self.state, entity_id, self.is_shop(entity_id), events, now):
            self._dirty = True
            self.store.async_delay_save(lambda: self.state, SAVE_DELAY)

    def is_shop(self, entity_id: str) -> bool:
        st = self.hass.states.get(entity_id)
        name = st.attributes.get("friendly_name") if st else None
        try:
            entry = er.async_get(self.hass).async_get(entity_id)
            platform = entry.platform if entry else None
        except Exception:  # noqa: BLE001 (by its name, then)
            platform = None
        return core.is_shop(entity_id, name, platform)

    # --- what the dashboard reads and asks ---------------------------------------------------------------------
    def open_names(self) -> list[str]:
        """The names of every open item on every list (a suggestion already on a list is not suggested)."""
        return [i["summary"] for r in self.readings.values() for i in r.values() if i["status"] != "completed"]

    def info(self, limit: int = 100) -> dict[str, Any]:
        """``wall_dashboard/lists/info``: the newest history first, the removed items, the suggestions, the catalogue."""
        now = time.time()
        core.prune(self.state, now)
        names = self.open_names()
        return {
            "history": list(reversed(self.state["history"][-limit:])),
            "deleted": list(reversed(self.state["deleted"])),
            "suggestions": core.suggestions(self.state["catalog"], names, now),
            "catalog": core.catalog_view(self.state["catalog"], names),
        }

    def pin(self, name: str, pin: bool) -> bool:
        """Pin or unpin a product of the catalogue; False when it is not there."""
        if not core.set_pin(self.state["catalog"], name, pin):
            return False
        self._dirty = True
        self.store.async_delay_save(lambda: self.state, SAVE_DELAY)
        return True

    def snooze(self, name: str) -> bool:
        """"Not now" on a suggestion or a pinned product: hidden until the next shopping; False when it is not there."""
        if not core.set_snooze(self.state["catalog"], name, time.time()):
            return False
        self._dirty = True
        self.store.async_delay_save(lambda: self.state, SAVE_DELAY)
        return True

    async def async_restore(self, at: float, summary: str) -> bool:
        """Bring a removed item back to its list as it was; False when it is not kept (any more)."""
        for i, d in enumerate(self.state["deleted"]):
            if d.get("summary") == summary and abs(float(d.get("at", 0)) - at) < 0.001:
                st = self.hass.states.get(d["list"])
                if st is None:
                    return False
                features = st.attributes.get("supported_features") or 0
                await self.hass.services.async_call(TODO, "add_item", core.restore_data(d, int(features)), blocking=True,
                                                    target={"entity_id": d["list"]})
                del self.state["deleted"][i]
                self._dirty = True
                self.store.async_delay_save(lambda: self.state, SAVE_DELAY)
                return True
        return False
