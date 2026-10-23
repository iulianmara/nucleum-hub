"""The WebSocket commands the dashboard sends to Home Assistant (``wall_dashboard/...``).

- ``wall_dashboard/artwork/info`` (D-050), in every edition.
- ``wall_dashboard/licence`` and ``wall_dashboard/licence/subscribe``, in the sale edition only (D-067: the
  integration verifies the token and tells the dashboard the state; the dashboard never verifies). The handlers live
  here, not in licence/, and only read the entry data the licence's start left in hass.data: so a licence that could
  not start still gets an answer, FULL with the reason broken_environment (plan §4.1 p.5), never a missing command
  the dashboard would have to guess about.

Any signed-in user may ask (wall tablets often run with an ordinary user, and every screen shows the activation
screen, D-065). Later ones go here too: ``wall_dashboard/feedback/info`` and ``/send``
(docs/FEEDBACK-INTEGRARE.md), the configuration (plan §4.8).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from . import convert
from .const import DATA_LICENCE_ENTRY, SIGNAL_LICENCE, WS_ARTWORK_INFO, WS_LICENCE, WS_LICENCE_SUBSCRIBE
from .models import WallDashboardData, async_loaded_data

_LOGGER = logging.getLogger(__name__)

# What Home Assistant answers for a command nobody registered: after the entry is removed the
# command stays registered until a restart, so it answers exactly that, and the dashboard skips
# the conversion step as it would without the integration.
ERR_UNKNOWN_COMMAND = "unknown_command"

# The licence's answer when it is not running in the sale edition (it could not start: the log says why): the
# dashboard stays fully usable. The same fields as licence/manager.py LicenceManager.payload, which a test holds.
LICENCE_NOT_RUNNING: dict[str, Any] = {
    "state": "FULL", "reason": "broken_environment", "until": None, "plan": None, "role": None, "grace": 0,
    "notice": None, "cloned": False, "lost": False, "trial_pending": False, "link": None, "link_kind": None,
}


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the commands of every edition, once per Home Assistant run."""
    websocket_api.async_register_command(hass, ws_artwork_info)


@callback
def async_register_licence_websocket(hass: HomeAssistant) -> None:
    """Register the licence's commands, once per Home Assistant run, in the sale edition."""
    websocket_api.async_register_command(hass, ws_licence)
    websocket_api.async_register_command(hass, ws_licence_subscribe)


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
    """``{"state", "reason", "until", "plan", "role", "grace", "notice", "cloned", "lost", "trial_pending", "now",
    "link", "link_kind"}``.

    ``language``: the dashboard's, for the link. ``refresh: true``: an admin opened the dashboard's settings, so the
    licence asks the server again in the background (within its budget, server/README.md §10.9); the answer is the
    state as it is, and the subscription brings the new one. From a user who is not an admin it is ignored.
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
