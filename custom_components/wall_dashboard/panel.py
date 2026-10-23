"""The dashboard in Home Assistant's sidebar (D-051): one package to install, no file to copy by hand.

- The panel "Nucleum Hub" (``/nucleum-hub``), on every screen, for every user. Its custom element,
  ``nucleum-hub-panel``, comes from ``frontend/panel.js``, the loader: it asks ``/api/wall_dashboard/app``
  with the user's token and shows the dashboard in a full-size frame of the same origin, so the
  dashboard finds Home Assistant around it exactly as it does today from ``/local``.
- Two public static paths (nothing secret in them): ``/nucleum_hub/panel.js`` (cached; the panel asks
  it as ``?v=<version>``, so a new version is a new address) and ``/nucleum_hub/wall-dashboard-weather/``
  (the weather card's landscape pictures, cached).
- Two authenticated views: ``/api/wall_dashboard/app``, the dashboard's file exactly as it was built
  (nothing injected, ``no-store``, its sha256 in ``X-Nucleum-Hub-Sha256``), and
  ``/api/wall_dashboard/info``, the integration's version, the build of the dashboard it carries
  (``frontend/build-info.json``) and the sha256 of the file the first view serves now (``file``): a
  screen left open after an update compares them with what it runs (D-054).

tools/package.mjs puts the dashboard's file, its build-info.json and the pictures into ``frontend/``;
the committed folder has only the loader (``frontend/README.txt``). The contract with the loader and
how the package is made: docs/PACHET.md.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
from pathlib import Path
import stat
from typing import Any

from aiohttp import hdrs, web

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant, callback

from .const import (
    CACHE_APP,
    FRONTEND_BUILD_INFO,
    FRONTEND_DIR_NAME,
    FRONTEND_HTML,
    FRONTEND_PANEL_JS,
    FRONTEND_WEATHER,
    HEADER_SHA256,
    PANEL_COMPONENT,
    PANEL_ICON,
    PANEL_TITLE,
    PANEL_URL_PATH,
    URL_APP,
    URL_INFO,
    URL_PANEL_JS,
    URL_WEATHER,
)
from .models import async_loaded_data

_LOGGER = logging.getLogger(__name__)

# The package's own folder: integration/custom_components/wall_dashboard/frontend.
FRONTEND = Path(__file__).parent / FRONTEND_DIR_NAME

# What a package must carry next to the code (a package built wrong: the source folder copied as is).
REQUIRED = (FRONTEND_PANEL_JS, FRONTEND_HTML, FRONTEND_BUILD_INFO)

TEXT_NOT_SET_UP = "Nucleum Hub is not set up."
TEXT_MISSING = (
    "The dashboard file is missing from the Nucleum Hub package (frontend/wall-dashboard.html). "
    "Install the package again."
)
_BUILD_KEYS = ("build", "edition", "sha256")


def missing_files(folder: Path = FRONTEND) -> list[str]:
    """The files of ``REQUIRED`` that are not in ``folder`` (blocking: run it in the executor)."""
    return [name for name in REQUIRED if not (folder / name).is_file()]


def accepts_gzip(header: str | None) -> bool:
    """True when an ``Accept-Encoding`` header takes gzip (and not with ``q=0``)."""
    for part in (header or "").split(","):
        name, _, params = part.partition(";")
        if name.strip().lower() != "gzip":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


class DashboardFile:
    """The dashboard's file of the package, kept in memory with its gzip (about a third of the size, so
    the panel opens faster over a slow link) and its sha256. Read, compressed and hashed in the executor,
    once per version of the file: a file replaced on disk (a package copied over, before the restart) is
    noticed by its size and modification time."""

    def __init__(self, hass: HomeAssistant, folder: Path = FRONTEND) -> None:
        self.hass = hass
        self.folder = folder
        self._stamp: tuple[int, int] | None = None
        self._plain = b""
        self._packed = b""
        self._sha256 = ""
        self._lock = asyncio.Lock()

    @property
    def html(self) -> Path:
        return self.folder / FRONTEND_HTML

    def _stamp_now(self) -> tuple[int, int] | None:
        try:
            info = self.html.stat()
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return (info.st_size, info.st_mtime_ns)

    def _read(self) -> tuple[tuple[int, int], bytes, bytes, str] | None:
        stamp = self._stamp_now()
        if stamp is None:
            return None
        try:
            plain = self.html.read_bytes()
        except OSError:
            return None
        return stamp, plain, gzip.compress(plain, compresslevel=9, mtime=0), hashlib.sha256(plain).hexdigest()

    async def async_get(self) -> tuple[bytes, bytes, str] | None:
        """``(the file, its gzip, its sha256)``, or None when the package has no dashboard file."""
        stamp = await self.hass.async_add_executor_job(self._stamp_now)
        if stamp is None:
            return None
        async with self._lock:
            if stamp != self._stamp:
                read = await self.hass.async_add_executor_job(self._read)
                if read is None:
                    return None
                self._stamp, self._plain, self._packed, self._sha256 = read
            return self._plain, self._packed, self._sha256

    def build_info(self) -> dict[str, str] | None:
        """``{build, edition, sha256}`` from the package's build-info.json, or None (blocking)."""
        try:
            raw = json.loads((self.folder / FRONTEND_BUILD_INFO).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict):
            return None
        out: dict[str, str] = {}
        for key in _BUILD_KEYS:
            value = raw.get(key)
            if not isinstance(value, str) or not value:
                return None
            out[key] = value
        return out

    def clear(self) -> None:
        self._stamp, self._plain, self._packed, self._sha256 = None, b"", b"", ""


