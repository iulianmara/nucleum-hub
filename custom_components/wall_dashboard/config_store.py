"""The dashboard's configuration kept by the integration, for every screen (D-178, P3-6).

The dashboard syncs its configuration (the layouts, the top bar, the dashboards, the photos: src/modules/sync.js) as
documents in three containers. Until 1.1.1 each container was a hidden Lovelace dashboard (``lovelace/config/save``),
which Home Assistant lets only an administrator write: a wall tablet signed in as an ordinary user (the safe way to set
one up) kept its whole layout on itself alone, lost when its browser data was cleared.

Here the integration keeps each container in its own file of Home Assistant's storage (``.storage/wall_dashboard.<path>``,
in Home Assistant's backups) and any signed-in user may read and write it. The first read of a container that is not here
yet takes it from its Lovelace dashboard, once, so nothing set up before is lost; the Lovelace dashboard is left as it
was. The containers' contents are the dashboard's (sync.js decides the revisions and the conflicts); here only their
names and their size are checked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
# sync.js CFG, IMG and V2 (the v2 container, read once for the import and tombstoned)
PATHS = frozenset({"wall-dashboard-v3-cfg", "wall-dashboard-v3-img", "wall-dashboard-v2-data"})
# the photos' container holds the house's pictures (2-3 MB today); a cap well above, below Home Assistant's message limit
MAX_BYTES = 16 * 1024 * 1024


class ConfigError(Exception):
    """A read or a write refused: ``code`` is what the dashboard is told ("invalid_format", "too_large")."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def size_of(config: Any) -> int:
    """The container's size as JSON, in bytes."""
    return len(json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class ConfigStore:
    """Each container in its own Store; a container read once from its Lovelace dashboard when it is not here yet."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._stores: dict[str, Store] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _store(self, path: str) -> Store:
        if path not in PATHS:
            raise ConfigError("invalid_format")
        if path not in self._stores:
            self._stores[path] = Store(self.hass, STORE_VERSION, f"{DOMAIN}.{path}")
            self._locks[path] = asyncio.Lock()
        return self._stores[path]

    async def _lovelace(self, path: str) -> dict[str, Any] | None:
        """The container as its hidden Lovelace dashboard holds it (an older build saved it there), or None."""
        from .dashboard import _dashboards  # noqa: PLC0415

        try:
            board = (_dashboards(self.hass) or {}).get(path)
            if board is None:
                return None
            config = await board.async_load(False)
        except Exception:  # noqa: BLE001 (ConfigNotFound: never saved; anything else: nothing to take)
            return None
        return config if isinstance(config, dict) else None

    async def async_read(self, path: str) -> dict[str, Any] | None:
        """The container, or None when there is none anywhere."""
        store = self._store(path)
        async with self._locks[path]:
            data = await store.async_load()
            if isinstance(data, dict) and isinstance(data.get("config"), dict):
                return data["config"]
            legacy = await self._lovelace(path)
            if legacy is None:
                return None
            await store.async_save({"config": legacy})
            _LOGGER.debug("The dashboard's %s was taken over from its Lovelace dashboard", path)
            return legacy

    async def async_write(self, path: str, config: Any) -> None:
        """The whole container, as the dashboard sends it."""
        store = self._store(path)
        if not isinstance(config, dict):
            raise ConfigError("invalid_format")
        if size_of(config) > MAX_BYTES:
            raise ConfigError("too_large")
        async with self._locks[path]:
            await store.async_save({"config": config})
