"""Artwork (D-050): a music cover converted inside the home, for the dashboard's step 3.

Two authenticated views, loaded by the dashboard as ``<img>`` through a signed path
(``auth/sign_path``, 300 s), both answering ``image/jpeg``:

- ``/api/wall_dashboard/artwork/player/<entity_id>?size=<n>&v=<hash>``: the player's current
  picture, taken as Home Assistant's own proxy takes it (the entity's ``async_get_media_image()``);
  a picture given as a full address is fetched by Home Assistant itself, as its proxy would.
- ``/api/wall_dashboard/artwork/url?u=<address>&size=<n>``: a Library / browse thumbnail at an
  address on the internet, fetched by Home Assistant through the SSRF guard below.

The SSRF guard of the url view: http / https only, no credentials, an IP literal must be public;
names are resolved by ``PublicOnlyResolver`` inside the guarded session's own connector, so every
address a name resolves to is checked and the connection goes to exactly those addresses (no DNS
rebinding window). Redirects are followed by hand, at most 3, each one checked the same way; 10 s
for the whole fetch, 10 MB at most, an image or nothing.

Answers: 200 JPEG (``Cache-Control: private, max-age=86400``), 400 bad request (or an address in
the home), 403 a user who may not read that player (a 401 would count as a failed login
in HA's ban middleware), 404 no picture, 415 not an image or not
decodable, 502 the fetch failed. Addresses are never logged above debug level, and then only
their host (privacy). The contract: ``docs/ARTWORK-INTEGRARE.md``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
import logging
import socket
import sys
from typing import Any

import aiohttp
from aiohttp import hdrs, web
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ThreadedResolver
from yarl import URL

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.components.http import KEY_HASS_USER, HomeAssistantView
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util.ssl import get_default_context

from . import convert
from .const import (
    CACHE_ERROR,
    CACHE_OK,
    CONVERT_CONCURRENCY,
    FAILURE_TTL,
    MAX_URL_PARAM,
    MAX_V_PARAM,
    URL_ARTWORK_PLAYER,
    URL_ARTWORK_URL,
)
from .models import async_loaded_data

_LOGGER = logging.getLogger(__name__)

FETCH_TIMEOUT = 10.0  # seconds, for the whole fetch with its redirects
MAX_FETCH_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 3
_REDIRECTS = frozenset((301, 302, 303, 307, 308))
_ACCEPT = {hdrs.ACCEPT: "image/jpeg,image/png,image/webp,image/*;q=0.8"}
# The same user agent Home Assistant's own requests carry: nothing that names this product.
_USER_AGENT = (
    f"HomeAssistant/{HA_VERSION} aiohttp/{aiohttp.__version__} "
    f"Python/{sys.version_info[0]}.{sys.version_info[1]}"
)
_MEDIA_PLAYER = "media_player"
_REASONS = {
    400: "Bad request",
    403: "Not allowed to read this player",
    404: "No picture",
    415: "Not an image this integration can convert",
    502: "The picture could not be fetched",
}


class ArtworkError(Exception):
    """A refusal, with the HTTP status the view answers."""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(detail or _REASONS.get(status, "error"))
        self.status = status


# --- The guarded fetch (url view) ----------------------------------------------------------------
class BlockedAddress(OSError):
    """A name that resolves to an address in the home (or to nothing public)."""


class PublicOnlyResolver(AbstractResolver):
    """Resolves like the system does, then refuses unless every address is public.

    aiohttp's connector connects only to the addresses a resolver returns, so the check and the
    connection use the same answer: a name cannot be checked as public and then connect elsewhere.
    (aiohttp never asks a resolver about an IP literal; ``convert.validate_target`` checks those.)
    """

    def __init__(self) -> None:
        self._inner = ThreadedResolver()

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[dict[str, Any]]:
        results = await self._inner.resolve(host, port, family)
        if not convert.all_public(str(item.get("host", "")) for item in results):
            raise BlockedAddress("the name does not resolve to public addresses only")
        return results

    async def close(self) -> None:
        await self._inner.close()


def _is_blocked(err: BaseException) -> bool:
    """True when a client error came from PublicOnlyResolver's refusal."""
    seen: set[int] = set()
    current: BaseException | None = err
    while current is not None and id(current) not in seen:
        if isinstance(current, BlockedAddress):
            return True
        seen.add(id(current))
        os_error = getattr(current, "os_error", None)
        if isinstance(os_error, BlockedAddress):
            return True
        current = current.__cause__ or current.__context__
    return False


def _check_target(url: URL, first: bool) -> None:
    try:
        convert.validate_target(url.scheme, url.raw_host, bool(url.raw_user or url.raw_password))
    except convert.BadTarget as err:
        # the address the dashboard sent: 400; one a redirect led to: the fetch failed
        raise ArtworkError(400 if first else 502, str(err)) from err