def _text(status: int, text: str) -> web.Response:
    return web.Response(status=status, text=text, headers={hdrs.CACHE_CONTROL: CACHE_APP})


class DashboardAppView(HomeAssistantView):
    """``/api/wall_dashboard/app``: the dashboard's HTML, to a signed-in user only."""

    url = URL_APP
    name = "api:wall_dashboard:app"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        data = async_loaded_data(self.hass)
        if data is None:
            return _text(404, TEXT_NOT_SET_UP)
        got = await data.dashboard.async_get()
        if got is None:
            return _text(404, TEXT_MISSING)
        plain, packed, sha256 = got
        headers = {
            hdrs.CACHE_CONTROL: CACHE_APP,
            hdrs.VARY: "Accept-Encoding",
            "X-Content-Type-Options": "nosniff",
            HEADER_SHA256: sha256,
        }
        body = plain
        if accepts_gzip(request.headers.get(hdrs.ACCEPT_ENCODING)):
            headers[hdrs.CONTENT_ENCODING] = "gzip"
            body = packed
        return web.Response(body=body, content_type="text/html", charset="utf-8", headers=headers)


class DashboardInfoView(HomeAssistantView):
    """``/api/wall_dashboard/info``: ``{version, dashboard: {build, edition, sha256} | null, file: sha256 | null}``.

    ``file`` is the sha256 of the file ``/api/wall_dashboard/app`` serves now (the same value it sends in
    ``X-Nucleum-Hub-Sha256``), or null when the package has none. The panel's loader asks this on a
    reconnection and every 30 minutes, and offers "New version: reload" when the version or the file
    differs from what the screen runs (D-054). Cheap when nothing changed: the file's size and time."""

    url = URL_INFO
    name = "api:wall_dashboard:info"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        data = async_loaded_data(self.hass)
        if data is None:
            return _text(404, TEXT_NOT_SET_UP)
        dashboard = await self.hass.async_add_executor_job(data.dashboard.build_info)
        got = await data.dashboard.async_get()
        return self.json(
            {"version": data.version, "dashboard": dashboard, "file": got[2] if got else None},
            headers={hdrs.CACHE_CONTROL: CACHE_APP},
        )


@callback
def async_register_views(hass: HomeAssistant) -> None:
    """Both views, once per Home Assistant run (``__init__`` guards against a second time)."""
    hass.http.register_view(DashboardAppView(hass))
    hass.http.register_view(DashboardInfoView(hass))


async def async_register_static_paths(hass: HomeAssistant, folder: Path = FRONTEND) -> None:
    """The loader and the weather pictures, public and cached, once per Home Assistant run."""
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(URL_PANEL_JS, str(folder / FRONTEND_PANEL_JS), cache_headers=True),
            StaticPathConfig(URL_WEATHER, str(folder / FRONTEND_WEATHER), cache_headers=True),
        ]
    )


def loader_tag(folder: Path = FRONTEND) -> str:
    """The first 10 hex of the loader's sha256, or "" without it (blocking). Its address carries it beside the version,
    so a loader changed under the same version is never served from a browser's cache (the static path is cached)."""
    try:
        return hashlib.sha256((folder / FRONTEND_PANEL_JS).read_bytes()).hexdigest()[:10]
    except OSError:
        return ""


async def async_register_panel(hass: HomeAssistant, version: str, title: str = PANEL_TITLE) -> bool:
    """The sidebar's entry, under the config entry's title ("Nucleum Hub" until its owner renames it), for
    every user. False when the address is already taken (a dashboard of the same address, say): the rest of the
    integration still works."""
    try:
        tag = await hass.async_add_executor_job(loader_tag)
        await panel_custom.async_register_panel(hass, **panel_config(version, title, tag))
    except ValueError:
        _LOGGER.warning(
            "The sidebar already has a panel at /%s, so Nucleum Hub's panel was not added; "
            "give that dashboard another address and reload Nucleum Hub",
            PANEL_URL_PATH,
        )
        return False
    return True


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """The panel leaves the sidebar (the static paths and the views stay until a restart)."""
    frontend.async_remove_panel(hass, PANEL_URL_PATH)


def panel_config(version: str, title: str = PANEL_TITLE, tag: str = "") -> dict[str, Any]:
    """The panel's registration, as the contract fixes it (docs/PACHET.md): the loader's module carries
    the version in its address (``v``, which the loader reads as its own) and the fingerprint of its file (``h``, for the
    browser's cache only), and gets the version in its config too; the sidebar shows ``title``."""
    return {
        "frontend_url_path": PANEL_URL_PATH,
        "webcomponent_name": PANEL_COMPONENT,
        "sidebar_title": title,
        "sidebar_icon": PANEL_ICON,
        "module_url": f"{URL_PANEL_JS}?v={version}" + (f"&h={tag}" if tag else ""),
        "embed_iframe": False,
        "require_admin": False,
        "config": {"version": version},
    }
