"""Who this home is, for the licence server (docs/LANSARE-PLAN.md §4.3, server/README.md §10.9).

- The home id binds the licence. 128 random bits, made once, at the first start and before the trial, and
  kept in a Store of its own (``.storage/wall_dashboard.home``). Removing the config entry never touches
  that file (nothing in this integration deletes a Store), so a reinstall finds the same home: no second
  trial, no extra activation. It is in every Home Assistant backup, so a restore keeps the licence. A cloned
  backup is the refresh nonce's business, not this file's.
- The instance's hash, ``iidh``, is a second signal for the trial (once per installation). It is computed at
  each request and never stored: the instance's UUID changes when Home Assistant repairs a duplicate id, so
  it can never be what binds a licence.

Neither leaves this module except towards the licence server: they are never logged and never reach the
dashboard, which only hears the licence's state (D-067).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.storage import Store

from .contract import STORE_HOME_KEY, STORE_HOME_VERSION, iidh_of, is_home_id, new_home_id

_LOGGER = logging.getLogger(__name__)

# One identity per Home Assistant run, shared by every setup of the entry, so a reload never races a first
# start into two home ids.
DATA_IDENTITY = "wall_dashboard_licence_identity"


class IdentityUnavailable(Exception):
    """The home id or the instance's hash cannot be had now. ``code`` says why:

    - ``store_unreadable``: the Store could not be read (or was written by a newer version of the
      integration). Nothing is written over it: a new id would cost the customer a move;
    - ``store_unwritable``: a new id could not be saved, so it is not used (the next attempt makes another);
    - ``no_instance_id`` / ``no_owner``: Home Assistant gave no instance UUID, or has no owner yet.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HomeIdentity:
    """The home id (read once, then kept in memory) and the instance's hash (computed each time)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        # private: the file is readable by Home Assistant's user only; atomic: a power cut while writing
        # leaves the old file or the new one, never half of one.
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_HOME_VERSION, STORE_HOME_KEY, private=True, atomic_writes=True
        )
        self._home: str | None = None
        self._lock = asyncio.Lock()
        # the licence asks again every hour while the id cannot be had: the log says so once
        self._warned = False

    async def async_home_id(self) -> str:
        """The home id, made and saved the first time it is asked for."""
        if self._home is not None:
            return self._home
        async with self._lock:
            if self._home is None:
                self._home = await self._async_load_or_make()
        return self._home

    async def _async_load_or_make(self) -> str:
        try:
            data = await self._store.async_load()
        except Exception as err:  # noqa: BLE001 (any reason the file cannot be read: keep it as it is)
            self._warn("The Nucleum Hub home id could not be read (%s); the licence waits until it can",
                       type(err).__name__)
            raise IdentityUnavailable("store_unreadable") from err
        if isinstance(data, dict) and is_home_id(data.get("home")):
            return str(data["home"])
        if data is not None:
            # Something the server would refuse anyway, so no licence rests on it. (Home Assistant moves a
            # corrupt file aside itself and hands over nothing, which lands in the branch below.)
            _LOGGER.warning("The Nucleum Hub home id store holds no usable home id; a new one is made")
        home = new_home_id()
        try:
            await self._store.async_save({"home": home})
        except Exception as err:  # noqa: BLE001 (a full disk, a read-only folder)
            self._warn("The Nucleum Hub home id could not be saved (%s); the licence waits until it can",
                       type(err).__name__)
            raise IdentityUnavailable("store_unwritable") from err
        return home

    def _warn(self, message: str, *args: Any) -> None:
        (_LOGGER.debug if self._warned else _LOGGER.warning)(message, *args)
        self._warned = True

    async def async_iidh(self) -> str:
        """The instance's hash for /v1/trial and /v1/activate: SHA-256 of the instance's UUID and the owner's
        id (contract.iidh_of). The owner is made at onboarding, before any integration can be added."""
        uuid = await instance_id.async_get(self._hass)
        if not isinstance(uuid, str) or not uuid:
            raise IdentityUnavailable("no_instance_id")
        owner = await self._hass.auth.async_get_owner()
        owner_id = getattr(owner, "id", None)
        if not isinstance(owner_id, str) or not owner_id:
            raise IdentityUnavailable("no_owner")
        return iidh_of(uuid, owner_id)


@callback
def async_get_identity(hass: HomeAssistant) -> HomeIdentity:
    """The one HomeIdentity of this Home Assistant run."""
    identity = hass.data.get(DATA_IDENTITY)
    if not isinstance(identity, HomeIdentity):
        identity = hass.data[DATA_IDENTITY] = HomeIdentity(hass)
    return identity
