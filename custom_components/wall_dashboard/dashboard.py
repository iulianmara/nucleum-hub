"""Nucleum Hub in the lists of Lovelace dashboards as well (D-087): the kiosk apps (Kiosk Satellite, read 24.10.2026)
offer only Home Assistant's Lovelace dashboards and the add-ons' pages, never an integration's panel, so the sidebar's
"Nucleum Hub" (``panel.py``) is not in their list. This adds one more entry, with nothing for the user to set up:

- The Lovelace dashboard ``/nucleum-hub-dashboard``, under the sidebar entry's title, for every user and hidden from
  the sidebar (``show_in_sidebar`` False), so the sidebar keeps its one "Nucleum Hub".
- Read-only: mode ``yaml`` and its config fixed here (``dashboard_config``), one panel view with the card
  ``custom:nucleum-hub-card`` of ``frontend/panel.js``, which shows the same dashboard as the panel on the whole
  screen: while it is on screen, Lovelace's toolbar is hidden and Home Assistant's kiosk mode keeps the sidebar in a
  drawer, opened only by the dashboard's own menu button (the owner's choice, 24.10.2026).
- The loader is loaded on every page of the frontend (``frontend.add_extra_js_url``) at the very address the panel
  imports it from, so it is one module and the card is defined before Lovelace draws it.

Home Assistant has no public way for an integration to add a Lovelace dashboard. This does what Lovelace does for a
YAML dashboard: a ``LovelaceConfig`` of its own in ``hass.data[LOVELACE_DATA].dashboards`` (which the websocket's
``lovelace/config`` reads by its ``url_path``) and the panel registered with the component ``lovelace`` (the same in
2026.3 and 2026.10). Imported here, not at module level, and guarded: when a later Home Assistant changes them, only
this entry is missing (a warning in the log), never the sidebar's panel or the rest of the integration.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CARD_TYPE, DASHBOARD_URL_PATH, PANEL_ICON, PANEL_TITLE

_LOGGER = logging.getLogger(__name__)

LOVELACE = "lovelace"
MODE_YAML = "yaml"


def dashboard_config(title: str = PANEL_TITLE) -> dict[str, Any]:
    """The dashboard's config: one panel view (a single card on the whole view) with the card that shows Nucleum Hub."""
    return {
        "title": title,
        "views": [{"path": "home", "title": title, "icon": PANEL_ICON, "type": "panel", "cards": [{"type": CARD_TYPE}]}],
    }


_CLASS: list[type] = []


def _config_class() -> type:
    """Lovelace's own base class, with a fixed config: never saved, never deleted (a YAML dashboard's answers). Made
    once, on first use."""
    if _CLASS:
        return _CLASS[0]
    from homeassistant.components.lovelace.dashboard import LovelaceConfig  # noqa: PLC0415 (guarded, see the docstring)
    from homeassistant.helpers.json import json_bytes, json_fragment  # noqa: PLC0415

    class NucleumHubDashboard(LovelaceConfig):
        """The fixed config of ``/nucleum-hub-dashboard``."""

        def __init__(self, hass: HomeAssistant, title: str) -> None:
            super().__init__(hass, DASHBOARD_URL_PATH, {"title": title, "mode": MODE_YAML})
            self.title = title

        @property
        def mode(self) -> str:
            return MODE_YAML

        async def async_get_info(self) -> dict[str, Any]:
            return {"mode": MODE_YAML, "views": 1}

        async def async_load(self, force: bool) -> dict[str, Any]:
            return dashboard_config(self.title)

        async def async_json(self, force: bool) -> Any:
            return json_fragment(json_bytes(dashboard_config(self.title)))

    _CLASS.append(NucleumHubDashboard)
    return NucleumHubDashboard


def _dashboards(hass: HomeAssistant) -> dict[str | None, Any] | None:
    from homeassistant.components.lovelace.const import LOVELACE_DATA  # noqa: PLC0415

    data = hass.data.get(LOVELACE_DATA)
    return getattr(data, "dashboards", None)


def async_register(hass: HomeAssistant, title: str, module_url: str, *, update: bool = False) -> bool:
    """The dashboard (or, with ``update``, its new title) and the loader on every page. False when it could not be added:
    the address taken by a dashboard of the user's, or a Home Assistant whose Lovelace is not what this expects."""
    from homeassistant.components import frontend  # noqa: PLC0415

    try:
        dashboards = _dashboards(hass)
        if dashboards is None:
            raise LookupError("Lovelace keeps no dashboards")
        existing = dashboards.get(DASHBOARD_URL_PATH)
        ours = _config_class()
        if existing is not None and not isinstance(existing, ours):
            _LOGGER.warning(
                "A dashboard already uses the address /%s, so Nucleum Hub is not added to the lists of dashboards "
                "(the sidebar's Nucleum Hub is not affected); give that dashboard another address and reload Nucleum Hub",
                DASHBOARD_URL_PATH,
            )
            return False
        frontend.async_register_built_in_panel(
            hass,
            LOVELACE,
            sidebar_title=title,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=DASHBOARD_URL_PATH,
            config={"mode": MODE_YAML},
            require_admin=False,
            update=update and existing is not None,
            show_in_sidebar=False,
        )
        dashboards[DASHBOARD_URL_PATH] = ours(hass, title)
    except Exception as err:  # noqa: BLE001 (Home Assistant's internals: whatever changed, the panel stays)
        _LOGGER.warning(
            "Nucleum Hub could not be added to the lists of dashboards (%s); the sidebar's Nucleum Hub is not affected",
            err,
        )
        return False
    frontend.add_extra_js_url(hass, module_url)
    return True


def async_remove(hass: HomeAssistant, module_url: str) -> None:
    """The dashboard leaves the lists and the loader the pages (from the next page load)."""
    from homeassistant.components import frontend  # noqa: PLC0415

    try:
        frontend.remove_extra_js_url(hass, module_url)
    except (KeyError, ValueError):
        pass
    try:
        dashboards = _dashboards(hass)
        if dashboards is not None and _CLASS and isinstance(dashboards.get(DASHBOARD_URL_PATH), _CLASS[0]):
            dashboards.pop(DASHBOARD_URL_PATH, None)
            frontend.async_remove_panel(hass, DASHBOARD_URL_PATH, warn_if_unknown=False)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Nucleum Hub's dashboard entry was not removed: %s", err)
