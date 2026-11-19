"""The Lists' memory (D-104: the logic runs here, on Home Assistant's server, so a customer writes no automation), pure.

lists.py reads every to-do list's items whenever a list changes (``todo.get_items``) and hands two readings to diff():
what happened in between becomes events, and apply() keeps them:

- the history: what was added, checked, unchecked, renamed and removed, on which list, when (HISTORY_MAX entries,
  HISTORY_DAYS days);
- the removed items, with their description and due date, so the dashboard can bring one back (DELETED_DAYS days);
- the catalogue of what is bought: a product checked on a shopping list is a purchase (its name, the list, when, the
  description it had), the last BUYS_MAX purchases each;
- from the catalogue, suggestions(): "Time for milk", a product bought at least SUGGEST_MIN times whose usual interval
  (the median of the gaps) has passed and that is not on a list now.

Nothing here imports Home Assistant; tests/test_lists_core.py runs it as it is. Times are seconds (a float, the epoch).
The description's format is the one the dashboard and the owner's card write (src/modules/lists.js parseDesc).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
import statistics
from typing import Any

DAY = 86400.0
HISTORY_MAX = 500
HISTORY_DAYS = 60
DELETED_DAYS = 30
DELETED_MAX = 200
BUYS_MAX = 12
CATALOG_MAX = 400
SUGGEST_MIN = 3
# a purchase checked again within this long is the same purchase (checked, unchecked by mistake, checked again)
SAME_BUY = 6 * 3600.0
# a product is due when this share of its usual interval has passed
DUE_SHARE = 0.9
SUMMARY_MAX = 255

KINDS = ("added", "checked", "unchecked", "renamed", "removed")


def key_of(item: Mapping[str, Any]) -> str | None:
    """An item's identity across readings: its uid, else its name (a list without uids)."""
    uid = item.get("uid")
    if isinstance(uid, str) and uid:
        return "u:" + uid
    summary = item.get("summary")
    return "s:" + summary if isinstance(summary, str) and summary else None


def clean(items: Any) -> dict[str, dict[str, Any]]:
    """A reading as {key: {uid, summary, status, description, due}}; what is not an item is left out."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes, Mapping)):
        return out
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        key = key_of(raw)
        if key is None or key in out:
            continue
        due = raw.get("due")
        out[key] = {
            "uid": raw.get("uid") if isinstance(raw.get("uid"), str) else None,
            "summary": str(raw.get("summary"))[:SUMMARY_MAX],
            "status": "completed" if raw.get("status") == "completed" else "needs_action",
            "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
            "due": due if isinstance(due, str) else (due.isoformat() if hasattr(due, "isoformat") else None),
        }
    return out


def diff(before: Mapping[str, Mapping[str, Any]] | None, after: Mapping[str, Mapping[str, Any]], at: float) -> list[dict[str, Any]]:
    """What happened between two readings of one list. The first reading (before None) is no news: nothing."""
    if before is None:
        return []
    events: list[dict[str, Any]] = []

    def ev(kind: str, item: Mapping[str, Any], **extra: Any) -> None:
        events.append({"at": at, "kind": kind, "summary": item["summary"], "description": item.get("description") or "",
                       "due": item.get("due"), "uid": item.get("uid"), **extra})

    for key, item in after.items():
        old = before.get(key)
        if old is None:
            ev("added", item)
            if item["status"] == "completed":
                ev("checked", item)
            continue
        if old["summary"] != item["summary"]:
            ev("renamed", item, was=old["summary"])
        if old["status"] != item["status"]:
            ev("checked" if item["status"] == "completed" else "unchecked", item)
    for key, item in before.items():
        if key not in after:
            ev("removed", item)
    return events


# what a list is for, as the dashboard reads it (src/modules/lists.js kindOf): a shopping list by its integration or by
# its id or name in any of the 12 languages
_SHOP = re.compile(r"shop|cump[aă]r|einkauf|course|boodschap|compra|spesa|zakup|ink[öo]p|n[áa]kup|indk[øo]b|grocer|market", re.I)


def is_shop(entity_id: str, name: Any, platform: Any) -> bool:
    """Whether a list is a shopping list (its checked items are purchases)."""
    if platform == "shopping_list":
        return True
    return bool(_SHOP.search(str(entity_id or "").removeprefix("todo.") + " " + str(name or "")))


def norm(name: Any) -> str:
    """A product's name as the catalogue keys it: lower case, spaces collapsed."""
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def empty() -> dict[str, Any]:
    """The memory before anything happened."""
    return {"history": [], "deleted": [], "catalog": {}}


