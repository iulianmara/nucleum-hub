"""The WebSocket commands the dashboard sends to Home Assistant (``wall_dashboard/...``).

- ``wall_dashboard/artwork/info`` (D-050), in every edition.
- ``wall_dashboard/licence`` and ``wall_dashboard/licence/subscribe``, in the sale edition only (D-067: the
  integration verifies the token and tells the dashboard the state; the dashboard never verifies). The handlers live
  here, not in licence/, and only read the entry data the licence's start left in hass.data: so a licence that could
  not start still gets an answer, FULL with the reason broken_environment (plan §4.1 p.5), never a missing command
  the dashboard would have to guess about. Any signed-in user may ask (wall tablets often run with an ordinary user,
  and every screen shows the activation screen, D-065).
- ``wall_dashboard/licence/trial``, in the sale edition only: "Start the free trial" on the activation screen (D-090).
  Only a Home Assistant admin (``require_admin``: anyone else gets the error ``unauthorized``), checked here and not
  only on the screen, since the trial is once per home and wall tablets often run with an ordinary user.
- ``wall_dashboard/ma``, ``/ma/info``, ``/ma/subscribe`` (D-178, P2-6), in every edition: a few of Music Assistant's own
  commands (ma_bridge.ALLOWED), its version and the events that change the recommendations, through Home Assistant's
  Music Assistant integration, wherever Music Assistant runs; any signed-in user. ``not_loaded`` without it.
- ``wall_dashboard/config/read`` and ``/config/write`` (D-178, P3-6), in every edition: the dashboard's configuration
  (src/modules/sync.js's containers), kept by the integration for any signed-in user (config_store.py).
- ``wall_dashboard/update/status`` (D-103), in every edition: the new version's state for the dashboard's notice and
  for the loader's look, which claims the provider's daily refresh here (updates.py; contract v2:
  docs/ACTUALIZARE-INTEGRARE.md). Only a Home Assistant admin: only an admin can install it or restart Home Assistant,
  and a wall tablet's ordinary user is shown nothing.

Later ones go here too: ``wall_dashboard/feedback/info`` and ``/send`` (docs/FEEDBACK-INTEGRARE.md), the
configuration (plan §4.8).
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import convert, ma_bridge, updates
from .config_store import ConfigError
from .const import (
    DATA_LICENCE_ENTRY,
    SIGNAL_LICENCE,
    WS_ARTWORK_INFO,
    WS_LICENCE,
    WS_LICENCE_SUBSCRIBE,
    WS_LICENCE_TRIAL,
    WS_CAMERA_PHOTOS,
    WS_CAMERA_PHOTOS_SETTINGS,
    WS_LISTS_INFO,
    WS_LISTS_PIN,
    WS_LISTS_SNOOZE,
    WS_HEAT_PAUSE,
    WS_HEAT_RESUME,
    WS_LISTS_RESTORE,
    WS_MAP_TILE,
    WS_MA,
    WS_CONFIG_READ,
    WS_CONFIG_WRITE,
    WS_MA_INFO,
    WS_MA_SUBSCRIBE,
    WS_ALBUM_CREATE,
    WS_ALBUM_LINK,
    WS_ALBUM_LIST,
    WS_ALBUM_PHOTOS,
    WS_ALBUM_REMOVE,
    WS_ALBUM_UPLOAD,
    WS_PHOTOS_CLEAR,
    WS_PHOTOS_PREFETCH,
    WS_PHOTOS_REFRESH,
    WS_PHOTOS_FEED,
    WS_PHOTOS_STORAGE,
    WS_NOTIFY_PREFS,
    WS_NOTIFY_SEND,
    WS_NOTIFY_TEST,
    WS_PLACES,
    WS_UPDATE_STATUS,
)
from .models import WallDashboardData, async_loaded_data
from .photos import PhotoError
from .tiles import TileError

_LOGGER = logging.getLogger(__name__)

# What Home Assistant answers for a command nobody registered: after the entry is removed the
# command stays registered until a restart, so it answers exactly that, and the dashboard skips
# the conversion step as it would without the integration.
ERR_UNKNOWN_COMMAND = "unknown_command"

# The licence's answer when it is not running in the sale edition (it could not start: the log says why): the
# dashboard stays fully usable. The same fields as licence/manager.py LicenceManager.payload, which a test holds.
LICENCE_NOT_RUNNING: dict[str, Any] = {
    "state": "FULL", "reason": "broken_environment", "until": None, "plan": None, "role": None, "grace": 0,
    "notice": None, "cloned": False, "lost": False, "trial_available": False, "trial_used": False,
    "trial_pending": False, "link": None, "link_kind": None,
}

# The answers of ``wall_dashboard/licence/trial`` that are not done, from the licence's own (licence/manager.py
# LicenceManager.async_start_trial): these say what the dashboard shows as they are; every other one is a request that
# could not get an answer (no connection, the server's 5xx, a timeout, Home Assistant not ready, the entry unloaded or
# reloading), which the dashboard offers to try again: "unavailable".
TRIAL_ERRORS: frozenset[str] = frozenset({"trial_used", "licensed", "rate_limited", "invalid_token", "not_available"})


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the commands of every edition, once per Home Assistant run."""
    websocket_api.async_register_command(hass, ws_artwork_info)
    websocket_api.async_register_command(hass, ws_update_status)
    websocket_api.async_register_command(hass, ws_lists_info)
    websocket_api.async_register_command(hass, ws_lists_restore)
    websocket_api.async_register_command(hass, ws_lists_pin)
    websocket_api.async_register_command(hass, ws_lists_snooze)
    websocket_api.async_register_command(hass, ws_heat_pause)
    websocket_api.async_register_command(hass, ws_heat_resume)
    websocket_api.async_register_command(hass, ws_camera_photos)
    websocket_api.async_register_command(hass, ws_camera_photos_settings)
    websocket_api.async_register_command(hass, ws_notify_prefs)
    websocket_api.async_register_command(hass, ws_notify_test)
    websocket_api.async_register_command(hass, ws_notify_send)
    websocket_api.async_register_command(hass, ws_places)
    websocket_api.async_register_command(hass, ws_map_tile)
    for command in (ws_ma, ws_ma_info, ws_ma_subscribe, ws_config_read, ws_config_write):
        websocket_api.async_register_command(hass, command)
    for command in (ws_photos_feed, ws_photos_refresh, ws_photos_prefetch, ws_photos_storage, ws_photos_clear, ws_album_list, ws_album_photos, ws_album_create, ws_album_remove, ws_album_upload, ws_album_link):
        websocket_api.async_register_command(hass, command)


