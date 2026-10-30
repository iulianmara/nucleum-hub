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

Later ones go here too: ``wall_dashboard/feedback/info`` and ``/send`` (docs/FEEDBACK-INTEGRARE.md), the
configuration (plan §4.8).
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
from .const import (
    DATA_LICENCE_ENTRY,
    SIGNAL_LICENCE,
    WS_ARTWORK_INFO,
    WS_LICENCE,
    WS_LICENCE_SUBSCRIBE,
    WS_LICENCE_TRIAL,
)
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
