"""The Lists' notifications (D-111), the pure part: each person's choices, the quiet hours, when a task is due, the
texts the dashboard hands over in the screen's language, the phones of a user. No Home Assistant here; tested by
tests/test_notify_core.py. The runtime is notify_lists.py."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from typing import Any

# every kind a person turns on or off (the rows of "My notifications", in the mockup's order)
KINDS = ("store", "due", "place", "go", "done", "ping", "digest")
DEFAULT_KINDS = {"store": True, "due": True, "place": True, "go": True, "done": False, "ping": True, "digest": False}
DEFAULT_QUIET = {"on": True, "from": "22:30", "to": "06:45"}
# the texts the dashboard hands over (its catalogue T, the screen's language); {n} {l} {who} {m} {t} {k} as below
TEXTS = {
    "title": (),            # the notifications' title, "Lists"
    "due": ("n",),          # a task due: {n} the task
    "go": ("who", "l"),     # {who} goes shopping with {l}
    "done": ("who", "t", "k"),  # {who} is done: {t} taken, {k} left
    "ping": ("who", "m"),   # {who}: {m}
    "digest": ("l", "n"),   # on {l}: {n} (the items added)
    "test": (),             # the test
    "store": ("l", "n"),    # at the store {l}: {n} (what is to buy there)
    "place": ("n", "l"),    # a task {n} at a place {l} (arriving or leaving)
}
TEXT_MAX = 160
MESSAGE_MAX = 200
DUE_HOUR = 9          # a task due on a day, without an hour: told at 09:00 that day
DUE_LATE = 15 * 60    # a moment missed by more than this (Home Assistant was off) is not told any more
DIGEST_AFTER = 15 * 60
DWELL = 4 * 60        # "In the store": told after this long inside the zone, not when passing by
REM_MAX = 7 * 24 * 60
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_LIST = re.compile(r"^todo\.[a-z0-9_]+$")


def default_prefs() -> dict[str, Any]:
    return {"kinds": dict(DEFAULT_KINDS), "lists": {}, "quiet": dict(DEFAULT_QUIET), "lang": "en", "tx": {}}


def valid_texts(tx: Any) -> dict[str, str]:
    """The texts the dashboard sent, kept only when each is a short string with exactly its own placeholders."""
    out: dict[str, str] = {}
    if not isinstance(tx, Mapping):
        return out
    for key, names in TEXTS.items():
        s = tx.get(key)
        if not isinstance(s, str) or not 0 < len(s) <= TEXT_MAX:
            continue
        if sorted(re.findall(r"\{(\w+)\}", s)) != sorted(names):
            continue
        out[key] = s
    return out


def merge_prefs(old: Any, patch: Any) -> dict[str, Any]:
    """A person's choices after a change from the dashboard; what is not valid is left as it was."""
    p = valid_prefs(old)
    if not isinstance(patch, Mapping):
        return p
    kinds = patch.get("kinds")
    if isinstance(kinds, Mapping):
        for k, v in kinds.items():
            if k in KINDS and isinstance(v, bool):
                p["kinds"][k] = v
    lists = patch.get("lists")
    if isinstance(lists, Mapping):
        for k, v in lists.items():
            if isinstance(k, str) and _LIST.match(k) and isinstance(v, bool):
                p["lists"][k] = v
    quiet = patch.get("quiet")
    if isinstance(quiet, Mapping):
        if isinstance(quiet.get("on"), bool):
            p["quiet"]["on"] = quiet["on"]
        for k in ("from", "to"):
            if isinstance(quiet.get(k), str) and _HHMM.match(quiet[k]):
                p["quiet"][k] = quiet[k]
    lang = patch.get("lang")
    if isinstance(lang, str) and re.match(r"^[a-z]{2}(-[A-Za-z]{2})?$", lang):
        p["lang"] = lang
    if "tx" in patch:
        tx = valid_texts(patch.get("tx"))
        if tx:
            p["tx"] = tx
    return p


def valid_prefs(p: Any) -> dict[str, Any]:
    """Stored choices read back; a damaged part falls back to the default."""
    out = default_prefs()
    if not isinstance(p, Mapping):
        return out
    for k, v in (p.get("kinds") or {}).items() if isinstance(p.get("kinds"), Mapping) else ():
        if k in KINDS and isinstance(v, bool):
            out["kinds"][k] = v
    if isinstance(p.get("lists"), Mapping):
        out["lists"] = {k: v for k, v in p["lists"].items() if isinstance(k, str) and _LIST.match(k) and isinstance(v, bool)}
    q = p.get("quiet")
    if isinstance(q, Mapping):
        if isinstance(q.get("on"), bool):
            out["quiet"]["on"] = q["on"]
        for k in ("from", "to"):
            if isinstance(q.get(k), str) and _HHMM.match(q[k]):
                out["quiet"][k] = q[k]
    if isinstance(p.get("lang"), str):
        out["lang"] = p["lang"][:8]
    out["tx"] = valid_texts(p.get("tx"))
    return out


