"""The Lists' notifications on the phones (D-111, "My notifications"): sent by this integration through the Home
Assistant app (``notify.mobile_app_<phone>``), with no automation written by the customer.

Each person chooses their own (``wall_dashboard/notify/prefs``, websocket.py): each kind on or off, the lists they come
from, their quiet hours (only "Ping urgent" passes), in the language of the screen they chose them on (the dashboard
hands its texts over). An administrator may open another member's. What is sent:
- ``due``: a task whose moment has come (its time, or 09:00 of its day), to its person ("pers=" in its description) or to
  everyone when it has none; told once;
- ``digest``: what was added to a list, once, 15 minutes after the last addition;
- ``go`` / ``done`` / ``ping``: told by a screen (``wall_dashboard/notify/send``), to everyone but the one who sent it
  (a ping to the chosen member, or to everyone else);
- ``store``: arriving at a store zone, what is still to buy there ("mag=" in the items), to the one who arrived; "Passing
  by" tells it at once, "In the store" only after ``DWELL`` inside; "Not today" holds it until midnight; a zone turned off
  is never told (``wall_dashboard/places``, the same settings for the whole house);
- ``place``: a task with "loc=zone.x la=in|out" in its description, when its person (or anyone, for a task without one)
  arrives at or leaves that zone; once a day; a task due on a later day waits for its day.
The zones are Home Assistant's own (the dashboard creates one with ``zone/create``); a person's zone is their state.
The rules themselves, pure: notify_core.py."""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import notify_core as core

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "wall_dashboard.notify"
STORE_VERSION = 1
TOLD_MAX = 500
SAVE_DELAY = 5.0


