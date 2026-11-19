"""The idle screen's pictures (D-126): the free pictures of the internet, through the integration, and the house's albums.

- "Actualizează acum" (16.11.2026): ``wall_dashboard/photos/refresh`` ({themes}) asks the Worker for the list at once (not
  waiting for the day, nor for the half hour after a Worker that did not answer; within REFRESH_GAP the list just
  fetched) and gives the screen the keys to fetch; ``wall_dashboard/photos/prefetch`` ({keys}, a few at a time) fetches
  and keeps them, saying how many were new, so the screen counts and times the download. Any user of the house.
- The free pictures: ``wall_dashboard/photos/feed`` ({themes}) gives a screen the day's pictures of the chosen themes
  (title, author, licence, key; never an address); the Worker's list is asked for only from the first time a screen
  wants it (the internet's pictures are off until a person turns them on), then once a day while some screen asks.
  A screen shows one through ``/api/wall_dashboard/photo/<key>`` (authenticated, a signed path for an ``<img>``): the
  integration fetches it once from its source's own host (photos_core.IMAGE_HOSTS, https), makes it a JPEG of at most
  2560 px and keeps it in ``<config>/nucleum_hub/photo_cache/`` (300 MB, the least recently shown go first). What
  they take: ``wall_dashboard/photos/storage``; ``wall_dashboard/photos/clear`` removes them (D-150, an admin only).
- The house's albums: the photos in ``<media>/nucleum_hub/<album>/<photo>.jpg`` (Home Assistant's media folder: every
  screen sees them, the backup keeps them, Media › Local media shows them too), their list in the store
  ``wall_dashboard.albums``. From a screen: ``wall_dashboard/album/list``, ``/photos``, ``/create``, ``/remove``, and
  ``/upload`` (the photo in pieces of at most 512 KB, converted to JPEG here, HEIC from an iPhone too); a photo or its
  thumbnail through ``/api/wall_dashboard/album_photo/<album>/<photo>[/thumb]``.
- The tablet's QR code: ``wall_dashboard/album/link`` gives the home's own address of ``/nucleum_hub/album/<token>``, a
  page without an account for 10 minutes, for the first phone that opens it, only from the home's network (a private
  address, never through Home Assistant Cloud); it sends the photos one by one to ``.../upload``.
The rules themselves, pure, are in photos_core.py.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

import aiohttp
from aiohttp import hdrs, web

from homeassistant.components.http import KEY_HASS_USER, HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from . import convert
from . import photos_core as core
from .models import async_loaded_data
from .streams import read_capped

_LOGGER = logging.getLogger(__name__)

FEED_FILE = ("nucleum_hub", "photo_feed.json")
CACHE_DIR = ("nucleum_hub", "photo_cache")
THUMBS_DIR = ("nucleum_hub", "album_thumbs")
STORE_KEY = "wall_dashboard.albums"
STORE_VERSION = 1
FETCH_TIMEOUT = 20.0
URL_PICTURE = "/api/wall_dashboard/photo/{key}"
URL_ALBUM_PHOTO = "/api/wall_dashboard/album_photo/{album}/{photo}"
URL_ALBUM_THUMB = "/api/wall_dashboard/album_photo/{album}/{photo}/thumb"
URL_UPLOAD_PAGE = "/nucleum_hub/album/{token}"
URL_UPLOAD = "/nucleum_hub/album/{token}/upload"


class PhotoError(Exception):
    """Not given now: ``code`` is "unavailable" (the Worker or a source did not answer), "not_found", "invalid",
    "too_large", "not_supported" (a picture this Home Assistant cannot convert), "expired", "away"."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- blocking helpers (the executor) ------------------------------------------------------------------------------
def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(body)
    os.replace(tmp, path)


def _read_touch(path: Path) -> bytes | None:
    try:
        body = path.read_bytes()
    except OSError:
        return None
    try:
        now = time.time()
        os.utime(path, (now, now))
    except OSError:
        pass
    return body


def _sweep(folder: Path, limit: int) -> int:
    if not folder.is_dir():
        return 0
    rows = []
    for p in folder.glob("*.jpg"):
        try:
            st = p.stat()
            rows.append((str(p), st.st_size, st.st_mtime))
        except OSError:
            continue
    gone = 0
    for name in core.to_drop(rows, limit):
        try:
            os.remove(name)
            gone += 1
        except OSError:
            pass
    return gone


def _usage(folder: Path, extra: Path) -> tuple[int, int]:
    """The bytes the kept pictures (and the day's list beside them) take, and how many pictures (D-150)."""
    total, count = 0, 0
    if folder.is_dir():
        for p in folder.glob("*.jpg"):
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                continue
    try:
        total += extra.stat().st_size
    except OSError:
        pass
    return total, count


def _clear(folder: Path) -> int:
    """Every kept picture removed (never the house's albums, which live in the media folder); how many."""
    if not folder.is_dir():
        return 0
    gone = 0
    for p in folder.glob("*.jpg"):
        try:
            os.remove(p)
            gone += 1
        except OSError:
            pass
    return gone


