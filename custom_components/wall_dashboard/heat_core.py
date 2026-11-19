"""The heating stopped from a window's notification, the pure part (D-146 K2, ``heat_resume.py``): which rooms are
waiting for their windows to close, and what to turn back on once they have. Standard library only.

A pause is ``{"climate": [{"id", "mode"}], "windows": [entity_id], "text": str, "title": str, "at": float}``, keyed by
its thermostats (``key_of``). ``due`` says, for one pause and the states now, the thermostats to turn back on (each to
the mode it had, ``heat`` when that is unknown), or ``None`` while a window is still open. A thermostat turned on again
by someone else (not ``off``) is left alone.
"""

from __future__ import annotations

from typing import Any

MAX_PAUSES = 50
MAX_IDS = 20
MAX_TEXT = 300
# a pause older than this is forgotten (a window left open for a week is not waited for any more)
MAX_AGE = 7 * 86400.0
OPEN = "on"
OFF = "off"
MODES = frozenset({"heat", "heat_cool", "auto", "cool", "dry", "fan_only"})


def _id(v: Any, domain: str) -> str | None:
    return v if isinstance(v, str) and v.startswith(domain + ".") and 3 <= len(v) <= 255 else None


def clean(climate: Any, windows: Any, text: Any = "", title: Any = "", at: float = 0.0) -> dict[str, Any] | None:
    """A pause from a command's fields, or None when it names no thermostat or no window."""
    th: list[dict[str, str]] = []
    for c in climate if isinstance(climate, list) else []:
        cid = _id(c.get("id"), "climate") if isinstance(c, dict) else None
        if not cid or any(x["id"] == cid for x in th):
            continue
        mode = c.get("mode")
        th.append({"id": cid, "mode": mode if mode in MODES else "heat"})
    wins = []
    for w in windows if isinstance(windows, list) else []:
        wid = _id(w, "binary_sensor")
        if wid and wid not in wins:
            wins.append(wid)
    if not th or not wins:
        return None
    return {"climate": th[:MAX_IDS], "windows": wins[:MAX_IDS], "text": str(text or "")[:MAX_TEXT],
            "title": str(title or "")[:MAX_TEXT], "at": float(at)}


def key_of(pause: dict[str, Any]) -> str:
    return ",".join(sorted(c["id"] for c in pause["climate"]))


def valid(data: Any) -> dict[str, Any]:
    """The store as read back: anything damaged left out."""
    out: dict[str, Any] = {"pauses": {}}
    pauses = data.get("pauses") if isinstance(data, dict) else None
    for p in (pauses.values() if isinstance(pauses, dict) else []):
        if not isinstance(p, dict):
            continue
        c = clean(p.get("climate"), p.get("windows"), p.get("text"), p.get("title"), p.get("at") if isinstance(p.get("at"), (int, float)) else 0.0)
        if c:
            out["pauses"][key_of(c)] = c
    return out


def add(state: dict[str, Any], pause: dict[str, Any]) -> str:
    """Keep a pause (a newer one for the same thermostats replaces it); the oldest leave past MAX_PAUSES."""
    k = key_of(pause)
    state["pauses"][k] = pause
    while len(state["pauses"]) > MAX_PAUSES:
        oldest = min(state["pauses"], key=lambda x: state["pauses"][x]["at"])
        del state["pauses"][oldest]
    return k


def drop(state: dict[str, Any], climate_ids: list[str]) -> list[str]:
    """Forget the pauses of these thermostats (turned on from a screen): the keys removed."""
    ids = {c for c in climate_ids if isinstance(c, str)}
    gone = [k for k, p in state["pauses"].items() if ids & {c["id"] for c in p["climate"]}]
    for k in gone:
        del state["pauses"][k]
    return gone


def due(pause: dict[str, Any], states: dict[str, str | None]) -> list[tuple[str, str]] | None:
    """The thermostats to turn back on now, each with its mode, or None while a window is open (or not known yet:
    a window that is not there is taken as closed, so a removed sensor never keeps the heating off)."""
    if any(states.get(w) == OPEN for w in pause["windows"]):
        return None
    if any(states.get(w) in ("unknown", "unavailable") for w in pause["windows"]):
        return None
    return [(c["id"], c["mode"]) for c in pause["climate"] if states.get(c["id"]) == OFF]


def stale(pause: dict[str, Any], now: float) -> bool:
    return now - pause.get("at", 0.0) > MAX_AGE