async def _read_limited(resp: aiohttp.ClientResponse) -> bytes:
    """The body, at most MAX_FETCH_BYTES; stops early when it is plainly not an image."""
    if resp.content_length is not None and resp.content_length > MAX_FETCH_BYTES:
        raise ArtworkError(502, "too large")
    image_type = convert.is_image_type(resp.headers.get(hdrs.CONTENT_TYPE))
    body = bytearray()
    checked = image_type
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > MAX_FETCH_BYTES:
            raise ArtworkError(502, "too large")
        if not checked and len(body) >= 1024:  # what sniff() reads
            checked = True
            if convert.sniff(bytes(body)) is None:
                raise ArtworkError(415, "not an image")
    if not checked and convert.sniff(bytes(body)) is None:
        raise ArtworkError(415, "not an image")
    return bytes(body)


class GuardedFetcher:
    """Fetches an address on the internet for the url view, through PublicOnlyResolver.

    Home Assistant's shared session (``async_get_clientsession``) cannot take another resolver, so
    this one has its own connector: Home Assistant's SSL context, no proxy from the environment,
    no DNS cache (each new connection resolves and checks again). Closed on unload and on stop.
    """

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                resolver=PublicOnlyResolver(),
                ssl=get_default_context(),
                use_dns_cache=False,
                limit=8,
                limit_per_host=4,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                cookie_jar=aiohttp.DummyCookieJar(),  # nothing a server sets is sent back later
                headers={hdrs.USER_AGENT: _USER_AGENT},
            )
        return self._session

    async def async_close(self) -> None:
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

    async def fetch(self, url: URL) -> tuple[bytes, str]:
        session = self._get_session()
        current = url
        try:
            async with asyncio.timeout(FETCH_TIMEOUT):
                for hop in range(MAX_REDIRECTS + 1):
                    _check_target(current, first=hop == 0)
                    async with session.get(current, allow_redirects=False, headers=_ACCEPT) as resp:
                        if resp.status in _REDIRECTS:
                            location = resp.headers.get(hdrs.LOCATION)
                            if not location:
                                raise ArtworkError(502, "a redirect without a location")
                            current = current.join(URL(location)).with_fragment(None)
                            continue
                        if resp.status != 200:
                            raise ArtworkError(502, f"status {resp.status}")
                        return await _read_limited(resp), resp.content_type
        except ArtworkError:
            raise
        except TimeoutError as err:
            raise ArtworkError(502, "timeout") from err
        except (aiohttp.ClientError, OSError, ValueError) as err:
            if _is_blocked(err):
                raise ArtworkError(400 if current == url else 502, "not a public address") from err
            raise ArtworkError(502, type(err).__name__) from err
        raise ArtworkError(502, "too many redirects")


async def _fetch_trusted(hass: HomeAssistant, url: str) -> tuple[bytes, str]:
    """A player's own picture given as a full address: fetched by Home Assistant's shared session,
    as its media player proxy fetches it (the address comes from the integration, not from a
    browser, so it may be in the home: a local media server, say). Same limits otherwise."""
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(FETCH_TIMEOUT):
            async with session.get(url, max_redirects=MAX_REDIRECTS, headers=_ACCEPT) as resp:
                if resp.status != 200:
                    raise ArtworkError(502, f"status {resp.status}")
                return await _read_limited(resp), resp.content_type
    except ArtworkError:
        raise
    except TimeoutError as err:
        raise ArtworkError(502, "timeout") from err
    except (aiohttp.ClientError, OSError, ValueError) as err:
        raise ArtworkError(502, type(err).__name__) from err


def _player_entity(hass: HomeAssistant, entity_id: str) -> Any | None:
    """The media player entity object, as Home Assistant's own image proxy finds it."""
    component = hass.data.get(_MEDIA_PLAYER)
    if not isinstance(component, EntityComponent):
        return None
    entity = component.get_entity(entity_id)
    if entity is None or not hasattr(entity, "async_get_media_image"):
        return None
    return entity