def _to_jpeg(body: bytes, size: int) -> tuple[bytes, int | None, int | None]:
    """A JPEG of at most ``size`` px on its longer side, EXIF turned and dropped, with its size; a JPEG kept as it is
    when Pillow cannot run here."""
    try:
        out = convert.to_jpeg(body, size, limit=core.PICTURE_SIZE)
    except convert.NotDecodable as err:
        if convert.sniff(body) == "jpeg" and "Pillow" in str(err):
            out = bytes(body)
        else:
            raise PhotoError("not_supported") from err
    except convert.NotAnImage as err:
        raise PhotoError("not_supported") from err
    dims = core.jpeg_size(out)
    return out, (dims[0] if dims else None), (dims[1] if dims else None)


def _remove_files(paths: list[Path]) -> None:
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


# --- the free pictures ---------------------------------------------------------------------------------------------
class PictureFeed:
    """The Worker's list of free pictures and the pictures themselves, kept for the screens."""

    def __init__(self, hass: HomeAssistant, version: str) -> None:
        self.hass = hass
        self.version = version
        self.file = Path(hass.config.path(*FEED_FILE))
        self.cache = Path(hass.config.path(*CACHE_DIR))
        self.today: dict[str, Any] = {"day": None, "themes": {}}
        self.before: dict[str, Any] = {"day": None, "themes": {}}
        self.fetched_at = 0.0
        self.asked_at = 0.0
        self._down_until = 0.0
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(2)
        self._inflight: dict[str, asyncio.Future[bytes]] = {}
        self._unsub: Any = None

    async def async_start(self) -> None:
        kept = await self.hass.async_add_executor_job(_read_json, self.file)
        if isinstance(kept, dict):
            self.today = core.feed(kept.get("today"))
            self.before = core.feed(kept.get("before"))
            self.fetched_at = float(kept.get("fetched_at") or 0)
            self.asked_at = float(kept.get("asked_at") or 0)
        self._unsub = async_track_time_interval(self.hass, self._async_tick, _hours(6))

    async def async_stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def _async_tick(self, _now: Any = None) -> None:
        """Every 6 hours: the list again when it is a day old and a screen asked this week; the cache within its limit."""
        now = time.time()
        if self.asked_at and now - self.asked_at < core.FEED_IDLE and now - self.fetched_at > core.FEED_MAX_AGE:
            try:
                await self._async_refresh()
            except PhotoError:
                pass
        await self.hass.async_add_executor_job(_sweep, self.cache, core.CACHE_MAX_BYTES)

    async def _async_refresh(self, force: bool = False) -> None:
        async with self._lock:
            now = time.time()
            if now - self.fetched_at < (core.REFRESH_GAP if force else core.FEED_MAX_AGE) and self.today.get("themes"):
                return
            if now < self._down_until and not force:
                raise PhotoError("unavailable")
            session = async_get_clientsession(self.hass)
            try:
                async with asyncio.timeout(FETCH_TIMEOUT):
                    async with session.get(core.FEED_URL, headers={hdrs.USER_AGENT: _ua(self.version), hdrs.ACCEPT: "application/json"}) as resp:
                        if resp.status != 200:
                            raise PhotoError("unavailable")
                        raw = await read_capped(resp.content, core.FEED_MAX_BYTES)
                body = json.loads(raw.decode("utf-8")) if len(raw) <= core.FEED_MAX_BYTES else None
            except PhotoError:
                self._down_until = now + core.FEED_RETRY
                raise
            except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
                self._down_until = now + core.FEED_RETRY
                _LOGGER.debug("The free pictures' list could not be fetched: %s", type(err).__name__)
                raise PhotoError("unavailable") from err
            fresh = core.feed(body)
            if not fresh["themes"]:
                self._down_until = now + core.FEED_RETRY
                raise PhotoError("unavailable")
            if fresh.get("day") != self.today.get("day"):
                self.before = self.today
            self.today = fresh
            self.fetched_at = now
            await self._async_save()

    async def _async_save(self) -> None:
        body = json.dumps({"today": _with_urls(self.today), "before": _with_urls(self.before), "fetched_at": self.fetched_at,
                           "asked_at": self.asked_at}).encode("utf-8")
        await self.hass.async_add_executor_job(_write, self.file, body)

    async def async_items(self, themes: list[str]) -> dict[str, Any]:
        """The chosen themes' pictures for a screen: {"day", "items": [...]}; PhotoError("unavailable") when there is no
        list yet and the Worker does not answer."""
        first = not self.asked_at
        self.asked_at = time.time()
        if time.time() - self.fetched_at > core.FEED_MAX_AGE or not self.today.get("themes"):
            try:
                await self._async_refresh()
            except PhotoError:
                if not self.today.get("themes"):
                    raise
        elif first:
            await self._async_save()
        return {"day": self.today.get("day"), "items": core.for_screen(self.today, themes), **self.status()}

    def status(self) -> dict[str, Any]:
        """When the list was fetched (seconds since 1970, 0: never) and how many pictures and themes it holds."""
        th = self.today.get("themes", {})
        return {"fetched_at": int(self.fetched_at), "total": sum(len(v) for v in th.values()), "themes_n": len(th)}

    async def async_refresh_now(self, themes: list[str]) -> dict[str, Any]:
        """"Actualizează acum": the list asked for at once, then {"day", "items", "keys", fetched_at, total, themes_n}: keys
        are the chosen themes' pictures the screen fetches (prefetch_keys). PhotoError("unavailable") when the Worker does
        not answer and there is no list."""
        self.asked_at = time.time()
        try:
            await self._async_refresh(force=True)
        except PhotoError:
            if not self.today.get("themes"):
                raise
        items = core.for_screen(self.today, themes)
        return {"day": self.today.get("day"), "items": items, "keys": core.prefetch_keys(items), **self.status()}

    async def async_prefetch(self, keys: list[str]) -> dict[str, int]:
        """A few pictures fetched and kept: {"fresh": fetched now, "kept": already here, "failed"}."""
        fresh = kept = failed = 0
        todo = []
        for key in keys[: core.PREFETCH_BATCH]:
            if not isinstance(key, str) or core.find((self.today, self.before), key) is None:
                failed += 1
            elif await self.hass.async_add_executor_job((self.cache / f"{key}.jpg").is_file):
                kept += 1
            else:
                todo.append(key)
        for res in await asyncio.gather(*(self.async_picture(k) for k in todo), return_exceptions=True):
            if isinstance(res, BaseException):
                failed += 1
            else:
                fresh += 1
        return {"fresh": fresh, "kept": kept, "failed": failed}

    async def async_picture(self, key: str) -> bytes:
        it = core.find((self.today, self.before), key)
        if it is None:
            raise PhotoError("not_found")
        path = self.cache / f"{key}.jpg"
        body = await self.hass.async_add_executor_job(_read_touch, path)
        if body:
            return body
        if key in self._inflight:
            return await asyncio.shield(self._inflight[key])
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            raw = await self._async_fetch(it["url"])
            body, _w, _h = await self.hass.async_add_executor_job(_to_jpeg, raw, core.PICTURE_SIZE)
            await self.hass.async_add_executor_job(_write, path, body)
            fut.set_result(body)
            return body
        except PhotoError as err:
            fut.set_exception(err)
            fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)

    async def async_storage(self) -> dict[str, int]:
        """What the kept pictures take in Home Assistant (D-150): ``{"bytes", "limit", "count"}``."""
        used, count = await self.hass.async_add_executor_job(_usage, self.cache, self.file)
        return {"bytes": used, "limit": core.CACHE_MAX_BYTES, "count": count}

    async def async_clear(self) -> dict[str, int]:
        """The kept pictures removed (fetched again when a screen shows one); the day's list stays, so the screens keep
        their pictures. ``{"cleared": n}`` with what is left."""
        gone = await self.hass.async_add_executor_job(_clear, self.cache)
        return {"cleared": gone, **(await self.async_storage())}

    async def _async_fetch(self, url: str) -> bytes:
        if core.picture_url(url) is None:
            raise PhotoError("not_found")
        session = async_get_clientsession(self.hass)
        async with self._sem:
            try:
                async with asyncio.timeout(FETCH_TIMEOUT):
                    async with session.get(url, headers={hdrs.USER_AGENT: _ua(self.version), hdrs.ACCEPT: "image/*"}, allow_redirects=False) as resp:
                        if resp.status != 200:
                            raise PhotoError("unavailable")
                        body = await read_capped(resp.content, core.PICTURE_MAX_BYTES)
            except PhotoError:
                raise
            except (TimeoutError, aiohttp.ClientError, OSError, ValueError) as err:
                raise PhotoError("unavailable") from err
        if len(body) > core.PICTURE_MAX_BYTES or not body:
            raise PhotoError("too_large" if body else "unavailable")
        return body


