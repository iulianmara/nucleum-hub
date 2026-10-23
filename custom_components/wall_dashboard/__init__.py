"""Nucleum Hub, a wall dashboard for Home Assistant: its integration (domain ``wall_dashboard``).

One package to install (D-051, version 0.2.0): adding the integration puts the dashboard in the
sidebar as the panel "Nucleum Hub", for every user, served only to signed-in users, with the weather
card's pictures (``panel.py``, docs/PACHET.md); and it converts music artwork inside the home, so a
cover that a tablet's browser cannot show (Apple TV's HEIC, for one) reaches it as a JPEG through Home
Assistant instead of from the internet (D-050: ``artwork.py``, ``websocket.py``,
docs/ARTWORK-INTEGRARE.md). The same dashboard is a Lovelace dashboard too, hidden from the sidebar, so the kiosk
apps that list only those offer it (D-087, ``dashboard.py``).

In the sale edition, the licence (D-064 to D-068, ``licence/``): started after the panel, imported lazily in the
executor and inside ``try`` (docs/LANSARE-PLAN.md §4.2), so a licence that fails, or a package without the folder
(the owner's, D-037), never takes the panel and its alarm keypad with it.

Later, each in its own module with a field in ``WallDashboardData`` (docs/LANSARE-PLAN.md §4.2,
§6.4; LANSARE-STACK.md Anexa G): the update entity and the feedback relay (docs/FEEDBACK-INTEGRARE.md).
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
import types
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_CLOSE
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.loader import async_get_integration

from . import convert, dashboard, panel, websocket
from .artwork import ArtworkService, async_register_views
from .const import (
    DATA_LICENCE_ENTRY,
    DATA_LICENCE_REGISTERED,
    DATA_REGISTERED,
    DOMAIN,
    EDITION_SALE,
    FRONTEND_BUILD_INFO,
    NAME,
    SIGNAL_LICENCE,
)
from .models import WallDashboardConfigEntry, WallDashboardData

_LOGGER = logging.getLogger(__name__)


def _build_info(folder: Path) -> dict[str, Any] | None:
    """The package's frontend/build-info.json as it is (the edition, the version, the dates), or None."""
    try:
        info = json.loads((folder / FRONTEND_BUILD_INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _startup_reads(folder: Path) -> tuple[list[str], list[str], dict[str, Any] | None]:
    """In the executor: the picture formats this Home Assistant can decode (imports Pillow and
    pillow-heif), the files missing from the package's frontend folder, and its build-info.json."""
    return convert.supported_formats(), panel.missing_files(folder), _build_info(folder)


def _import_licence() -> types.SimpleNamespace:
    """In the executor: the licence's manager and options flow (the first import reads their files)."""
    return types.SimpleNamespace(manager=importlib.import_module(f"{__name__}.licence.manager"),
                                 flow=importlib.import_module(f"{__name__}.licence.flow"))


async def async_setup_entry(hass: HomeAssistant, entry: WallDashboardConfigEntry) -> bool:
    """Set up Nucleum Hub: the panel with its views and static paths, the artwork views and the
    WebSocket command; then, in the sale edition, the licence."""
    integration = await async_get_integration(hass, DOMAIN)
    version = str(integration.version or "0.0.0")
    formats, missing, build_info = await hass.async_add_executor_job(_startup_reads, panel.FRONTEND)
    if "heic" not in formats:
        _LOGGER.warning(
            "pillow-heif could not be loaded: HEIC covers cannot be converted on this Home Assistant"
        )
    if missing:
        _LOGGER.warning(
            "The Nucleum Hub package is incomplete (frontend/%s missing): install the package again, "
            "from HACS or from its zip, not the source folder",
            ", frontend/".join(missing),
        )
    edition = build_info.get("edition") if build_info else None
    artwork = ArtworkService(hass)
    entry.runtime_data = WallDashboardData(
        version=version,
        formats=formats,
        artwork=artwork,
        dashboard=panel.DashboardFile(hass, panel.FRONTEND),
        edition=edition if isinstance(edition, str) else None,
    )

    # aiohttp's router cannot drop a view or a static path, and a WebSocket command registered twice
    # would only be replaced: all of them are registered once per Home Assistant run and answer
    # "not here" while no entry is loaded (models.async_loaded_data).
    if not hass.data.get(DATA_REGISTERED):
        hass.data[DATA_REGISTERED] = True
        async_register_views(hass)
        panel.async_register_views(hass)
        websocket.async_register_websocket(hass)
        await panel.async_register_static_paths(hass, panel.FRONTEND)

    # The sidebar's panel, on every setup, under the entry's title; it leaves with the entry (async_unload_entry).
    # Renamed in Settings › Devices & services, the sidebar follows at once (_async_entry_updated).
    tag = await hass.async_add_executor_job(panel.loader_tag)
    entry.runtime_data.module_url = panel.module_url(version, tag)
    entry.runtime_data.panel = await panel.async_register_panel(hass, version, _title(entry), tag)
    entry.runtime_data.title = _title(entry)
    # The same dashboard in the lists of Lovelace dashboards, for the kiosk apps (D-087); it leaves with the entry too.
    entry.runtime_data.lovelace = dashboard.async_register(hass, _title(entry), entry.runtime_data.module_url)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    async def _async_close(_event: Event) -> None:
        await artwork.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_CLOSE, _async_close))

    # The licence, last and apart: the panel is already there whatever happens below.
    if entry.runtime_data.edition == EDITION_SALE:
        await _async_start_licence(hass, entry.runtime_data, build_info)

    # Later features are set up here (see the module's docstring).
    return True


