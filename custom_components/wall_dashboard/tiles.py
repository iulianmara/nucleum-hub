"""The map of the places (D-116): OpenStreetMap's tiles asked by the integration, never by a screen, and kept here.

- The dashboard asks a tile with ``wall_dashboard/map/tile`` ({z, x, y}) and gets it back as base64 PNG: a screen asks
  nothing outside Home Assistant, needs no key, and the panel's frame (srcdoc) shows it as it is.
- A kept tile is answered from ``<config>/nucleum_hub/map_tiles/<z>/<x>/<y>.png`` and its mtime set to now: the last
  use, from which it is kept at least 6 months (tiles_core.KEEP_S); once a day the tiles not shown for that long go.
- A new one is fetched from OpenStreetMap with our own User-Agent, two at a time, the same tile once however many
  screens ask, at most tiles_core.RATE_MAX an hour; after a refusal or no answer nothing new is asked for 5 minutes.
The rules themselves, pure, are in tiles_core.py.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import os
from pathlib import Path
import time
from typing import Any

import aiohttp
from aiohttp import hdrs

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from . import tiles_core as core
from .streams import read_capped

_LOGGER = logging.getLogger(__name__)

FOLDER = ("nucleum_hub", "map_tiles")
FETCH_TIMEOUT = 10.0


class TileError(Exception):
    """A tile that cannot be given now: ``code`` is "invalid", "busy" (the hour's limit) or "unavailable"."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _read_and_touch(path: Path, now: float) -> bytes | None:
    try:
        body = path.read_bytes()
    except OSError:
        return None
    try:
        os.utime(path, (now, now))  # the last use: kept 6 months from here
    except OSError:
        pass
    return body


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def _sweep(folder: Path, now: float) -> int:
    if not folder.is_dir():
        return 0
    files = []
    for path in folder.rglob("*.png"):
        try:
            files.append((str(path), path.stat().st_mtime))
        except OSError:
            continue
    gone = 0
    for name in core.expired(files, now):
        try:
            os.remove(name)
            gone += 1
        except OSError:
            pass
    for path in sorted((p for p in folder.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            path.rmdir()  # only the empty ones
        except OSError:
            pass
    return gone


class MapTiles:
    """The tiles of the places' map, for the loaded entry."""

    def __init__(self, hass: HomeAssistant, version: str) -> None:
        self.hass = hass
        self.folder = Path(hass.config.path(*FOLDER))
        self._ua = core.user_agent(version)
        self._sem = asyncio.Semaphore(2)
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        self._rate = core.RateWindow()
        self._down_until = 0.0
        self._unsub: Any = None

    async def async_start(self) -> None:
        await self._async_sweep()
        self._unsub = async_track_time_interval(self.hass, self._async_sweep, timedelta(hours=24))

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_sweep(self, _now: Any = None) -> None:
        gone = await self.hass.async_add_executor_job(_sweep, self.folder, time.time())
        if gone:
            _LOGGER.debug("%s map tiles not shown for 6 months removed", gone)

    async def async_tile(self, z: Any, x: Any, y: Any) -> bytes:
        if not core.valid(z, x, y):
            raise TileError("invalid")
        rel = core.rel_path(z, x, y)
        path = self.folder / rel
        body = await self.hass.async_add_executor_job(_read_and_touch, path, time.time())
        if body is not None and core.is_png(body):
            return body
        if rel in self._inflight:
            return await asyncio.shield(self._inflight[rel])
        now = time.time()
        if now < self._down_until:
            raise TileError("unavailable")
        if not self._rate.allow(now):
            raise TileError("busy")
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._inflight[rel] = fut
        try:
            body = await self._async_fetch(z, x, y)
            await self.hass.async_add_executor_job(_write, path, body)
            fut.set_result(body)
            return body
        except TileError as err:
            fut.set_exception(err)
            fut.exception()  # read, so a future nobody else awaited is not logged
            raise
        finally:
            self._inflight.pop(rel, None)

    async def _async_fetch(self, z: int, x: int, y: int) -> bytes:
        session = async_get_clientsession(self.hass)
        async with self._sem:
            try:
                async with asyncio.timeout(FETCH_TIMEOUT):
                    async with session.get(core.url(z, x, y), headers={hdrs.USER_AGENT: self._ua, hdrs.ACCEPT: "image/png"}) as resp:
                        if resp.status != 200:
                            if resp.status != 404:
                                self._down_until = time.time() + core.DOWN_S
                            _LOGGER.debug("OpenStreetMap answered %s for a map tile", resp.status)
                            raise TileError("unavailable")
                        body = await read_capped(resp.content, core.MAX_BYTES)
            except TileError:
                raise
            except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
                self._down_until = time.time() + core.DOWN_S
                _LOGGER.debug("A map tile could not be fetched: %s", type(err).__name__)
                raise TileError("unavailable") from err
        if not core.is_png(body):
            raise TileError("unavailable")
        return body