def _with_urls(kept: dict[str, Any]) -> dict[str, Any]:
    """The list as the Worker gives it back (with each address), for the file it is kept in."""
    return {"day": kept.get("day"), "themes": {t: [dict(it) for it in items] for t, items in kept.get("themes", {}).items()}}


def _ua(version: str) -> str:
    return f"NucleumHub/{version or '0'} (Home Assistant integration; +https://nucleumhub.com) idle-screen pictures"


def _hours(n: int) -> Any:
    from datetime import timedelta  # noqa: PLC0415

    return timedelta(hours=n)


# --- the house's albums --------------------------------------------------------------------------------------------
def media_root(hass: HomeAssistant) -> Path:
    """``<media>/nucleum_hub``: Home Assistant's local media folder (its first one), else ``<config>/media``."""
    dirs = getattr(hass.config, "media_dirs", None) or {}
    base = dirs.get("local") if isinstance(dirs, dict) else None
    if not base and isinstance(dirs, dict) and dirs:
        base = next(iter(dirs.values()))
    return Path(base or hass.config.path("media")) / "nucleum_hub"


class HouseAlbums:
    """The photos uploaded from the phones, for every screen of the house."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store: Store = Store(hass, STORE_VERSION, STORE_KEY)
        self.albums: list[dict[str, Any]] = []
        self.root = media_root(hass)
        self.thumbs = Path(hass.config.path(*THUMBS_DIR))
        self.links: dict[str, dict[str, Any]] = {}  # token -> {album, expires, ip, session, count}
        self.uploads: dict[tuple[str, str], dict[str, Any]] = {}  # (user, upload id) -> {album, parts, size, at}

    async def async_start(self) -> None:
        try:
            self.albums = core.valid_albums(await self.store.async_load())
        except Exception:  # noqa: BLE001 (a damaged store: the albums' files stay, their list starts empty)
            _LOGGER.exception("The house's albums could not be read")
            self.albums = []

    async def async_stop(self) -> None:
        self.links.clear()
        self.uploads.clear()

    def _save(self) -> None:
        self.store.async_delay_save(lambda: {"albums": self.albums}, 2.0)

    def _house(self) -> None:
        if not any(a["id"] == core.HOUSE for a in self.albums):
            self.albums.insert(0, {"id": core.HOUSE, "name": "", "created": time.time(), "photos": []})
            self._save()

    def get(self, album: Any) -> dict[str, Any] | None:
        self._house()
        return next((a for a in self.albums if a["id"] == album), None) if isinstance(album, str) else None

    def list(self) -> list[dict[str, Any]]:
        self._house()
        now = time.time()
        return [core.summary(a, now) for a in self.albums]

    def photos(self, album: str) -> list[dict[str, Any]]:
        a = self.get(album)
        if a is None:
            raise PhotoError("not_found")
        now = time.time()
        return [core.photo_row(p, now) for p in reversed(a["photos"])]

    def create(self, name: Any) -> dict[str, Any]:
        n = core.album_name(name)
        if n is None:
            raise PhotoError("invalid")
        self._house()
        if len(self.albums) >= core.ALBUMS_MAX:
            raise PhotoError("too_large")
        a = {"id": core.new_id(10), "name": n, "created": time.time(), "photos": []}
        self.albums.append(a)
        self._save()
        return core.summary(a, time.time())

    def path_of(self, album: str, photo: str) -> Path:
        return self.root / album / f"{photo}.jpg"

    def thumb_of(self, album: str, photo: str) -> Path:
        return self.thumbs / album / f"{photo}.jpg"

    async def async_remove(self, album: str, ids: list[str]) -> int:
        a = self.get(album)
        if a is None:
            raise PhotoError("not_found")
        gone = [p for p in a["photos"] if p["id"] in set(ids)]
        if not gone:
            return 0
        a["photos"] = [p for p in a["photos"] if p not in gone]
        files = [self.path_of(album, p["id"]) for p in gone] + [self.thumb_of(album, p["id"]) for p in gone]
        await self.hass.async_add_executor_job(_remove_files, files)
        self._save()
        return len(gone)

    async def async_add(self, album: str, body: bytes, by: str = "") -> dict[str, Any]:
        """A photo as a phone sent it: converted to a JPEG (HEIC too), its thumbnail made, kept in the album."""
        a = self.get(album)
        if a is None:
            raise PhotoError("not_found")
        if len(a["photos"]) >= core.PHOTOS_MAX:
            raise PhotoError("too_large")
        if not body or len(body) > core.UPLOAD_MAX_BYTES:
            raise PhotoError("too_large")
        jpeg, w, h = await self.hass.async_add_executor_job(_to_jpeg, body, core.PICTURE_SIZE)
        thumb, _tw, _th = await self.hass.async_add_executor_job(_to_jpeg, jpeg, core.THUMB_SIZE)
        pid = core.new_id(14)
        await self.hass.async_add_executor_job(_write, self.path_of(album, pid), jpeg)
        await self.hass.async_add_executor_job(_write, self.thumb_of(album, pid), thumb)
        photo = {"id": pid, "w": w, "h": h, "by": core.who(by), "at": time.time(), "size": len(jpeg)}
        a["photos"].append(photo)
        self._save()
        return core.photo_row(photo, time.time())

    async def async_chunk(self, user: str, upload: str, album: str, seq: int, data: str, last: bool, by: str) -> dict[str, Any] | None:
        """A piece of a photo sent through the WebSocket; the photo itself once the last piece came."""
        now = time.time()
        for k in [k for k, u in self.uploads.items() if now - u["at"] > core.LINK_SECONDS]:
            self.uploads.pop(k, None)
        key = (user, upload)
        up = self.uploads.get(key)
        if seq == 0:
            if self.get(album) is None:
                raise PhotoError("not_found")
            up = self.uploads[key] = {"album": album, "parts": [], "size": 0, "at": now}
        if up is None or up["album"] != album or seq != len(up["parts"]):
            self.uploads.pop(key, None)
            raise PhotoError("invalid")
        try:
            piece = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as err:
            self.uploads.pop(key, None)
            raise PhotoError("invalid") from err
        if len(piece) > core.CHUNK_MAX_BYTES or up["size"] + len(piece) > core.UPLOAD_MAX_BYTES:
            self.uploads.pop(key, None)
            raise PhotoError("too_large")
        up["parts"].append(piece)
        up["size"] += len(piece)
        up["at"] = now
        if not last:
            return None
        self.uploads.pop(key, None)
        return await self.async_add(album, b"".join(up["parts"]), by)

    # --- the QR code's link ----------------------------------------------------------------------------------------
    def link(self, album: str) -> dict[str, Any]:
        if self.get(album) is None:
            raise PhotoError("not_found")
        now = time.time()
        for t in [t for t, l in self.links.items() if not core.link_ok(l, now) or l["album"] == album]:
            self.links.pop(t, None)   # the album's earlier code ends with a new one
        base = _local_url(self.hass)
        if not base:
            raise PhotoError("unavailable")
        token = core.new_token()
        self.links[token] = {"album": album, "expires": now + core.LINK_SECONDS, "ip": None, "session": None, "count": 0}
        return {"url": base.rstrip("/") + URL_UPLOAD_PAGE.format(token=token), "expires": now + core.LINK_SECONDS,
                "minutes": core.LINK_SECONDS // 60}

    def claim_link(self, token: str, ip: str) -> dict[str, Any]:
        """The page asked: the link for the first phone that opens it (the same phone may open it again)."""
        link = self.links.get(token) if core.TOKEN.match(token or "") else None
        if not core.link_ok(link, time.time()):
            raise PhotoError("expired")
        if link["ip"] not in (None, ip):
            raise PhotoError("expired")
        link["ip"] = ip
        if not link["session"]:
            link["session"] = core.new_token()
        return link


def _local_url(hass: HomeAssistant) -> str | None:
    """Home Assistant's address inside the home (its internal URL, or the address it found), never its cloud one."""
    try:
        from homeassistant.helpers.network import NoURLAvailableError, get_url  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return get_url(hass, allow_internal=True, allow_external=False, allow_cloud=False, allow_ip=True, prefer_external=False)
    except NoURLAvailableError:
        return None