# --- The service: cache, one conversion per picture, the executor ---------------------------------
class ArtworkService:
    """Pictures converted once and kept (``convert.ImageLRU``), refusals kept for a minute
    (``convert.FailureMemo``), and one fetch and conversion per picture however many tablets ask."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.cache = convert.ImageLRU()
        self.failures = convert.FailureMemo(ttl=FAILURE_TTL)
        self._locks: dict[Hashable, list[Any]] = {}
        self._slots = asyncio.Semaphore(CONVERT_CONCURRENCY)
        self._fetcher = GuardedFetcher()

    async def async_close(self) -> None:
        self.cache.clear()
        self.failures.clear()
        await self._fetcher.async_close()

    async def async_get(
        self, key: Hashable, produce: Callable[[], Awaitable[tuple[bytes, str]]], size: int
    ) -> bytes:
        """The JPEG for ``key``: from the cache, or ``produce()`` the original and convert it."""
        if (hit := self.cache.get(key)) is not None:
            return hit
        if (status := self.failures.get(key)) is not None:
            raise ArtworkError(status)
        slot = self._locks.get(key)
        if slot is None:
            slot = self._locks[key] = [asyncio.Lock(), 0]
        slot[1] += 1
        try:
            async with slot[0]:
                if (hit := self.cache.get(key)) is not None:
                    return hit
                if (status := self.failures.get(key)) is not None:
                    raise ArtworkError(status)
                try:
                    original, _content_type = await produce()
                    jpeg = await self._convert(original, size)
                except ArtworkError as err:
                    self.failures.put(key, err.status)
                    raise
                self.cache.put(key, jpeg)
                return jpeg
        finally:
            slot[1] -= 1
            if slot[1] == 0:
                self._locks.pop(key, None)

    async def _convert(self, original: bytes, size: int) -> bytes:
        if convert.sniff(original) is None:
            raise ArtworkError(415, "not an image")
        async with self._slots:
            try:
                return await self.hass.async_add_executor_job(convert.to_jpeg, original, size)
            except convert.ConvertError as err:
                raise ArtworkError(415, str(err)) from err

    async def player_original(self, entity_id: str, picture: str) -> tuple[bytes, str]:
        """The player's current picture as Home Assistant's media player proxy gives it."""
        player = _player_entity(self.hass, entity_id)
        data: bytes | None = None
        content_type: str | None = None
        if player is not None:
            try:
                data, content_type = await player.async_get_media_image()
            except Exception as err:  # noqa: BLE001 (another integration's error: a failed fetch)
                _LOGGER.debug("Artwork of %s: the player gave no picture (%s)", entity_id, type(err).__name__)
                data = None
        if data is None and picture.startswith(("http://", "https://")):
            return await _fetch_trusted(self.hass, picture)
        if not data:
            raise ArtworkError(502, "the player gave no picture")
        return bytes(data), content_type or ""

    async def url_original(self, url: URL) -> tuple[bytes, str]:
        try:
            return await self._fetcher.fetch(url)
        except ArtworkError as err:
            _LOGGER.debug("Artwork from %s: %s %s", url.host, err.status, err)
            raise


# --- The views ----------------------------------------------------------------------------------
def _error(status: int) -> web.Response:
    return web.Response(
        status=status,
        text=_REASONS.get(status, "error"),
        headers={hdrs.CACHE_CONTROL: CACHE_ERROR},
    )


def _jpeg(body: bytes) -> web.Response:
    return web.Response(
        body=body,
        content_type="image/jpeg",
        headers={hdrs.CACHE_CONTROL: CACHE_OK, "X-Content-Type-Options": "nosniff"},
    )


def _size(request: web.Request) -> int:
    try:
        return convert.round_size(request.query.get("size"))
    except ValueError as err:
        raise ArtworkError(400, "size") from err


class ArtworkPlayerView(HomeAssistantView):
    """``/api/wall_dashboard/artwork/player/<entity_id>?size=<n>&v=<hash>``."""

    url = URL_ARTWORK_PLAYER
    name = "api:wall_dashboard:artwork:player"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, entity_id: str) -> web.Response:
        data = async_loaded_data(self.hass)
        if data is None:
            return _error(404)
        if not valid_entity_id(entity_id) or entity_id.split(".", 1)[0] != _MEDIA_PLAYER:
            return _error(400)
        if len(request.query.get("v", "")) > MAX_V_PARAM:
            return _error(400)
        try:
            size = _size(request)
        except ArtworkError as err:
            return _error(err.status)
        user = request[KEY_HASS_USER]
        if not user.permissions.check_entity(entity_id, POLICY_READ):
            # 403, not Unauthorized: HA's ban middleware counts a raised 401 as a failed login, and a
            # wall tablet signed in as a limited user must never walk itself into an IP ban.
            return _error(403)
        state = self.hass.states.get(entity_id)
        picture = state.attributes.get("entity_picture") if state is not None else None
        if not isinstance(picture, str) or not picture:
            return _error(404)
        # The cache follows the picture the player shows now (the dashboard's v= names the same
        # picture for the browser's own cache).
        key = ("player", entity_id, convert.picture_identity(picture), size)
        try:
            body = await data.artwork.async_get(
                key, lambda: data.artwork.player_original(entity_id, picture), size
            )
        except ArtworkError as err:
            return _error(err.status)
        return _jpeg(body)


class ArtworkUrlView(HomeAssistantView):
    """``/api/wall_dashboard/artwork/url?u=<address>&size=<n>``."""

    url = URL_ARTWORK_URL
    name = "api:wall_dashboard:artwork:url"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        data = async_loaded_data(self.hass)
        if data is None:
            return _error(404)
        raw = request.query.get("u", "")
        if not raw or len(raw) > MAX_URL_PARAM:
            return _error(400)
        try:
            size = _size(request)
            target = URL(raw)
        except ArtworkError as err:
            return _error(err.status)
        except (ValueError, TypeError):
            return _error(400)
        if not target.is_absolute():
            return _error(400)
        try:
            _check_target(target, first=True)
        except ArtworkError as err:
            return _error(err.status)
        target = target.with_fragment(None)
        key = ("url", str(target), size)
        try:
            body = await data.artwork.async_get(key, lambda: data.artwork.url_original(target), size)
        except ArtworkError as err:
            return _error(err.status)
        return _jpeg(body)


@callback
def async_register_views(hass: HomeAssistant) -> None:
    """Both views, once per Home Assistant run (``__init__`` guards against a second time)."""
    hass.http.register_view(ArtworkPlayerView(hass))
    hass.http.register_view(ArtworkUrlView(hass))