class ListNotifier:
    """Every person's choices and what is sent to their phones."""

    def __init__(self, hass: HomeAssistant, lists: Any) -> None:
        self.hass = hass
        self.lists = lists
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.data: dict[str, Any] = {"users": {}, "told": [], "places": {}}
        self._tick: Any = None
        self._dwell: dict[tuple[str, str], Any] = {}
        self._unsub: Any = None
        self._digest: dict[str, dict[str, Any]] = {}
        self._running = False

    async def async_start(self) -> None:
        try:
            raw = await self.store.async_load()
        except Exception:  # noqa: BLE001 (a damaged store starts with everyone's defaults)
            _LOGGER.exception("The Lists' notification choices could not be read; everyone starts with the defaults")
            raw = None
        if isinstance(raw, dict):
            users = raw.get("users") if isinstance(raw.get("users"), dict) else {}
            self.data["users"] = {u: core.valid_prefs(p) for u, p in users.items() if isinstance(u, str)}
            self.data["told"] = [k for k in raw.get("told") or [] if isinstance(k, str)][-TOLD_MAX:]
            places = raw.get("places") if isinstance(raw.get("places"), dict) else {}
            self.data["places"] = {z: p for z, p in places.items() if isinstance(z, str) and z.startswith("zone.") and isinstance(p, dict)}
        self._running = True
        self._unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._state_changed)
        if self.lists is not None:
            self.lists.listeners.append(self._list_events)
        self._schedule()

    async def async_stop(self) -> None:
        self._running = False
        if self._tick:
            self._tick()
            self._tick = None
        for d in self._digest.values():
            if d.get("timer"):
                d["timer"]()
        self._digest.clear()
        for t in self._dwell.values():
            t()
        self._dwell.clear()
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self.lists is not None and self._list_events in self.lists.listeners:
            self.lists.listeners.remove(self._list_events)

    def _save(self) -> None:
        self.store.async_delay_save(lambda: self.data, SAVE_DELAY)

    # --- who is in the house ---------------------------------------------------------------------------------------
    def members(self) -> list[dict[str, Any]]:
        """The people of Home Assistant who log in: [{user_id, person, name, phones}]."""
        entries = self.hass.config_entries.async_entries("mobile_app")
        out = []
        for eid in sorted(self.hass.states.async_entity_ids("person")):
            st = self.hass.states.get(eid)
            uid = st.attributes.get("user_id") if st else None
            if isinstance(uid, str) and uid:
                out.append({"user_id": uid, "person": eid, "name": st.attributes.get("friendly_name") or eid,
                            "phones": core.phones_of(entries, uid)})
        return out

    def name_of(self, user_id: str | None) -> str:
        for m in self.members():
            if m["user_id"] == user_id:
                return str(m["name"])
        return "?"

    def prefs(self, user_id: str) -> dict[str, Any]:
        return self.data["users"].get(user_id) or core.default_prefs()

    def set_prefs(self, user_id: str, patch: Any) -> dict[str, Any]:
        self.data["users"][user_id] = core.merge_prefs(self.prefs(user_id), patch)
        self._save()
        return self.data["users"][user_id]

    # --- sending ---------------------------------------------------------------------------------------------------
    async def async_send(self, user_id: str, kind: str, list_id: str | None = None, **values: Any) -> int:
        """Send one kind to one person's phones, when they want it and it may go now; how many phones it went to."""
        p = self.prefs(user_id)
        if kind != "test" and not core.wants(p, kind, list_id):
            return 0
        if not core.may_send(p, kind, dt_util.as_local(dt_util.utc_from_timestamp(self._now()))):
            return 0
        phones = next((m["phones"] for m in self.members() if m["user_id"] == user_id), [])
        sent = 0
        for ph in phones:
            data: dict[str, Any] = {"group": "nucleum-hub-lists", "tag": f"nucleum-hub-{kind}"}
            if kind == "ping":
                data.update({"push": {"interruption-level": "time-sensitive"}, "priority": "high", "ttl": 0})
            try:
                await self.hass.services.async_call("notify", ph["service"], {
                    "title": core.fill(p, "title"), "message": core.fill(p, kind, **values), "data": data}, blocking=True)
                sent += 1
            except Exception:  # noqa: BLE001 (a phone that is gone; the others still get it)
                _LOGGER.debug("The notification could not go to %s", ph["service"], exc_info=True)
        return sent

    async def async_event(self, sender: str | None, kind: str, list_id: str | None = None, to: str | None = None,
                          **values: Any) -> int:
        """"go", "done" or "ping" told by a screen: to everyone but the sender (a ping to one member when chosen)."""
        who = self.name_of(sender)
        n = 0
        for m in self.members():
            if m["user_id"] == sender or (to and m["user_id"] != to):
                continue
            n += await self.async_send(m["user_id"], kind, list_id, who=who, **values)
        return n

    def _now(self) -> float:
        return time.time()

    # --- the tasks due ---------------------------------------------------------------------------------------------
    @callback
    def _schedule(self) -> None:
        """One timer, at the next task's moment (none while no task has one); armed again after every reading."""
        if self._tick:
            self._tick()
            self._tick = None
        if not self._running or self.lists is None:
            return
        local = dt_util.as_local(dt_util.utc_from_timestamp(self._now()))
        nxt = core.next_due(self.lists.readings, local, self.data["told"])
        if nxt is None:
            return

        @callback
        def _go(_now: Any) -> None:
            self._tick = None
            self.hass.async_create_task(self.async_check_due(), "wall_dashboard notify due")

        self._tick = async_call_later(self.hass, max(1.0, nxt - local.timestamp()), _go)

    async def async_check_due(self) -> None:
        if self.lists is None:
            return
        local = dt_util.as_local(dt_util.utc_from_timestamp(self._now()))
        due = core.due_now(self.lists.readings, local, self.data["told"])
        if not due:
            self._schedule()
            return
        members = self.members()
        for d in due:
            self.data["told"].append(d["key"])
            for m in members:
                if d["person"] and m["person"] != d["person"]:
                    continue
                await self.async_send(m["user_id"], "due", d["list"], n=d["summary"])
        self.data["told"] = self.data["told"][-TOLD_MAX:]
        self._save()
        self._schedule()

    # --- the digest after changes ----------------------------------------------------------------------------------
    @callback
    def _list_events(self, list_id: str, events: list[dict[str, Any]]) -> None:
        self._schedule()   # a task added, moved or done: its moment
        added = [e["summary"] for e in events if e.get("kind") == "added"]
        if not added or not self._running:
            return
        d = self._digest.setdefault(list_id, {"names": [], "timer": None})
        d["names"].extend(n for n in added if n not in d["names"])
        if d["timer"]:
            d["timer"]()

        @callback
        def _go(_now: Any) -> None:
            item = self._digest.pop(list_id, None)
            if item and item["names"]:
                self.hass.async_create_task(self._send_digest(list_id, item["names"]), "wall_dashboard notify digest")

        d["timer"] = async_call_later(self.hass, core.DIGEST_AFTER, _go)

    async def _send_digest(self, list_id: str, names: list[str]) -> None:
        st = self.hass.states.get(list_id)
        title = (st.attributes.get("friendly_name") if st else None) or list_id
        text = ", ".join(names[:6]) + (f" (+{len(names) - 6})" if len(names) > 6 else "")
        for m in self.members():
            await self.async_send(m["user_id"], "digest", list_id, l=title, n=text)

    # --- the places: stores and the tasks at a place ---------------------------------------------------------------
    def zones(self) -> dict[str, str]:
        """Home Assistant's zones: {entity_id: name}."""
        out = {}
        for eid in self.hass.states.async_entity_ids("zone"):
            st = self.hass.states.get(eid)
            out[eid] = str((st.attributes.get("friendly_name") if st else None) or eid)
        return out

    def place(self, zone: str, name: Any = None) -> dict[str, Any]:
        return core.valid_place(self.data["places"].get(zone), name if name is not None else self.zones().get(zone, ""))

    def set_places(self, patch: Any) -> None:
        if not isinstance(patch, dict):
            return
        zones = self.zones()
        for z, p in patch.items():
            if z in zones and isinstance(p, dict):
                self.data["places"][z] = core.valid_place({**self.place(z, zones[z]), **p}, zones[z])
        self._save()

    def places_answer(self) -> dict[str, Any]:
        """``wall_dashboard/places``: every zone with its settings (the dashboard reads the zones themselves from HA)."""
        return {"places": {z: self.place(z, n) for z, n in self.zones().items()}, "dwell": core.DWELL}

    @callback
    def _state_changed(self, event: Any) -> None:
        eid = event.data.get("entity_id") or ""
        if not eid.startswith("person.") or not self._running:
            return
        old, new = event.data.get("old_state"), event.data.get("new_state")
        zones = self.zones()
        a = core.zone_of(getattr(old, "state", None), zones)
        b = core.zone_of(getattr(new, "state", None), zones)
        if a == b:
            return
        if a:
            t = self._dwell.pop((eid, a), None)
            if t:
                t()
            self.hass.async_create_task(self._place_tasks(eid, a, "out"), "wall_dashboard notify leave")
        if b:
            self.hass.async_create_task(self._place_tasks(eid, b, "in"), "wall_dashboard notify arrive")
            self._arrive_store(eid, b, zones.get(b, b))

    def _member_of(self, person: str) -> dict[str, Any] | None:
        return next((m for m in self.members() if m["person"] == person), None)

    @callback
    def _arrive_store(self, person: str, zone: str, name: str) -> None:
        p = self.place(zone, name)
        today = dt_util.as_local(dt_util.utc_from_timestamp(self._now())).date().isoformat()
        if p["kind"] != "store" or not p["on"] or p["skip"] == today:
            return
        if p["mode"] == "pass":
            self.hass.async_create_task(self._send_store(person, zone, name), "wall_dashboard notify store")
            return

        @callback
        def _go(_now: Any) -> None:
            self._dwell.pop((person, zone), None)
            st = self.hass.states.get(person)
            if st is not None and core.zone_of(st.state, self.zones()) == zone:
                self.hass.async_create_task(self._send_store(person, zone, name), "wall_dashboard notify store")

        old = self._dwell.pop((person, zone), None)
        if old:
            old()
        self._dwell[(person, zone)] = async_call_later(self.hass, core.DWELL, _go)

    async def _send_store(self, person: str, zone: str, name: str) -> None:
        m = self._member_of(person)
        if m is None or self.lists is None:
            return
        for list_id, names in core.store_items(self.lists.readings, zone).items():
            text = ", ".join(names[:8]) + (f" (+{len(names) - 8})" if len(names) > 8 else "")
            await self.async_send(m["user_id"], "store", list_id, l=core.place_name(name), n=text)

    async def _place_tasks(self, person: str, zone: str, way: str) -> None:
        if self.lists is None:
            return
        local = dt_util.as_local(dt_util.utc_from_timestamp(self._now()))
        tasks = core.place_tasks(self.lists.readings, zone, way, person, local, self.data["told"])
        if not tasks:
            return
        name = core.place_name(self.zones().get(zone, zone))
        m = self._member_of(person)
        for t in tasks:
            self.data["told"].append(t["key"])
            if m is not None:
                await self.async_send(m["user_id"], "place", t["list"], n=t["summary"], l=name)
        self.data["told"] = self.data["told"][-TOLD_MAX:]
        self._save()

    # --- what the dashboard reads ----------------------------------------------------------------------------------
    def answer(self, user_id: str, admin: bool, viewer: str | None) -> dict[str, Any]:
        """``wall_dashboard/notify/prefs``: the person's choices, their phones, the lists; the members for an admin."""
        members = self.members()
        me = next((m for m in members if m["user_id"] == user_id), None)
        return {
            "user_id": user_id, "name": me["name"] if me else self.name_of(user_id), "self": user_id == viewer,
            "prefs": {k: v for k, v in self.prefs(user_id).items() if k != "tx"},
            "phones": [p["name"] for p in (me["phones"] if me else [])],
            "admin": admin,
            # everyone sees who is in the house (a ping's recipient); only an administrator opens another's choices
            "members": [{"user_id": m["user_id"], "name": m["name"], "phones": len(m["phones"])} for m in members],
        }