def _title(entry: WallDashboardConfigEntry) -> str:
    """The sidebar's title: the entry's, or the product's name when it has none."""
    return (entry.title or "").strip() or NAME


async def _async_entry_updated(hass: HomeAssistant, entry: WallDashboardConfigEntry) -> None:
    """The entry renamed (or its options changed): a new title is put on the sidebar's entry and on the Lovelace
    dashboard's, nothing else moves."""
    data = entry.runtime_data
    if data is None or not data.panel or _title(entry) == data.title:
        return
    panel.async_remove_panel(hass)
    data.panel = await panel.async_register_panel(hass, data.version, _title(entry))
    if data.lovelace:
        data.lovelace = dashboard.async_register(hass, _title(entry), data.module_url, update=True)
    data.title = _title(entry)


async def _async_start_licence(hass: HomeAssistant, data: WallDashboardData, build_info: dict[str, Any]) -> None:
    """The sale edition's licence. Its WebSocket commands are registered first and answer even when what follows
    fails (FULL, broken_environment: websocket.LICENCE_NOT_RUNNING), so the dashboard is never left guessing."""
    hass.data[DATA_LICENCE_ENTRY] = data
    if not hass.data.get(DATA_LICENCE_REGISTERED):
        hass.data[DATA_LICENCE_REGISTERED] = True
        websocket.async_register_licence_websocket(hass)
    try:
        modules = await hass.async_add_executor_job(_import_licence)
        data.licence = await modules.manager.async_start(hass, build_info=build_info)
        data.licence_flow = modules.flow.LicenceOptionsFlow
    except Exception:  # noqa: BLE001 (whatever it is, the panel stays and the dashboard stays fully usable)
        _LOGGER.exception("The Nucleum Hub licence could not start; the dashboard stays fully usable")
        data.licence = data.licence_flow = None
    # Told once the licence is in place, not by its start: a screen that stayed subscribed through a reload hears the
    # new start's state, or nothing when it is the same.
    async_dispatcher_send(hass, SIGNAL_LICENCE)


async def async_unload_entry(hass: HomeAssistant, entry: WallDashboardConfigEntry) -> bool:
    """Unload: the panel leaves the sidebar, the caches are emptied and the guarded session closed.
    The views, the static paths and the command stay registered (Home Assistant cannot remove them)
    and answer as if the integration were absent. The licence stops its timer; its stores stay."""
    data = entry.runtime_data
    if data.licence is not None:
        try:
            await data.licence.async_stop()
        except Exception:  # noqa: BLE001 (stopping must never keep the rest from unloading)
            _LOGGER.exception("The Nucleum Hub licence did not stop cleanly")
        data.licence = data.licence_flow = None
    if hass.data.get(DATA_LICENCE_ENTRY) is data:
        del hass.data[DATA_LICENCE_ENTRY]
    if data.panel:
        panel.async_remove_panel(hass)
        data.panel = False
    if data.lovelace:
        dashboard.async_remove(hass, data.module_url)
        data.lovelace = False
    data.dashboard.clear()
    await data.artwork.async_close()
    return True