def _from_home(hass: HomeAssistant, request: web.Request) -> str | None:
    """The phone's address when it is in the home's network and not through Home Assistant Cloud, else None."""
    ip = str(getattr(request, "remote", "") or "")
    if not core.is_home_address(ip):
        return None
    try:
        from homeassistant.helpers.network import is_cloud_connection  # noqa: PLC0415

        if is_cloud_connection(hass):
            return None
    except Exception:  # noqa: BLE001 (an older Home Assistant: the address alone)
        pass
    return ip


# --- the views ------------------------------------------------------------------------------------------------------
class PictureView(HomeAssistantView):
    """``/api/wall_dashboard/photo/<key>``: a free picture of the day's list, fetched and kept by the integration."""

    url = URL_PICTURE
    name = "api:wall_dashboard:photo"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, key: str) -> web.Response:
        data = async_loaded_data(self.hass)
        feed = getattr(data, "pictures", None) if data is not None else None
        if feed is None:
            return web.Response(status=404)
        try:
            body = await feed.async_picture(key)
        except PhotoError as err:
            return web.Response(status=404 if err.code == "not_found" else 502, headers={"Cache-Control": "no-store"})
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


class AlbumPhotoView(HomeAssistantView):
    """``/api/wall_dashboard/album_photo/<album>/<photo>`` and ``.../thumb``: a photo of the house's albums."""

    url = URL_ALBUM_PHOTO
    extra_urls = [URL_ALBUM_THUMB]
    name = "api:wall_dashboard:album_photo"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, album: str, photo: str) -> web.Response:
        data = async_loaded_data(self.hass)
        albums = getattr(data, "albums", None) if data is not None else None
        if albums is None or not core.ALBUM_ID.match(album) or not core.PHOTO_ID.match(photo):
            return web.Response(status=404)
        a = albums.get(album)
        if a is None or not any(p["id"] == photo for p in a["photos"]):
            return web.Response(status=404)
        thumb = request.path.endswith("/thumb")
        path = albums.thumb_of(album, photo) if thumb else albums.path_of(album, photo)
        body = await self.hass.async_add_executor_job(_read_touch, path)
        if not body:
            return web.Response(status=404)
        return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