def valid(state: Any) -> dict[str, Any]:
    """A stored memory, or an empty one when it is not of this form (a store from another version, damaged)."""
    if not isinstance(state, Mapping):
        return empty()
    out = empty()
    if isinstance(state.get("history"), list):
        out["history"] = [h for h in state["history"] if isinstance(h, Mapping) and h.get("kind") in KINDS][-HISTORY_MAX:]
    if isinstance(state.get("deleted"), list):
        out["deleted"] = [d for d in state["deleted"] if isinstance(d, Mapping) and isinstance(d.get("summary"), str)][-DELETED_MAX:]
    if isinstance(state.get("catalog"), Mapping):
        out["catalog"] = {k: v for k, v in state["catalog"].items() if isinstance(k, str) and isinstance(v, Mapping)
                          and isinstance(v.get("buys"), list)}
    return out


def apply(state: dict[str, Any], list_id: str, shop: bool, events: list[dict[str, Any]], at: float) -> bool:
    """Keep the events of one list: the history, the removed items, the purchases (on a shopping list). True when
    anything changed (the caller saves)."""
    if not events:
        return False
    for e in events:
        state["history"].append({"at": e["at"], "list": list_id, "kind": e["kind"], "summary": e["summary"],
                                 **({"was": e["was"]} if "was" in e else {})})
        if e["kind"] == "removed":
            state["deleted"].append({"at": e["at"], "list": list_id, "summary": e["summary"],
                                     "description": e["description"], "due": e["due"]})
        if e["kind"] == "checked" and shop:
            name = norm(e["summary"])
            if not name:
                continue
            item = state["catalog"].get(name) or {"name": e["summary"], "list": list_id, "buys": [], "description": ""}
            buys = [b for b in item["buys"] if isinstance(b, (int, float))]
            count = item.get("count") if isinstance(item.get("count"), int) else len(buys)
            if not buys or e["at"] - buys[-1] > SAME_BUY:
                buys.append(e["at"])
                count += 1
            item["count"] = count
            item.update({"name": e["summary"], "list": list_id, "buys": buys[-BUYS_MAX:],
                         "description": e["description"] or item.get("description", "")})
            state["catalog"][name] = item
    prune(state, at)
    return True


def prune(state: dict[str, Any], now: float) -> None:
    """The history and the removed items within their days and counts; the catalogue within CATALOG_MAX (the products
    bought longest ago go first, a pinned one last)."""
    state["history"] = [h for h in state["history"] if now - float(h.get("at", 0)) <= HISTORY_DAYS * DAY][-HISTORY_MAX:]
    state["deleted"] = [d for d in state["deleted"] if now - float(d.get("at", 0)) <= DELETED_DAYS * DAY][-DELETED_MAX:]
    cat = state["catalog"]
    if len(cat) > CATALOG_MAX:
        last = sorted(cat, key=lambda k: (bool(cat[k].get("pin")), max(cat[k]["buys"] or [0])))
        for k in last[: len(cat) - CATALOG_MAX]:
            del cat[k]


def interval_of(buys: list[float]) -> float | None:
    """The usual time between two purchases: the median of the gaps, None with fewer than SUGGEST_MIN purchases."""
    b = sorted(x for x in buys if isinstance(x, (int, float)))
    if len(b) < SUGGEST_MIN:
        return None
    gaps = [y - x for x, y in zip(b, b[1:]) if y - x > SAME_BUY]
    return statistics.median(gaps) if len(gaps) >= SUGGEST_MIN - 1 else None