@callback
def async_register_licence_websocket(hass: HomeAssistant) -> None:
    """Register the licence's commands, once per Home Assistant run, in the sale edition."""
    websocket_api.async_register_command(hass, ws_licence)
    websocket_api.async_register_command(hass, ws_licence_subscribe)
    websocket_api.async_register_command(hass, ws_licence_trial)


@websocket_api.websocket_command({vol.Required("type"): WS_ARTWORK_INFO})
@callback
def ws_artwork_info(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """``{"artwork": true, "version": "0.1.0", "max": 1024, "formats": [...]}``.

    ``formats`` are the ones this Home Assistant can really decode (normally all eight of
    ``convert.FORMATS``; AVIF needs Pillow's own AVIF support, HEIC / HEIF need pillow-heif).
    """
    data = async_loaded_data(hass)
    if data is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    connection.send_result(
        msg["id"],
        {
            "artwork": True,
            "version": data.version,
            "max": convert.MAX_SIZE,
            "formats": list(data.formats),
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_UPDATE_STATUS,
    vol.Optional("candidate"): updates.request_candidate,
    vol.Optional("claim_refresh"): bool,
})
@callback
def ws_update_status(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """``{"provider", "repository_id", "entity_id", "installed", "latest", "available", "in_progress", "percentage",
    "restart_needed", "covered", "reason", "covered_until", "edition", "pending_version", "latest_raw",
    "supports_version", "warning_accepted", "refresh_due", "refresh_claimed"}`` (updates.async_status; contract v2).

    ``candidate``: the new version as the dashboard read it from the release notes, null or exactly ``{"version",
    "released", "lts"}`` (updates.request_candidate: any other form is ``invalid_format``, before the command runs), for
    ``covered``; without it (the loader's look, or notes without the block) ``covered`` is None with the reason
    ``no_candidate``. ``claim_refresh: true``: the caller sends the provider's refresh if the answer says
    ``refresh_claimed``, and only then (one caller a day per Home Assistant run). ``warning_accepted`` is said of the
    calling user. A user who is not an admin gets the error ``unauthorized``; while no entry is loaded,
    ``unknown_command``, as Home Assistant answers without the integration."""
    data = async_loaded_data(hass)
    if data is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    user_id = getattr(connection.user, "id", None)
    status = updates.async_status(
        hass, data, msg.get("candidate"), claim_refresh=msg.get("claim_refresh") is True, user_id=user_id)
    try:
        updates.async_sync_repair(hass, status, data.licence)   # D-105: the same warning beside Settings › Updates
    except Exception:  # noqa: BLE001 (the repair is a nicety; the answer goes all the same)
        _LOGGER.exception("The repair of an uncovered Nucleum Hub version could not be updated")
    connection.send_result(msg["id"], status)


@websocket_api.websocket_command({vol.Required("type"): WS_LISTS_INFO})
@callback
def ws_lists_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """``{"history", "deleted", "suggestions"}`` of the Lists (D-104, lists.py), for any user of Home Assistant: the
    lists are theirs to read already. ``unknown_command`` while no entry is loaded, or its memory did not start."""
    data = async_loaded_data(hass)
    if data is None or data.lists is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    connection.send_result(msg["id"], data.lists.info())


@websocket_api.websocket_command({
    vol.Required("type"): WS_LISTS_RESTORE,
    vol.Required("at"): vol.Any(int, float),
    vol.Required("summary"): str,
})
@websocket_api.async_response
async def ws_lists_restore(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Bring a removed item back to its list as it was: ``{"restored": true}``, or ``not_found`` when it is no
    longer kept (30 days) or its list is gone."""
    data = async_loaded_data(hass)
    if data is None or data.lists is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    if not 0 < len(msg["summary"]) <= 255:
        connection.send_error(msg["id"], "invalid_format", "summary: 1-255 characters")
        return
    if await data.lists.async_restore(float(msg["at"]), msg["summary"]):
        connection.send_result(msg["id"], {"restored": True})
    else:
        connection.send_error(msg["id"], "not_found", "That item is no longer kept.")


@websocket_api.websocket_command({
    vol.Required("type"): WS_LISTS_PIN,
    vol.Required("name"): str,
    vol.Required("pin"): bool,
})
@callback
def ws_lists_pin(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Pin or unpin a product of the catalogue (D-104, mockup screen 9): ``{"pin": bool}``, or ``not_found`` when it
    is not in the catalogue."""
    data = async_loaded_data(hass)
    if data is None or data.lists is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    if not 0 < len(msg["name"]) <= 255:
        connection.send_error(msg["id"], "invalid_format", "name: 1-255 characters")
        return
    if data.lists.pin(msg["name"], msg["pin"]):
        connection.send_result(msg["id"], {"pin": msg["pin"]})
    else:
        connection.send_error(msg["id"], "not_found", "That product is not in the catalogue.")


@websocket_api.websocket_command({
    vol.Required("type"): WS_HEAT_PAUSE,
    vol.Required("climate"): list,
    vol.Required("windows"): list,
    vol.Optional("text"): str,
    vol.Optional("title"): str,
})
@callback
def ws_heat_pause(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The heating a screen stopped because of an open window (D-146): turned back on here once the windows close, and
    said with the screen's text; ``{"paused": true}``, ``invalid_format`` without a thermostat or a window."""
    data = async_loaded_data(hass)
    if data is None or getattr(data, "heat", None) is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    if data.heat.pause(msg["climate"], msg["windows"], msg.get("text", ""), msg.get("title", "")):
        connection.send_result(msg["id"], {"paused": True})
    else:
        connection.send_error(msg["id"], "invalid_format", "climate: [{id, mode}], windows: [binary_sensor ids]")


@websocket_api.websocket_command({vol.Required("type"): WS_HEAT_RESUME, vol.Required("climate"): list})
@callback
def ws_heat_resume(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The heating turned back on from a screen: its pause forgotten; ``{"resumed": n}``."""
    data = async_loaded_data(hass)
    if data is None or getattr(data, "heat", None) is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    connection.send_result(msg["id"], {"resumed": data.heat.resume(msg["climate"])})


@websocket_api.websocket_command({
    vol.Required("type"): WS_LISTS_SNOOZE,
    vol.Required("name"): str,
})
@callback
def ws_lists_snooze(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """"Not now" on a suggestion or a pinned product (D-120 A): hidden until the next shopping; ``{"snoozed": true}``,
    or ``not_found`` when it is not in the catalogue."""
    data = async_loaded_data(hass)
    if data is None or data.lists is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    if not 0 < len(msg["name"]) <= 255:
        connection.send_error(msg["id"], "invalid_format", "name: 1-255 characters")
        return
    if data.lists.snooze(msg["name"]):
        connection.send_result(msg["id"], {"snoozed": True})
    else:
        connection.send_error(msg["id"], "not_found", "That product is not in the catalogue.")


@websocket_api.websocket_command({
    vol.Required("type"): WS_CAMERA_PHOTOS,
    vol.Required("entity_id"): str,
    vol.Optional("after"): vol.Any(int, float),
    vol.Optional("before"): vol.Any(int, float),
})
@callback
def ws_camera_photos(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """``{"enabled", "photos": [{"id", "at", "kind"}]}``: our pictures of a camera in a window (D-106), for a user who
    may read the camera; ``unknown_command`` while no entry is loaded."""
    data = async_loaded_data(hass)
    if data is None or data.camera_photos is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    cam = msg["entity_id"]
    perms = getattr(connection.user, "permissions", None)
    if not cam.startswith("camera.") or (perms is not None and not perms.check_entity(cam, "read")):
        connection.send_error(msg["id"], "not_found", "Camera not found.")
        return
    now = time.time()
    after = float(msg.get("after", now - 86400))
    before = float(msg.get("before", now))
    connection.send_result(msg["id"], {"enabled": data.camera_photos.data["settings"]["enabled"],
                                       "photos": data.camera_photos.photos(cam, after, before)})


@websocket_api.websocket_command({
    vol.Required("type"): WS_CAMERA_PHOTOS_SETTINGS,
    vol.Optional("enabled"): bool,
    vol.Optional("limit_gb"): vol.Any(int, float),
    vol.Optional("asked"): bool,
    vol.Optional("pick"): dict,
})
@websocket_api.async_response
async def ws_camera_photos_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The pictures' settings ``{"enabled", "limit_gb", "asked", "used_bytes", "cameras", "nvr", "keep_days"}``; changing
    them (any of enabled, limit_gb, asked) is for an administrator."""
    data = async_loaded_data(hass)
    if data is None or data.camera_photos is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    change = {k: msg[k] for k in ("enabled", "limit_gb", "asked", "pick") if k in msg}
    if not change:
        connection.send_result(msg["id"], data.camera_photos.settings())
        return
    if not getattr(connection.user, "is_admin", False):
        connection.send_error(msg["id"], "unauthorized", "Only an administrator changes this.")
        return
    connection.send_result(msg["id"], await data.camera_photos.async_set(change.get("enabled"), change.get("limit_gb"), change.get("asked"), change.get("pick")))


def _notify_target(connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> tuple[str | None, str | None]:
    """Whose notifications a command is about: the user's own, or another member's for an administrator only."""
    me = getattr(connection.user, "id", None)
    other = msg.get("user_id")
    if other and other != me:
        if not getattr(connection.user, "is_admin", False):
            return None, "Only an administrator opens another member's notifications."
        return other, None
    return me, None


@websocket_api.websocket_command({
    vol.Required("type"): WS_NOTIFY_PREFS,
    vol.Optional("user_id"): str,
    vol.Optional("set"): dict,
})
@callback
def ws_notify_prefs(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A person's notifications of the Lists (D-111): ``{"user_id", "name", "self", "prefs", "phones", "admin",
    "members"}``, after ``set`` (kinds, lists, quiet, lang, tx) when given; another member's for an administrator."""
    data = async_loaded_data(hass)
    if data is None or data.notify is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    uid, err = _notify_target(connection, msg)
    if err or not uid:
        connection.send_error(msg["id"], "unauthorized", err or "No user.")
        return
    if "set" in msg:
        data.notify.set_prefs(uid, msg["set"])
    connection.send_result(msg["id"], data.notify.answer(uid, bool(getattr(connection.user, "is_admin", False)),
                                                         getattr(connection.user, "id", None)))


@websocket_api.websocket_command({vol.Required("type"): WS_NOTIFY_TEST, vol.Optional("user_id"): str})
@websocket_api.async_response
async def ws_notify_test(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """"Send a test" to a person's phones: ``{"sent": n}``, or ``no_phone`` when they have none with the app."""
    data = async_loaded_data(hass)
    if data is None or data.notify is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    uid, err = _notify_target(connection, msg)
    if err or not uid:
        connection.send_error(msg["id"], "unauthorized", err or "No user.")
        return
    n = await data.notify.async_send(uid, "test")
    if n:
        connection.send_result(msg["id"], {"sent": n})
    else:
        connection.send_error(msg["id"], "no_phone", "No phone with the Home Assistant app for this person.")


@websocket_api.websocket_command({
    vol.Required("type"): WS_NOTIFY_SEND,
    vol.Required("kind"): str,
    vol.Optional("list"): str,
    vol.Optional("list_name"): str,
    vol.Optional("to"): str,
    vol.Optional("message"): str,
    vol.Optional("taken"): int,
    vol.Optional("left"): int,
})
@websocket_api.async_response
async def ws_notify_send(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A moment told by a screen: going shopping, done shopping, a ping; ``{"sent": n}`` (the phones it went to)."""
    data = async_loaded_data(hass)
    if data is None or data.notify is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    kind, lst = msg["kind"], msg.get("list")
    if kind not in ("go", "done", "ping"):
        connection.send_error(msg["id"], "invalid_format", "kind: go, done or ping")
        return
    if lst is not None and not (lst.startswith("todo.") and len(lst) <= 255):
        connection.send_error(msg["id"], "invalid_format", "list: a todo entity")
        return
    if lst is not None:
        perms = getattr(connection.user, "permissions", None)
        if perms is not None and not perms.check_entity(lst, "read"):
            connection.send_error(msg["id"], "unauthorized", "Not allowed.")
            return
    name = (msg.get("list_name") or lst or "")[:80]
    values: dict[str, Any] = {}
    if kind == "go":
        values = {"l": name}
    elif kind == "done":
        values = {"t": max(0, msg.get("taken", 0)), "k": max(0, msg.get("left", 0))}
    else:
        text = (msg.get("message") or "").strip()
        if not 0 < len(text) <= 200:
            connection.send_error(msg["id"], "invalid_format", "message: 1-200 characters")
            return
        values = {"m": text}
    n = await data.notify.async_event(getattr(connection.user, "id", None), kind, lst, msg.get("to"), **values)
    connection.send_result(msg["id"], {"sent": n})


@websocket_api.websocket_command({vol.Required("type"): WS_PLACES, vol.Optional("set"): dict})
@callback
def ws_places(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The places of the Lists: ``{"places": {zone: {on, kind, mode, skip}}, "dwell"}``, after ``set`` when given (the
    same for the whole house: anyone may turn a store's reminder off for today)."""
    data = async_loaded_data(hass)
    if data is None or data.notify is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    if "set" in msg:
        data.notify.set_places(msg["set"])
    connection.send_result(msg["id"], data.notify.places_answer())


@websocket_api.websocket_command({vol.Required("type"): WS_MAP_TILE, vol.Required("z"): int, vol.Required("x"): int, vol.Required("y"): int})
@websocket_api.async_response
async def ws_map_tile(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """An OpenStreetMap tile of the places' map (D-116), from the integration's own copy or fetched by it:
    ``{"content_type": "image/png", "data": <base64>}``; the error's code says why not ("invalid", "busy", "unavailable")."""
    data = async_loaded_data(hass)
    if data is None or data.tiles is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    try:
        body = await data.tiles.async_tile(msg["z"], msg["x"], msg["y"])
    except TileError as err:
        connection.send_error(msg["id"], err.code, "The map tile is not available.")
        return
    connection.send_result(msg["id"], {"content_type": "image/png", "data": base64.b64encode(body).decode("ascii")})


# --- the idle screen's pictures (D-126, photos.py) ------------------------------------------------------------------
def _albums(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> Any:
    data = async_loaded_data(hass)
    keeper = getattr(data, "albums", None) if data is not None else None
    if keeper is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
    return keeper


def _first_name(connection: websocket_api.ActiveConnection) -> str:
    name = str(getattr(connection.user, "name", "") or "").strip()
    return name.split()[0] if name else ""


@websocket_api.websocket_command({vol.Required("type"): WS_PHOTOS_FEED, vol.Required("themes"): list})
@websocket_api.async_response
async def ws_photos_feed(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The free pictures of the chosen themes, ``{"day", "items": [{key, theme, src, w, h, title, author, license}]}``;
    "unavailable" while there is no list and our Worker does not answer."""
    data = async_loaded_data(hass)
    feed = getattr(data, "pictures", None) if data is not None else None
    if feed is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    try:
        connection.send_result(msg["id"], await feed.async_items([t for t in msg["themes"][:20] if isinstance(t, str)]))
    except PhotoError as err:
        connection.send_error(msg["id"], err.code, "The free pictures are not available now.")


def _pictures(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> Any:
    data = async_loaded_data(hass)
    feed = getattr(data, "pictures", None) if data is not None else None
    if feed is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
    return feed


@websocket_api.websocket_command({vol.Required("type"): WS_PHOTOS_REFRESH, vol.Required("themes"): list})
@websocket_api.async_response
async def ws_photos_refresh(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """"Actualizează acum": the list asked of the Worker at once, ``{"day", "items", "keys", "fetched_at", "total",
    "themes_n"}``; any user of the house; "unavailable" when the Worker does not answer and there is no list."""
    feed = _pictures(hass, connection, msg)
    if feed is None:
        return
    try:
        connection.send_result(msg["id"], await feed.async_refresh_now([t for t in msg["themes"][:20] if isinstance(t, str)]))
    except PhotoError as err:
        connection.send_error(msg["id"], err.code, "The free pictures are not available now.")


@websocket_api.websocket_command({vol.Required("type"): WS_PHOTOS_PREFETCH, vol.Required("keys"): list})
@websocket_api.async_response
async def ws_photos_prefetch(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A few of the keys photos/refresh gave, fetched and kept: ``{"fresh", "kept", "failed"}``; any user of the house."""
    feed = _pictures(hass, connection, msg)
    if feed is not None:
        connection.send_result(msg["id"], await feed.async_prefetch(list(msg["keys"])))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_PHOTOS_STORAGE})
@websocket_api.async_response
async def ws_photos_storage(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """What the kept free pictures take (D-150): ``{"bytes", "limit", "count"}``; a Home Assistant admin only."""
    feed = _pictures(hass, connection, msg)
    if feed is not None:
        connection.send_result(msg["id"], await feed.async_storage())


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): WS_PHOTOS_CLEAR})
@websocket_api.async_response
async def ws_photos_clear(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The kept free pictures removed (never the house's albums): ``{"cleared", "bytes", "limit", "count"}``; an admin
    only."""
    feed = _pictures(hass, connection, msg)
    if feed is not None:
        connection.send_result(msg["id"], await feed.async_clear())


@websocket_api.websocket_command({vol.Required("type"): WS_ALBUM_LIST})
@callback
def ws_album_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The house's albums ``{"albums": [{id, name, count, new, by, house}]}`` (the house's own made at the first ask)."""
    keeper = _albums(hass, connection, msg)
    if keeper is not None:
        connection.send_result(msg["id"], {"albums": keeper.list()})


@websocket_api.websocket_command({vol.Required("type"): WS_ALBUM_PHOTOS, vol.Required("album"): str})
@callback
def ws_album_photos(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """An album's photos, newest first: ``{"photos": [{id, w, h, by, at, new}]}``."""
    keeper = _albums(hass, connection, msg)
    if keeper is None:
        return
    try:
        connection.send_result(msg["id"], {"photos": keeper.photos(msg["album"])})
    except PhotoError as err:
        connection.send_error(msg["id"], err.code, "No such album.")


@websocket_api.websocket_command({vol.Required("type"): WS_ALBUM_CREATE, vol.Required("name"): str})
@callback
def ws_album_create(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """"New album": another album of the house, ``{"album": {...}}``."""
    keeper = _albums(hass, connection, msg)
    if keeper is None:
        return
    try:
        connection.send_result(msg["id"], {"album": keeper.create(msg["name"])})
    except PhotoError as err:
        connection.send_error(msg["id"], err.code, "The album could not be made.")


@websocket_api.websocket_command({vol.Required("type"): WS_ALBUM_REMOVE, vol.Required("album"): str, vol.Required("photos"): list})
@websocket_api.async_response
async def ws_album_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Photos taken out of an album (their files too): ``{"removed": n}``."""
    keeper = _albums(hass, connection, msg)
    if keeper is None:
        return
    try:
        connection.send_result(msg["id"], {"removed": await keeper.async_remove(msg["album"], [p for p in msg["photos"][:500] if isinstance(p, str)])})
    except PhotoError as err:
        connection.send_error(msg["id"], err.code, "No such album.")


@websocket_api.websocket_command({
    vol.Required("type"): WS_ALBUM_UPLOAD,
    vol.Required("album"): str,
    vol.Required("upload"): str,
    vol.Required("seq"): int,
    vol.Required("data"): str,
    vol.Optional("last"): bool,
})
@websocket_api.async_response
async def ws_album_upload(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A piece of a photo (base64, at most 512 KB once decoded): ``{"done": false}``, then with the last one
    ``{"done": true, "photo": {...}}`` once it is converted and kept."""
    keeper = _albums(hass, connection, msg)
    if keeper is None:
        return
    user = str(getattr(connection.user, "id", "") or "")
    if not re.fullmatch(r"[a-z0-9]{8,32}", msg["upload"]) or not 0 <= msg["seq"] <= 200:
        connection.send_error(msg["id"], "invalid", "Not a piece of a photo.")
        return
    try:
        row = await keeper.async_chunk(user, msg["upload"], msg["album"], msg["seq"], msg["data"], bool(msg.get("last")), _first_name(connection))
    except PhotoError as err:
        connection.send_error(msg["id"], err.code, "The photo could not be kept.")
        return
    connection.send_result(msg["id"], {"done": row is not None, "photo": row})


@websocket_api.websocket_command({vol.Required("type"): WS_ALBUM_LINK, vol.Required("album"): str})
@callback
def ws_album_link(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The QR code's link for an album: ``{"url", "expires", "minutes"}``, Home Assistant's address inside the home; a
    new one ends the album's earlier one. "unavailable" when Home Assistant knows no address of its own in the home."""
    keeper = _albums(hass, connection, msg)
    if keeper is None:
        return
    try:
        connection.send_result(msg["id"], keeper.link(msg["album"]))
    except PhotoError as err:
        connection.send_error(msg["id"], err.code, "No link now.")


def _sale(hass: HomeAssistant) -> WallDashboardData | None:
    """The sale edition's entry data from the start of its licence until it unloads (const.DATA_LICENCE_ENTRY), or
    None: the owner's edition, no entry, or one being reloaded (the screen then keeps what it was told last)."""
    data = hass.data.get(DATA_LICENCE_ENTRY)
    return data if isinstance(data, WallDashboardData) else None


def licence_payload(data: WallDashboardData, language: Any = None) -> dict[str, Any]:
    """The licence's state for the dashboard (licence/manager.py LicenceManager.payload), or LICENCE_NOT_RUNNING when
    the licence is not running or could not answer (never an error the dashboard would have to guess about)."""
    if data.licence is not None:
        try:
            return data.licence.payload(language)
        except Exception:  # noqa: BLE001 (the dashboard stays fully usable, plan §4.1 p.5; the log says why)
            _LOGGER.exception("The Nucleum Hub licence could not say its state")
    return {**LICENCE_NOT_RUNNING, "now": int(time.time())}


@websocket_api.websocket_command(
    {vol.Required("type"): WS_LICENCE, vol.Optional("language"): str, vol.Optional("refresh"): bool}
)
@callback
def ws_licence(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """``{"state", "reason", "until", "plan", "role", "grace", "notice", "cloned", "lost", "trial_available",
    "trial_used", "trial_pending", "now", "link", "link_kind"}``.

    ``language``: the dashboard's, for the link. ``refresh: true``: an admin opened the dashboard's settings, so the
    licence bound here is checked again in the background (within its budget, server/README.md §10.9; never the
    trial, D-090); the answer is the state as it is, and the subscription brings the new one. From a user who is not
    an admin it is ignored.
    """
    data = _sale(hass)
    if data is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    if msg.get("refresh") and data.licence is not None and getattr(connection.user, "is_admin", False):
        data.licence.async_request_check()
    connection.send_result(msg["id"], licence_payload(data, msg.get("language")))


@websocket_api.websocket_command({vol.Required("type"): WS_LICENCE_SUBSCRIBE, vol.Optional("language"): str})
@callback
def ws_licence_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The state at once, as an event, then again at every change (the same fields as ``wall_dashboard/licence``),
    until the dashboard unsubscribes or the connection closes. It lasts through a reload of the entry: the licence
    started again says its state, and what it says is sent when it differs from what this subscription sent last
    (``now`` aside)."""
    if _sale(hass) is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    language = msg.get("language")
    last: list[Any] = [None]

    @callback
    def send() -> None:
        data = _sale(hass)
        if data is None:
            return  # unloaded: the screen keeps what it was told last
        payload = licence_payload(data, language)
        said = {key: value for key, value in payload.items() if key != "now"}
        if said != last[0]:
            last[0] = said
            connection.send_message(websocket_api.event_message(msg["id"], payload))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_LICENCE, send)
    connection.send_result(msg["id"])
    send()


def trial_answer(error: str | None, minutes: int | None = None) -> dict[str, Any]:
    """The result of ``wall_dashboard/licence/trial`` for the licence's outcome (its error code, None when done)."""
    if error is None:
        return {"ok": True}
    code = error if error in TRIAL_ERRORS else "unavailable"
    return {"ok": False, "error": code, "minutes": minutes if code == "rate_limited" else None}


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): WS_LICENCE_TRIAL, vol.Optional("language"): str,
    vol.Optional("email"): str, vol.Optional("news"): bool, vol.Optional("consent_version"): str,
})
@websocket_api.async_response
async def ws_licence_trial(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """``{"ok": true}`` once the trial's token is held (the subscriptions then bring TRIAL), or ``{"ok": false,
    "error", "minutes"}`` (TRIAL_ERRORS, trial_answer). The licence asks the server once; a press while that request is
    on its way gets the same answer. Waits for the answer (up to the request's timeout, plus a request ahead of it).

    The dashboard sends ``{"type"}``, and from 1.0.2 the optional e-mail of D-095: ``email``, ``news`` (the box, false
    when not ticked), ``consent_version`` (its text's version) and ``language`` (the e-mail's language); the licence passes
    on only what the server takes (licence/api.py trial_contact), so none of it can stop the trial. ``language`` was
    taken before too, as the two other commands take it: Home Assistant answers a key the schema does not name with ``invalid_format`` before the command
    (and require_admin) runs, and a schema of ``type`` alone takes no other key at all, so a dashboard that sent its
    language (as one build of 26.10 did) would never start the trial and would never even hear "admins only".

    While the entry is unloaded or reloading, the press is answered ``unavailable`` (the screen offers to try again),
    never ``unknown_command``: this command being registered already says the integration has the trial, and the
    dashboard takes ``unknown_command`` for an older integration and drops the trial's card for the connection."""
    data = _sale(hass)
    if data is None:
        connection.send_result(msg["id"], trial_answer("unavailable"))
        return
    if data.licence is None:  # the licence could not start: the dashboard stays fully usable (FULL)
        connection.send_result(msg["id"], trial_answer("not_available"))
        return
    try:
        outcome = await data.licence.async_start_trial({
            "email": msg.get("email"), "lang": msg.get("language"), "news": msg.get("news", False),
            "consent_version": msg.get("consent_version"),
        })
    except Exception:  # noqa: BLE001 (a mistake of ours: the dashboard is answered, and offers to try again)
        _LOGGER.exception("The Nucleum Hub licence could not ask for the trial")
        connection.send_result(msg["id"], trial_answer("unknown"))
        return
    connection.send_result(msg["id"], trial_answer(outcome.error, outcome.minutes))


def _ma(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> Any:
    """The Music Assistant client, or None after the error was sent (our entry not loaded: unknown_command; no Music
    Assistant integration loaded: not_loaded)."""
    if async_loaded_data(hass) is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return None
    mass = ma_bridge.client(hass)
    if mass is None:
        connection.send_error(msg["id"], "not_loaded", "Music Assistant is not set up in Home Assistant.")
    return mass


@websocket_api.websocket_command({vol.Required("type"): WS_MA, vol.Required("command"): str, vol.Optional("args"): dict})
@websocket_api.async_response
async def ws_ma(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """One of Music Assistant's own commands (ma_bridge.ALLOWED), its result as Music Assistant gives it."""
    if not ma_bridge.allowed(msg["command"], msg.get("args")):
        connection.send_error(msg["id"], "not_allowed", "This Music Assistant command is not available.")
        return
    mass = _ma(hass, connection, msg)
    if mass is None:
        return
    try:
        result = await mass.send_command(msg["command"], **(msg.get("args") or {}))
    except Exception as err:  # noqa: BLE001 (Music Assistant's own errors: an unknown command on an older server, a provider's)
        connection.send_error(msg["id"], "ma_error", str(err) or type(err).__name__)
        return
    connection.send_result(msg["id"], ma_bridge.jsonable(result))


@websocket_api.websocket_command({vol.Required("type"): WS_MA_INFO})
@callback
def ws_ma_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """``{"server_version", "schema_version"}`` of the Music Assistant server."""
    mass = _ma(hass, connection, msg)
    if mass is not None:
        connection.send_result(msg["id"], ma_bridge.info(mass))


@websocket_api.websocket_command({vol.Required("type"): WS_MA_SUBSCRIBE})
@callback
def ws_ma_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """The events that change the recommendations (ma_bridge.EVENTS), ``{event, object_id, data}``, until the dashboard
    unsubscribes or the connection closes."""
    mass = _ma(hass, connection, msg)
    if mass is None:
        return

    def send(event: Any) -> None:
        payload = ma_bridge.event_payload(event)
        if payload is not None:
            connection.send_message(websocket_api.event_message(msg["id"], payload))  # Music Assistant calls back on the event loop

    try:
        unsub = mass.subscribe(send)
    except Exception:  # noqa: BLE001
        connection.send_error(msg["id"], "not_loaded", "Music Assistant does not answer.")
        return
    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): WS_CONFIG_READ, vol.Required("path"): str})
@websocket_api.async_response
async def ws_config_read(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A container of the dashboard's configuration, or None (taken from its Lovelace dashboard the first time)."""
    data = async_loaded_data(hass)
    if data is None or data.config is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    try:
        connection.send_result(msg["id"], await data.config.async_read(msg["path"]))
    except ConfigError as err:
        connection.send_error(msg["id"], err.code, "The configuration cannot be read.")


@websocket_api.websocket_command({vol.Required("type"): WS_CONFIG_WRITE, vol.Required("path"): str, vol.Required("config"): dict})
@websocket_api.async_response
async def ws_config_write(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A whole container, as the dashboard sends it; any signed-in user (a wall tablet's ordinary user too)."""
    data = async_loaded_data(hass)
    if data is None or data.config is None:
        connection.send_error(msg["id"], ERR_UNKNOWN_COMMAND, "Unknown command.")
        return
    try:
        await data.config.async_write(msg["path"], msg["config"])
    except ConfigError as err:
        connection.send_error(msg["id"], err.code, "The configuration cannot be saved.")
        return
    connection.send_result(msg["id"])