class UploadPageView(HomeAssistantView):
    """``/nucleum_hub/album/<token>``: the page the QR code opens on a phone, without an account."""

    url = URL_UPLOAD_PAGE
    name = "nucleum_hub:album:page"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, token: str) -> web.Response:
        lang = core.page_language(request.headers.get(hdrs.ACCEPT_LANGUAGE) if hasattr(hdrs, "ACCEPT_LANGUAGE") else request.headers.get("Accept-Language"))
        data = async_loaded_data(self.hass)
        albums = getattr(data, "albums", None) if data is not None else None
        ip = _from_home(self.hass, request)
        if albums is None:
            return _page(lang, "ended", status=404)
        if ip is None:
            return _page(lang, "away", status=403)
        try:
            link = albums.claim_link(token, ip)
        except PhotoError:
            return _page(lang, "ended", status=403)
        return _page(lang, "ok", session=link["session"], minutes=core.remaining_minutes(link, time.time()))


class UploadView(HomeAssistantView):
    """``/nucleum_hub/album/<token>/upload``: one photo from the page above (its session in X-Album-Session)."""

    url = URL_UPLOAD
    name = "nucleum_hub:album:upload"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request, token: str) -> web.Response:
        data = async_loaded_data(self.hass)
        albums = getattr(data, "albums", None) if data is not None else None
        ip = _from_home(self.hass, request)
        link = albums.links.get(token) if albums is not None and core.TOKEN.match(token or "") else None
        if albums is None or ip is None or not core.link_ok(link, time.time()) or link["ip"] != ip \
                or not link["session"] or request.headers.get("X-Album-Session") != link["session"]:
            return self.json({"ok": False, "error": "expired"}, status_code=403)
        if link["count"] >= 300:
            return self.json({"ok": False, "error": "too_large"}, status_code=413)
        size = request.content_length
        if size is not None and size > core.UPLOAD_MAX_BYTES:
            return self.json({"ok": False, "error": "too_large"}, status_code=413)
        body = await read_capped(request.content, core.UPLOAD_MAX_BYTES)
        if len(body) > core.UPLOAD_MAX_BYTES:
            return self.json({"ok": False, "error": "too_large"}, status_code=413)
        try:
            row = await albums.async_add(link["album"], body, "")
        except PhotoError as err:
            return self.json({"ok": False, "error": err.code}, status_code=415 if err.code == "not_supported" else 400)
        link["count"] += 1
        return self.json({"ok": True, "photo": row["id"]})