def last_buy(catalog: Mapping[str, Mapping[str, Any]]) -> float:
    """When anything was last bought (the last shopping), 0 before any."""
    return max((b for item in catalog.values() for b in item.get("buys") or [] if isinstance(b, (int, float))), default=0)


def is_snoozed(item: Mapping[str, Any], last: float) -> bool:
    """"Not now" on a suggestion or a pinned product (D-120 A): hidden until the next shopping, a purchase after it."""
    at = item.get("snooze")
    return isinstance(at, (int, float)) and at >= last


def set_snooze(catalog: dict[str, dict[str, Any]], name: Any, at: float) -> bool:
    """"Not now" on a product of the catalogue at ``at``; False when it is not in the catalogue."""
    item = catalog.get(norm(name))
    if item is None:
        return False
    item["snooze"] = at
    return True


def suggestions(catalog: Mapping[str, Mapping[str, Any]], open_names: Iterable[str], now: float,
                limit: int = 8) -> list[dict[str, Any]]:
    """What is probably needed again: due by its usual interval, not on a list now and not put off until the next
    shopping ("Not now"), the most overdue first."""
    on_list = {norm(n) for n in open_names}
    out = []
    last_all = last_buy(catalog)
    for key, item in catalog.items():
        if key in on_list or is_snoozed(item, last_all):
            continue
        gap = interval_of(item.get("buys") or [])
        if gap is None:
            continue
        last = max(item["buys"])
        since = now - last
        if since < DUE_SHARE * gap:
            continue
        out.append({"name": item.get("name") or key, "list": item.get("list"), "description": item.get("description") or "",
                    "every_days": round(gap / DAY, 1), "last": last, "overdue": round(since / gap, 2)})
    out.sort(key=lambda s: -s["overdue"])
    return out[:limit]


def catalog_view(catalog: Mapping[str, Mapping[str, Any]], open_names: Iterable[str]) -> list[dict[str, Any]]:
    """The catalogue as the dashboard shows it (D-104, mockup screen 9): each product with how many times it was bought,
    when last, its usual interval in days (None until known), pinned or not and whether it is on a list now; the most
    bought first."""
    on_list = {norm(n) for n in open_names}
    out = []
    last_all = last_buy(catalog)
    for key, item in catalog.items():
        buys = [b for b in item.get("buys") or [] if isinstance(b, (int, float))]
        if not buys:
            continue
        gap = interval_of(buys)
        count = item.get("count") if isinstance(item.get("count"), int) else len(buys)
        out.append({"name": item.get("name") or key, "list": item.get("list"), "description": item.get("description") or "",
                    "count": max(count, len(buys)), "last": max(buys), "every_days": None if gap is None else round(gap / DAY, 1),
                    "pin": bool(item.get("pin")), "on_list": key in on_list, "snoozed": is_snoozed(item, last_all)})
    out.sort(key=lambda p: (-p["count"], -p["last"]))
    return out


def set_pin(catalog: dict[str, dict[str, Any]], name: Any, pin: bool) -> bool:
    """Pin or unpin a product of the catalogue (pinned ones are one tap away on the shopping card); False when it is not
    in the catalogue."""
    item = catalog.get(norm(name))
    if item is None:
        return False
    item["pin"] = bool(pin)
    return True


def restore_data(deleted: Mapping[str, Any], features: int) -> dict[str, Any]:
    """The data of todo.add_item that brings a removed item back as it was, as far as the list allows (its
    supported_features: 16 a due date, 32 a due time, 64 a description)."""
    data: dict[str, Any] = {"item": deleted["summary"]}
    if deleted.get("description") and features & 64:
        data["description"] = deleted["description"]
    due = deleted.get("due")
    if isinstance(due, str) and due:
        if "T" in due and features & 32:
            data["due_datetime"] = due
        elif features & (16 | 32):
            data["due_date"] = due[:10]
    return data
