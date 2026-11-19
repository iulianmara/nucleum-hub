"""What a loaded Nucleum Hub entry keeps (``entry.runtime_data``), and how the views find it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

if TYPE_CHECKING:
    from .artwork import ArtworkService
    from .panel import DashboardFile


@dataclass
class WallDashboardData:
    """The runtime state of the one Nucleum Hub entry.

    Later features get their own field here (plan §4.2): the update entity, the feedback queue
    (docs/FEEDBACK-INTEGRARE.md).
    """

    version: str  # the integration's version (manifest.json), told to the dashboard
    formats: list[str]  # the picture formats this Home Assistant can decode
    artwork: ArtworkService
    dashboard: DashboardFile  # the dashboard's file of the package (D-051)
    panel: bool = False  # True once the sidebar panel is registered (removed again on unload)
    title: str = ""  # the title the sidebar's entry shows (the config entry's; __init__.py _async_entry_updated)
    module_url: str = ""  # the loader's address, the panel's module and the extra module of every page (dashboard.py)
    lovelace: bool = False  # True once the Lovelace dashboard is registered (dashboard.py; removed again on unload)
    edition: str | None = None  # the packaged dashboard's edition (frontend/build-info.json), None without one
    # The licence (sale edition only, D-037): its manager (licence/manager.py) and the options flow's class
    # (licence/flow.py), both None when the edition has none or it could not start. Typed loosely so that nothing
    # here imports the licence (plan §4.2).
    licence: Any = None
    licence_flow: Any = None
    # The Lists' memory (D-104, lists.py): the history, the removed items and the purchases of every to-do list
    lists: Any = None
    # the dashboard's configuration, for every screen (D-178, config_store.py)
    config: Any = None
    camera_photos: Any = None
    # The Lists' notifications on the phones (D-111, notify_lists.py)
    notify: Any = None
    # The places' map (D-116, tiles.py): OpenStreetMap's tiles, fetched and kept 6 months from their last use
    tiles: Any = None
    # The idle screen's pictures (D-126, photos.py): the free pictures' list and cache, the house's albums
    pictures: Any = None
    albums: Any = None
    # The heating stopped from a window's notification (D-146, heat_resume.py): back on once the windows close
    heat: Any = None


WallDashboardConfigEntry = ConfigEntry[WallDashboardData]


@callback
def async_loaded_data(hass: HomeAssistant) -> WallDashboardData | None:
    """The data of the loaded entry, or None when the integration is not set up (or was removed:
    the views and the WebSocket command stay registered until Home Assistant restarts)."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and isinstance(
            getattr(entry, "runtime_data", None), WallDashboardData
        ):
            return entry.runtime_data
    return None