@callback
def async_register_views(hass: HomeAssistant) -> None:
    for view in (PictureView(hass), AlbumPhotoView(hass), UploadPageView(hass), UploadView(hass)):
        hass.http.register_view(view)


# --- the upload page (the phone's language) ---------------------------------------------------------------------
PAGE_TEXT: dict[str, dict[str, str]] = {
    "en": {"title": "The home's album", "sub": "Send photos to the screens at home", "pick": "Choose photos", "sent": "Sent", "wait": "Waiting",
           "fail": "Didn't work", "foot": "The link lasts 10 minutes ({n} left) and works only on the home's Wi-Fi. You don't need an app or an account.",
           "ended": "This link has ended or was opened on another phone. Ask the tablet for a new code.",
           "away": "This link works only on the home's Wi-Fi. Connect the phone to the Wi-Fi and scan the code again."},
    "ro": {"title": "Albumul casei", "sub": "Trimiți poze pe ecranele din casă", "pick": "Alege poze", "sent": "Trimisă", "wait": "În așteptare",
           "fail": "Nu a mers", "foot": "Linkul ține 10 minute (încă {n}) și merge doar pe Wi-Fi-ul casei. Nu ai nevoie de aplicație sau de cont.",
           "ended": "Linkul a expirat sau a fost deschis pe alt telefon. Cere un cod nou pe tabletă.",
           "away": "Linkul merge doar pe Wi-Fi-ul casei. Conectează telefonul la Wi-Fi și scanează din nou codul."},
    "de": {"title": "Das Album des Hauses", "sub": "Fotos an die Bildschirme im Haus senden", "pick": "Fotos auswählen", "sent": "Gesendet", "wait": "Wartet",
           "fail": "Hat nicht geklappt", "foot": "Der Link gilt 10 Minuten (noch {n}) und funktioniert nur im WLAN des Hauses. Du brauchst keine App und kein Konto.",
           "ended": "Dieser Link ist abgelaufen oder wurde auf einem anderen Handy geöffnet. Lass dir auf dem Tablet einen neuen Code zeigen.",
           "away": "Dieser Link funktioniert nur im WLAN des Hauses. Verbinde das Handy mit dem WLAN und scanne den Code erneut."},
    "fr": {"title": "L’album de la maison", "sub": "Envoyez des photos sur les écrans de la maison", "pick": "Choisir des photos", "sent": "Envoyée", "wait": "En attente",
           "fail": "Échec", "foot": "Le lien dure 10 minutes (encore {n}) et ne fonctionne que sur le Wi-Fi de la maison. Aucune application ni aucun compte n’est nécessaire.",
           "ended": "Ce lien a expiré ou a été ouvert sur un autre téléphone. Demandez un nouveau code sur la tablette.",
           "away": "Ce lien ne fonctionne que sur le Wi-Fi de la maison. Connectez le téléphone au Wi-Fi et scannez à nouveau le code."},
    "nl": {"title": "Het album van het huis", "sub": "Stuur foto’s naar de schermen in huis", "pick": "Foto’s kiezen", "sent": "Verstuurd", "wait": "Wacht",
           "fail": "Mislukt", "foot": "De link werkt 10 minuten (nog {n}) en alleen op de wifi van het huis. Je hebt geen app of account nodig.",
           "ended": "Deze link is verlopen of op een andere telefoon geopend. Vraag een nieuwe code op de tablet.",
           "away": "Deze link werkt alleen op de wifi van het huis. Verbind de telefoon met de wifi en scan de code opnieuw."},
    "es": {"title": "El álbum de la casa", "sub": "Envía fotos a las pantallas de casa", "pick": "Elegir fotos", "sent": "Enviada", "wait": "En espera",
           "fail": "No ha funcionado", "foot": "El enlace dura 10 minutos (quedan {n}) y solo funciona en el wifi de casa. No necesitas app ni cuenta.",
           "ended": "Este enlace ha caducado o se abrió en otro teléfono. Pide un código nuevo en la tableta.",
           "away": "Este enlace solo funciona en el wifi de casa. Conecta el teléfono al wifi y vuelve a escanear el código."},
    "it": {"title": "L’album di casa", "sub": "Invia foto agli schermi di casa", "pick": "Scegli le foto", "sent": "Inviata", "wait": "In attesa",
           "fail": "Non è andata", "foot": "Il link dura 10 minuti (ancora {n}) e funziona solo sul Wi-Fi di casa. Non servono app né account.",
           "ended": "Questo link è scaduto o è stato aperto su un altro telefono. Chiedi un nuovo codice al tablet.",
           "away": "Questo link funziona solo sul Wi-Fi di casa. Collega il telefono al Wi-Fi e scansiona di nuovo il codice."},
    "pl": {"title": "Album domu", "sub": "Wysyłaj zdjęcia na ekrany w domu", "pick": "Wybierz zdjęcia", "sent": "Wysłane", "wait": "Czeka",
           "fail": "Nie udało się", "foot": "Link działa 10 minut (zostało {n}) i tylko w domowym Wi-Fi. Nie potrzebujesz aplikacji ani konta.",
           "ended": "Ten link wygasł lub otwarto go na innym telefonie. Poproś tablet o nowy kod.",
           "away": "Ten link działa tylko w domowym Wi-Fi. Połącz telefon z Wi-Fi i zeskanuj kod jeszcze raz."},
    "sv": {"title": "Husets album", "sub": "Skicka bilder till skärmarna hemma", "pick": "Välj bilder", "sent": "Skickad", "wait": "Väntar",
           "fail": "Det gick inte", "foot": "Länken gäller i 10 minuter ({n} kvar) och fungerar bara på husets wifi. Du behöver ingen app eller konto.",
           "ended": "Länken har gått ut eller öppnats på en annan telefon. Be surfplattan om en ny kod.",
           "away": "Länken fungerar bara på husets wifi. Anslut telefonen till wifi och skanna koden igen."},
    "cs": {"title": "Album domácnosti", "sub": "Posílejte fotky na obrazovky v domě", "pick": "Vybrat fotky", "sent": "Odesláno", "wait": "Čeká",
           "fail": "Nepovedlo se", "foot": "Odkaz platí 10 minut (zbývá {n}) a funguje jen na domácí Wi-Fi. Nepotřebujete aplikaci ani účet.",
           "ended": "Platnost odkazu vypršela nebo byl otevřen na jiném telefonu. Vyžádejte si na tabletu nový kód.",
           "away": "Odkaz funguje jen na domácí Wi-Fi. Připojte telefon k Wi-Fi a naskenujte kód znovu."},
    "da": {"title": "Husets album", "sub": "Send billeder til skærmene derhjemme", "pick": "Vælg billeder", "sent": "Sendt", "wait": "Venter",
           "fail": "Det lykkedes ikke", "foot": "Linket gælder i 10 minutter ({n} tilbage) og virker kun på husets wifi. Du behøver ingen app eller konto.",
           "ended": "Linket er udløbet eller blev åbnet på en anden telefon. Bed tabletten om en ny kode.",
           "away": "Linket virker kun på husets wifi. Forbind telefonen til wifi og scan koden igen."},
    "pt-BR": {"title": "O álbum da casa", "sub": "Envie fotos para as telas da casa", "pick": "Escolher fotos", "sent": "Enviada", "wait": "Aguardando",
              "fail": "Não deu certo", "foot": "O link vale por 10 minutos (faltam {n}) e só funciona no Wi-Fi da casa. Você não precisa de app nem de conta.",
              "ended": "Este link expirou ou foi aberto em outro celular. Peça um novo código no tablet.",
              "away": "Este link só funciona no Wi-Fi da casa. Conecte o celular ao Wi-Fi e escaneie o código de novo."},
}

