"""The heating stopped from a window's notification turns back on by itself (D-146 K2), whether or not a screen is on.

A screen that turned a room's thermostats off ("Oprește încălzirea în …") hands them over with
``wall_dashboard/heat/pause`` (the windows to watch, each thermostat's mode before, the text to say in the screen's
language). When every window of the pause is closed, the thermostats still off go back to their mode
(``climate.set_hvac_mode``) and Home Assistant's notification says so (``persistent_notification.create``, with the
text the screen gave). ``wall_dashboard/heat/resume`` forgets a pause (the heating turned on from a screen). The pauses
are kept in a store of this integration, so a restart of Home Assistant does not lose them. The logic itself, pure, is
heat_core.py.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from . import heat_core as core

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "wall_dashboard.heat"
STORE_VERSION = 1


class HeatResume:
    """The rooms whose heating waits for their windows to close."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.state: dict[str, Any] = {"pauses": {}}
        self._unsub: list[Any] = []
        self._running = False

    async def async_start(self) -> None:
        try:
            self.state = core.valid(await self.store.async_load())
        except Exception:  # noqa: BLE001 (a damaged store starts empty)
            _LOGGER.exception("The heating's pauses could not be read; they start empty")
            self.state = {"pauses": {}}
        self._running = True
        self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed))
        if getattr(self.hass, "state", None) == CoreState.running:
            self._check_all()
        else:
            self._unsub.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, lambda _e: self._check_all()))

    async def async_stop(self) -> None:
        self._running = False
        for un in self._unsub:
            try:
                un()
            except Exception:  # noqa: BLE001
                pass
        self._unsub.clear()

    # --- the commands -------------------------------------------------------------------------------------------
    def pause(self, climate: Any, windows: Any, text: Any, title: Any) -> bool:
        p = core.clean(climate, windows, text, title, self._now())
        if p is None:
            return False
        core.add(self.state, p)
        self._save()
        self._check(core.key_of(p))   # the window may already be closed again
        return True

    def resume(self, climate_ids: Any) -> int:
        gone = core.drop(self.state, climate_ids if isinstance(climate_ids, list) else [])
        if gone:
            self._save()
        return len(gone)

    # --- following the windows ----------------------------------------------------------------------------------
    @callback
    def _changed(self, event: Event) -> None:
        eid = event.data.get("entity_id")
        if not self._running or not isinstance(eid, str) or not eid.startswith(("binary_sensor.", "climate.")):
            return
        for k, p in list(self.state["pauses"].items()):
            if eid in p["windows"] or any(c["id"] == eid for c in p["climate"]):
                self._check(k)

    @callback
    def _check_all(self) -> None:
        for k in list(self.state["pauses"]):
            self._check(k)

    @callback
    def _check(self, key: str) -> None:
        p = self.state["pauses"].get(key)
        if p is None:
            return
        if core.stale(p, self._now()):
            del self.state["pauses"][key]
            self._save()
            return
        states: dict[str, str | None] = {}
        for eid in p["windows"] + [c["id"] for c in p["climate"]]:
            s = self.hass.states.get(eid)
            states[eid] = s.state if s is not None else None
        # a window that left Home Assistant counts as closed: never keep the heating off for a sensor that is gone
        for w in p["windows"]:
            if states.get(w) is None:
                states.pop(w, None)
        todo = core.due(p, states)
        if todo is None:
            return
        del self.state["pauses"][key]
        self._save()
        if not todo:
            return   # turned on again by someone else meanwhile: nothing to do, nothing to say
        self.hass.async_create_task(self._turn_on(todo, p), "wall_dashboard heat resume")

    async def _turn_on(self, todo: list[tuple[str, str]], p: dict[str, Any]) -> None:
        for cid, mode in todo:
            try:
                await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": cid, "hvac_mode": mode}, blocking=True)
            except Exception:  # noqa: BLE001 (one thermostat refusing does not stop the others)
                _LOGGER.warning("The heating of %s could not be turned back on", cid)
        if p.get("text"):
            try:
                await self.hass.services.async_call("persistent_notification", "create", {
                    "title": p.get("title") or "Nucleum Hub", "message": p["text"], "notification_id": "wall_dashboard_heat_" + core.key_of(p).replace(".", "_").replace(",", "_")[:120]})
            except Exception:  # noqa: BLE001
                _LOGGER.warning("The notification of the heating turned back on could not be shown")

    # --- helpers ------------------------------------------------------------------------------------------------
    def _now(self) -> float:
        clock = getattr(self.hass, "clock", None)
        if callable(clock):
            return float(clock())
        import time
        return time.time()

    def _save(self) -> None:
        try:
            self.store.async_delay_save(lambda: self.state, 1.0)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("The heating's pauses could not be saved")
