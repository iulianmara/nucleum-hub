"""Music Assistant for the dashboard through Home Assistant's own Music Assistant integration (D-178, P2-6).

The dashboard needs a few of Music Assistant's own commands that Home Assistant's actions do not give: a queue's items
("Up next", "History & queue"), a queue played from an item, the recently played, the tracks of an album or a playlist,
and the recommendations ("Picked for you", D-055). It used to open Music Assistant's socket itself, through the
Supervisor's ingress: only on Home Assistant OS or Supervised, only for the known add-on slugs, and the list of add-ons
only for an administrator. Music Assistant running anywhere else (its own Docker container, another machine) gave no
queue and no recommendations.

Home Assistant's Music Assistant integration already holds a connection to the server, wherever it runs
(``entry.runtime_data.mass``, a ``music_assistant_client.MusicAssistantClient``). The integration sends the dashboard's
commands on it: one way for every install, for any signed-in user (a wall tablet's ordinary user too), and only the
commands below (reads, and playing an item of a queue that is already there).

Pure parts (``allowed``, ``event_payload``, ``jsonable``) tested by integration/tests/test_ma_bridge.py.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

MA_DOMAIN = "music_assistant"

ALLOWED = frozenset({
    "player_queues/items",
    "player_queues/play_index",
    "music/recently_played_items",
    "music/albums/album_tracks",
    "music/playlists/playlist_tracks",
    "music/recommendations",
    "music/recommendations/items",
})
# the events that change "Picked for you" (src/modules/marec.js eventKind): nothing else is passed on
EVENTS = frozenset({"media_item_played", "playlog_updated", "recommendations_updated", "provider_event"})


def allowed(command: Any, args: Any) -> bool:
    """Whether the dashboard may send ``command`` with ``args`` (a dict of names to values, or nothing)."""
    if not isinstance(command, str) or command not in ALLOWED:
        return False
    if args is None:
        return True
    return isinstance(args, dict) and all(isinstance(k, str) and k.isidentifier() for k in args)


def jsonable(value: Any, depth: int = 0) -> Any:
    """``value`` as plain JSON data: dataclasses and Music Assistant's models (``to_dict``) as dicts, enums as their
    value; anything else that is not JSON as None."""
    if depth > 12:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value.value if isinstance(value, enum.Enum) else value
    if isinstance(value, enum.Enum):
        return jsonable(value.value, depth + 1)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return jsonable(to_dict(), depth + 1)
        except Exception:  # noqa: BLE001
            return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value), depth + 1)
    if isinstance(value, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in value]
    return None


def event_payload(event: Any) -> dict[str, Any] | None:
    """A Music Assistant event (``MassEvent``: event, object_id, data) as the dashboard reads it, or None for one it
    does not need."""
    kind = getattr(event, "event", None)
    name = str(getattr(kind, "value", kind) or "").lower()
    if name not in EVENTS:
        return None
    return {"event": name, "object_id": jsonable(getattr(event, "object_id", None)), "data": jsonable(getattr(event, "data", None))}


def client(hass: Any) -> Any:
    """The Music Assistant client of Home Assistant's loaded Music Assistant integration, or None."""
    try:
        entries = hass.config_entries.async_entries(MA_DOMAIN)
    except Exception:  # noqa: BLE001
        return None
    for entry in entries:
        if str(getattr(getattr(entry, "state", None), "value", getattr(entry, "state", ""))) != "loaded":
            continue
        mass = getattr(getattr(entry, "runtime_data", None), "mass", None)
        if mass is not None and callable(getattr(mass, "send_command", None)):
            return mass
    return None


def info(mass: Any) -> dict[str, Any]:
    """The server's version, as Music Assistant's own socket says it first (``server_version``, ``schema_version``)."""
    si = getattr(mass, "server_info", None)
    return {"server_version": jsonable(getattr(si, "server_version", None)), "schema_version": jsonable(getattr(si, "schema_version", None))}