_PAGE_CSS = (
    "*{box-sizing:border-box}body{margin:0;background:#fff;color:#1B1F27;font:16px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
    ".w{max-width:520px;margin:0 auto;padding:24px 20px 28px;min-height:100vh;display:flex;flex-direction:column}"
    ".hd{display:flex;gap:12px;align-items:center;margin:4px 0 18px}.lg{width:44px;height:44px;border-radius:14px;flex:none;"
    "background:linear-gradient(135deg,#4B55C8,#5b3fd0);display:grid;place-items:center;color:#fff}"
    "h1{font-size:20px;margin:0;line-height:1.2}.sub{font-size:13px;color:#5d6475;font-weight:600}"
    ".pick{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;min-height:56px;border:0;border-radius:16px;"
    "background:#4B55C8;color:#fff;font:700 16px system-ui,sans-serif;cursor:pointer}"
    ".list{display:flex;flex-direction:column;gap:8px;margin-top:14px}.row{display:flex;align-items:center;gap:12px;padding:8px;"
    "border-radius:14px;background:#F4F5F8}.th{width:52px;height:52px;border-radius:9px;flex:none;background:#E7E9EF center/cover no-repeat}"
    ".nm{flex:1;min-width:0}.nm b{display:block;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
    ".bar{height:5px;border-radius:3px;background:#E7E9EF;margin-top:7px;overflow:hidden}.bar i{display:block;height:100%;width:0;"
    "background:#4B55C8;transform-origin:left}.st{font:700 13px system-ui,sans-serif;color:#5d6475;min-width:72px;text-align:right}"
    ".ok .bar i{background:#21704A}.ok .st{color:#21704A}.bad .st{color:#B23434}"
    ".ft{margin-top:auto;padding-top:20px;font-size:13px;color:#5d6475;font-weight:600}.msg{margin-top:30px;font-size:16px;font-weight:600}"
)