def wants(prefs: Mapping[str, Any], kind: str, list_id: str | None = None) -> bool:
    """Whether a person gets this kind (from this list: a list not chosen yet counts as chosen)."""
    if not prefs["kinds"].get(kind):
        return False
    return list_id is None or prefs["lists"].get(list_id, True)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def quiet_now(prefs: Mapping[str, Any], local: dt.datetime) -> bool:
    """Whether the person's quiet hours hold at this local moment (from 22:30 to 06:45 crosses midnight)."""
    q = prefs["quiet"]
    if not q.get("on"):
        return False
    a, b, m = _minutes(q["from"]), _minutes(q["to"]), local.hour * 60 + local.minute
    if a == b:
        return False
    return a <= m < b if a < b else m >= a or m < b


def may_send(prefs: Mapping[str, Any], kind: str, local: dt.datetime) -> bool:
    """Only "Ping urgent" and the test pass through the quiet hours."""
    return kind in ("ping", "test") or not quiet_now(prefs, local)


def _tokens(description: Any) -> list[str]:
    """The words of an item's description before its note ("nota=" runs to the end, src/modules/lists.js parseDesc)."""
    if not isinstance(description, str):
        return []
    return description.split(" nota=")[0].split("nota=")[0].split()


def no_reminder(description: Any) -> bool:
    """"rem=no": the task sheet's "None", its moment told to no one."""
    return "rem=no" in _tokens(description)


def remind_before(description: Any) -> int:
    """"rem=60": the task is told this many minutes before its moment (the task sheet's "1 h before")."""
    for tk in _tokens(description):
        if tk.startswith("rem=") and tk[4:].isdigit():
            return min(int(tk[4:]), REM_MAX)
    return 0


def task_place(description: Any) -> tuple[str, str] | None:
    """"loc=zone.work la=out": the place a task is told at and when, "in" (arriving, the default) or "out" (leaving)."""
    zone, way = None, "in"
    for tk in _tokens(description):
        if tk.startswith("loc=") and re.match(r"^zone\.[a-z0-9_]+$", tk[4:]):
            zone = tk[4:]
        elif tk in ("la=in", "la=out"):
            way = tk[3:]
    return (zone, way) if zone else None


def stores_of(description: Any) -> list[str]:
    """"mag=zone.a,zone.b": the stores an item is bought at."""
    for tk in _tokens(description):
        if tk.startswith("mag="):
            return [z for z in tk[4:].split(",") if re.match(r"^zone\.[a-z0-9_]+$", z)]
    return []


def place_name(name: Any) -> str:
    """A zone's name without its "Store: " prefix, as the dashboard shows it."""
    return re.sub(r"^[^:]{1,24}:\s*", "", str(name or "")) or str(name or "")


def default_place(name: Any) -> dict[str, Any]:
    """A zone's reminder settings before anyone changed them: on; a store when its name has a "Store: " prefix."""
    store = bool(re.match(r"^[^:]{1,24}:\s*\S", str(name or "")))
    return {"on": True, "kind": "store" if store else "place", "mode": "pass", "skip": ""}


def valid_place(p: Any, name: Any) -> dict[str, Any]:
    out = default_place(name)
    if not isinstance(p, Mapping):
        return out
    if isinstance(p.get("on"), bool):
        out["on"] = p["on"]
    if p.get("kind") in ("store", "place"):
        out["kind"] = p["kind"]
    if p.get("mode") in ("pass", "in"):
        out["mode"] = p["mode"]
    if isinstance(p.get("skip"), str) and re.match(r"^(\d{4}-\d{2}-\d{2})?$", p["skip"]):
        out["skip"] = p["skip"]
    return out


def zone_of(state: Any, zones: Mapping[str, Any]) -> str | None:
    """The zone a person's state names: "home" is zone.home, "not_home" none, else the zone with that name."""
    if not isinstance(state, str) or state in ("not_home", "unknown", "unavailable", ""):
        return None
    if state == "home":
        return "zone.home"
    for eid, name in zones.items():
        if name == state:
            return eid
    return None


def store_items(readings: Mapping[str, Mapping[str, Mapping[str, Any]]], zone: str) -> dict[str, list[str]]:
    """What is still to buy at a store: {list: [summary, ...]}."""
    out: dict[str, list[str]] = {}
    for list_id, items in readings.items():
        for item in items.values():
            if item.get("status") != "completed" and zone in stores_of(item.get("description")):
                out.setdefault(list_id, []).append(str(item.get("summary") or ""))
    return out


def place_tasks(readings: Mapping[str, Mapping[str, Mapping[str, Any]]], zone: str, way: str, person: str,
                local_now: dt.datetime, told: Iterable[str]) -> list[dict[str, Any]]:
    """The tasks told when ``person`` arrives at (``in``) or leaves (``out``) ``zone``, today, not told yet: a task for
    another person is not; a task due on a later day waits for its day. [{key, list, summary, person}]"""
    seen, out, today = set(told), [], local_now.date().isoformat()
    for list_id, items in readings.items():
        for item in items.values():
            if item.get("status") == "completed":
                continue
            tp = task_place(item.get("description"))
            if not tp or tp != (zone, way):
                continue
            who = person_of(item.get("description"))
            if who and who != person:
                continue
            due = item.get("due")
            if isinstance(due, str) and len(due) >= 10 and due[:10] > today:
                continue
            key = f"place|{list_id}|{item.get('uid') or item.get('summary')}|{today}|{way}"
            if key in seen:
                continue
            out.append({"key": key, "list": list_id, "summary": item.get("summary") or "", "person": who or person})
    return out