_PAGE_JS = """(function(){var T=JSON.parse(document.getElementById('t').textContent),inp=document.getElementById('f'),list=document.getElementById('l'),q=[],busy=false;
function row(f){var r=document.createElement('div');r.className='row';var th=document.createElement('span');th.className='th';
if(/^image\\/(jpeg|png|webp|gif)$/.test(f.type)){try{th.style.backgroundImage='url('+URL.createObjectURL(f)+')';}catch(e){}}
var nm=document.createElement('div');nm.className='nm';var b=document.createElement('b');b.textContent=f.name;var bar=document.createElement('div');bar.className='bar';
var i=document.createElement('i');bar.appendChild(i);nm.appendChild(b);nm.appendChild(bar);var st=document.createElement('span');st.className='st';st.textContent=T.wait;
r.appendChild(th);r.appendChild(nm);r.appendChild(st);list.appendChild(r);return{r:r,i:i,st:st};}
function next(){if(busy||!q.length)return;busy=true;var j=q.shift(),x=new XMLHttpRequest();x['open']('POST',location.pathname.replace(/\\/$/,'')+'/upload');
x.setRequestHeader('X-Album-Session',T.session);x.upload.onprogress=function(e){if(e.lengthComputable){var p=Math.round(e.loaded/e.total*100);j.v.i.style.width=p+'%';j.v.st.textContent=p+' %';}};
x.onload=function(){var ok=x.status===200;j.v.r.className='row '+(ok?'ok':'bad');j.v.i.style.width='100%';j.v.st.textContent=ok?T.sent:T.fail;busy=false;next();};
x.onerror=function(){j.v.r.className='row bad';j.v.st.textContent=T.fail;busy=false;next();};x.send(j.f);}
inp.addEventListener('change',function(){Array.prototype.forEach.call(inp.files||[],function(f){q.push({f:f,v:row(f)});});inp.value='';next();});
var n=T.minutes,ft=document.getElementById('m');setInterval(function(){n=Math.max(0,n-1);ft.textContent=T.foot.replace('{n}',n);},60000);})();"""

_HOUSE_SVG = ('<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
              'stroke-linejoin="round"><path d="M4 10.5 12 4l8 6.5V20H4Z"/><path d="M9.5 20v-5.5h5V20"/></svg>')
_IMG_SVG = ('<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" '
            'stroke-linejoin="round"><rect x="3.5" y="5" width="17" height="14" rx="2.5"/><circle cx="9" cy="10" r="1.8"/>'
            '<path d="m4 17 5-4.5 3.5 3 3-2.5 4.5 4"/></svg>')


def page_html(lang: str, state: str, session: str | None = None, minutes: int = 0) -> str:
    """The phone's page: the button and the list of what is sent ("ok"), or why the link does not work here."""
    t = PAGE_TEXT.get(lang) or PAGE_TEXT["en"]
    e = html.escape
    head = (f'<!doctype html><html lang="{e(lang)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex"><meta name="referrer" content="no-referrer"><title>{e(t["title"])}</title><style>{_PAGE_CSS}</style></head><body><div class="w">'
            f'<div class="hd"><span class="lg">{_HOUSE_SVG}</span><div><h1>{e(t["title"])}</h1><div class="sub">{e(t["sub"])}</div></div></div>')
    if state != "ok":
        return head + f'<div class="msg">{e(t["ended" if state == "ended" else "away"])}</div></div></body></html>'
    data = json.dumps({"session": session or "", "minutes": int(minutes), "wait": t["wait"], "sent": t["sent"], "fail": t["fail"], "foot": t["foot"]})
    data = data.replace("<", "\\u003c")
    return (head + f'<label class="pick">{_IMG_SVG}<span>{e(t["pick"])}</span><input id="f" type="file" accept="image/*" multiple hidden></label>'
            f'<div class="list" id="l"></div><div class="ft" id="m">{e(t["foot"].replace("{n}", str(int(minutes))))}</div></div>'
            f'<script type="application/json" id="t">{data}</script><script>{_PAGE_JS}</script></body></html>')


def _page(lang: str, state: str, status: int = 200, session: str | None = None, minutes: int = 0) -> web.Response:
    return web.Response(text=page_html(lang, state, session, minutes), content_type="text/html", status=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"})