def due_at(due: Any, local_now: dt.datetime, before: int = 0) -> float | None:
    """The moment (unix seconds) a task's due is told: its own time, or 09:00 of its day in the local zone; ``before``
    minutes earlier ("rem=")."""
    at = _due_at(due, local_now)
    return None if at is None else at - before * 60


def _due_at(due: Any, local_now: dt.datetime) -> float | None:
    if not isinstance(due, str) or not due:
        return None
    try:
        if len(due) == 10:
            d = dt.date.fromisoformat(due)
            return dt.datetime(d.year, d.month, d.day, DUE_HOUR, tzinfo=local_now.tzinfo).timestamp()
        moment = dt.datetime.fromisoformat(due.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=local_now.tzinfo)
    return moment.timestamp()


def person_of(description: Any) -> str | None:
    """The person a task is for, "pers=person.ana" in its description (src/modules/lists.js rowMeta)."""
    if not isinstance(description, str):
        return None
    for tk in _tokens(description):
        if re.match(r"^pers=person\.[a-z0-9_]+$", tk):
            return tk[5:]
    return None


def due_now(readings: Mapping[str, Mapping[str, Mapping[str, Any]]], local_now: dt.datetime,
            told: Iterable[str]) -> list[dict[str, Any]]:
    """The tasks whose moment has come and is not older than DUE_LATE, not told yet: [{key, list, summary, person}]."""
    now, seen, out = local_now.timestamp(), set(told), []
    for list_id, items in readings.items():
        for item in items.values():
            if item.get("status") == "completed" or no_reminder(item.get("description")):
                continue
            at = due_at(item.get("due"), local_now, remind_before(item.get("description")))
            if at is None or not at <= now < at + DUE_LATE:
                continue
            key = f"{list_id}|{item.get('uid') or item.get('summary')}|{item.get('due')}"
            if key in seen:
                continue
            out.append({"key": key, "list": list_id, "summary": item.get("summary") or "", "person": person_of(item.get("description"))})
    return out


def next_due(readings: Mapping[str, Mapping[str, Mapping[str, Any]]], local_now: dt.datetime,
              told: Iterable[str]) -> float | None:
    """The next moment a task is told: now when one is due already, else the earliest one to come; None for none."""
    if due_now(readings, local_now, told):
        return local_now.timestamp()
    now, best = local_now.timestamp(), None
    for items in readings.values():
        for item in items.values():
            skip = item.get("status") == "completed" or no_reminder(item.get("description"))
            at = None if skip else due_at(item.get("due"), local_now, remind_before(item.get("description")))
            if at is not None and at > now and (best is None or at < best):
                best = at
    return best


def fill(prefs: Mapping[str, Any], key: str, **values: Any) -> str:
    """A text in the person's language, its placeholders filled; English when the dashboard sent none yet."""
    s = prefs["tx"].get(key) or EN[key]
    for k, v in values.items():
        s = s.replace("{" + k + "}", str(v)[:MESSAGE_MAX])
    return s


# what is sent until a dashboard has handed over its texts (it does at its first visit to "My notifications")
EN = {
    "title": "Lists",
    "due": "Due now: {n}",
    "go": "{who} is going shopping ({l})",
    "done": "{who} is done shopping: {t} taken, {k} left",
    "ping": "{who}: {m}",
    "digest": "On {l}: {n}",
    "test": "Nucleum Hub notifications work on this phone.",
    "store": "At {l}: {n}",
    "place": "{n} ({l})",
}


def slug(name: Any) -> str:
    """homeassistant.util.slugify as the mobile app's notify service is named: notify.mobile_app_<slug>."""
    s = str(name or "").lower()
    for a, b in (("ă", "a"), ("â", "a"), ("î", "i"), ("ș", "s"), ("ş", "s"), ("ț", "t"), ("ţ", "t"), ("ä", "a"), ("ö", "o"),
                 ("ü", "u"), ("ß", "ss"), ("é", "e"), ("è", "e"), ("à", "a"), ("ç", "c"), ("ñ", "n"), ("ø", "o"), ("å", "a")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def phones_of(entries: Iterable[Any], user_id: str) -> list[dict[str, str]]:
    """The phones of a user, from the mobile app's config entries: [{name, service}]."""
    out = []
    for e in entries:
        data = getattr(e, "data", None) or {}
        if data.get("user_id") == user_id and data.get("device_name"):
            out.append({"name": str(data["device_name"]), "service": "mobile_app_" + slug(data["device_name"])})
    return out
