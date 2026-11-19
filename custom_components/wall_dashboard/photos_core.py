"""The idle screen's pictures (D-126), pure: what the integration accepts of the Worker's list of free pictures, the
hosts it fetches them from, where each one is kept and which go when the cache is full; the house's albums (their ids,
names, photos) and the upload link of the tablet's QR code (10 minutes, one phone, only from the home's network).

- The free pictures: the Nucleum Hub Worker makes the list once a day (server/worker/photos.mjs, GET /v1/photos) from
  Wikimedia Commons' featured pictures, NASA, the Art Institute of Chicago and the Met. The integration asks for it only
  after a screen turned the internet's pictures on, keeps it a day, and fetches each picture itself from the source's
  own host (IMAGE_HOSTS, https only): a screen asks nothing outside Home Assistant and never an address of its choosing,
  only a key of the list.
- The house's album: photos uploaded from the phones, kept in Home Assistant's media folder (so every screen sees them
  and Home Assistant's backup keeps them), converted to JPEG here (HEIC from an iPhone too, convert.py).

Nothing here imports Home Assistant; tests/test_photos_core.py runs it as it is.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import ipaddress
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

# --- The free pictures -------------------------------------------------------------------------------------------
# The Worker's route (server/worker/index.mjs; the same server as licence/contract.API_BASE, which a test holds equal)
FEED_URL = "https://api.nucleumhub.com/v1/photos"
IMAGE_HOSTS = ("upload.wikimedia.org", "images-assets.nasa.gov", "images.metmuseum.org", "www.artic.edu")
THEMES = ("landscapes", "mountains", "sea", "cities", "animals", "birds", "space", "flowers", "art", "architecture",
          "spring", "summer", "autumn", "winter", "potd")
SOURCES = ("commons", "nasa", "aic", "met")
FEED_MAX_AGE = 20 * 3600  # the list is asked for again after this (once a day; the Worker makes it at 03:29 UTC)
FEED_RETRY = 30 * 60  # after the Worker did not answer, nothing asked for half an hour
FEED_IDLE = 7 * 86400  # no screen asked for a week: the list is no longer refreshed
FEED_MAX_BYTES = 2 * 1024 * 1024
PER_THEME = 40
PICTURE_MAX_BYTES = 20 * 1024 * 1024  # one picture, as fetched from its source
PICTURE_SIZE = 2560  # the longer side kept (a wall tablet is 1280 x 800 at most twice)
CACHE_MAX_BYTES = 300 * 1024 * 1024  # the pictures kept for the screens, the least recently shown go first
KEY = re.compile(r"^[0-9a-f]{24}$")


def _text(value: Any, limit: int) -> str:
    """Text as text: no control characters, spaces folded, at most ``limit`` characters."""
    out = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    out = re.sub(r"\s+", " ", out).strip()
    return out[:limit]


def _size(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 100000 else None


def picture_url(value: Any) -> str | None:
    """The picture's address when it is https on one of the sources' hosts, else None (nothing else is ever fetched)."""
    if not isinstance(value, str) or len(value) > 1024:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname not in IMAGE_HOSTS or parts.username or parts.password or parts.port not in (None, 443):
        return None
    return value


def key_of(url: str) -> str:
    """The key a screen asks a picture by: a hash of its address (the address itself never leaves the integration)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def item(raw: Any, theme: str) -> dict[str, Any] | None:
    """One picture of the Worker's list as kept here, or None when it is not one we may fetch and show."""
    if not isinstance(raw, dict):
        return None
    url = picture_url(raw.get("url"))
    if url is None or raw.get("src") not in SOURCES:
        return None
    lic = _text(raw.get("license"), 40)
    if not lic:
        return None
    return {"key": key_of(url), "url": url, "theme": theme, "src": raw["src"], "w": _size(raw.get("w")), "h": _size(raw.get("h")),
            "title": _text(raw.get("title"), 100), "author": _text(raw.get("author"), 80), "license": lic}


def feed(body: Any) -> dict[str, Any]:
    """The Worker's answer as kept here: {"day", "themes": {theme: [item]}}; anything else is left out."""
    out: dict[str, Any] = {"day": None, "themes": {}}
    if not isinstance(body, dict):
        return out
    day = body.get("day")
    out["day"] = day if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None
    themes = body.get("themes") if isinstance(body.get("themes"), dict) else {}
    for theme in THEMES:
        seen: set[str] = set()
        kept = []
        for raw in themes.get(theme) or []:
            it = item(raw, theme)
            if it is None or it["key"] in seen:
                continue
            seen.add(it["key"])
            kept.append(it)
            if len(kept) >= PER_THEME:
                break
        if kept:
            out["themes"][theme] = kept
    return out


def for_screen(kept: dict[str, Any], themes: Iterable[str]) -> list[dict[str, Any]]:
    """What a screen is told of the chosen themes: everything but the address (it asks by the key)."""
    out = []
    for theme in themes:
        if theme not in THEMES:
            continue
        for it in kept.get("themes", {}).get(theme, []):
            out.append({k: v for k, v in it.items() if k != "url"})
    return out


PREFETCH_MAX = 120  # "Actualizează acum": the pictures a screen fetches at once, the chosen themes in turn
PREFETCH_BATCH = 6  # the keys of one wall_dashboard/photos/prefetch (the screen counts after each)
REFRESH_GAP = 30  # seconds: a press again within them uses the list just fetched, the Worker is not asked again


def prefetch_keys(items: Iterable[dict[str, Any]], cap: int = PREFETCH_MAX) -> list[str]:
    """The keys of "Actualizează acum": the chosen themes' pictures in turn (one of each theme, then the next), each once,
    at most cap, so a few themes are fetched whole and many themes each get their share."""
    by: dict[str, list[str]] = {}
    for it in items:
        k = it.get("key") if isinstance(it, dict) else None
        if isinstance(k, str) and KEY.match(k):
            by.setdefault(str(it.get("theme") or ""), []).append(k)
    out: list[str] = []
    seen: set[str] = set()
    lists = list(by.values())
    i = 0
    while len(out) < cap and any(i < len(lst) for lst in lists):
        for lst in lists:
            if i < len(lst) and lst[i] not in seen and len(out) < cap:
                seen.add(lst[i])
                out.append(lst[i])
        i += 1
    return out


def find(kept: Iterable[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """The picture of a key in the lists kept (today's, then the day before's), or None."""
    if not isinstance(key, str) or not KEY.match(key):
        return None
    for one in kept:
        for items in (one or {}).get("themes", {}).values():
            for it in items:
                if it.get("key") == key:
                    return it
    return None


def to_drop(files: Iterable[tuple[str, int, float]], limit: int = CACHE_MAX_BYTES) -> list[str]:
    """The kept pictures to remove so the rest fit in ``limit``: ``files`` is (path, bytes, last use), the least
    recently used go first."""
    rows = sorted(files, key=lambda f: f[2])
    total = sum(max(0, int(f[1])) for f in rows)
    out = []
    for path, size, _used in rows:
        if total <= limit:
            break
        out.append(path)
        total -= max(0, int(size))
    return out


# --- The house's albums ------------------------------------------------------------------------------------------
HOUSE = "casa"  # the house's own album, made at the first ask; its name is the screen's ("Albumul casei")
ALBUM_ID = re.compile(r"^[a-z0-9]{4,16}$")
PHOTO_ID = re.compile(r"^[a-z0-9]{10,24}$")
NAME_MAX = 40
ALBUMS_MAX = 20
PHOTOS_MAX = 2000  # per album
UPLOAD_MAX_BYTES = 30 * 1024 * 1024  # one photo as sent by a phone (a HEIC or a JPEG is 1-8 MB)
CHUNK_MAX_BYTES = 512 * 1024  # a piece of an upload through the WebSocket (decoded)
LINK_SECONDS = 600  # the QR code's link: 10 minutes
NEW_SECONDS = 3 * 86400  # a photo is "new" for three days
THUMB_SIZE = 480


def album_name(value: Any) -> str | None:
    """An album's name as given (any language), or None when it is empty."""
    name = _text(value, NAME_MAX)
    return name or None


def new_id(length: int = 12) -> str:
    """A new album's or photo's id: lower-case letters and digits."""
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def who(value: Any) -> str:
    """Who added a photo, as the screen shows it ("2 new, from Ana"): a person's first name, at most 24 characters."""
    return _text(value, 24)


def is_new(at: Any, now: float) -> bool:
    return isinstance(at, (int, float)) and now - float(at) < NEW_SECONDS


def summary(album: dict[str, Any], now: float) -> dict[str, Any]:
    """What a screen is told of an album: its id, name, count, how many are new and who added the newest."""
    photos = album.get("photos") or []
    fresh = [p for p in photos if is_new(p.get("at"), now)]
    newest = max(fresh, key=lambda p: p.get("at") or 0) if fresh else None
    return {"id": album["id"], "name": album.get("name") or "", "count": len(photos), "new": len(fresh),
            "by": (newest or {}).get("by") or "", "house": album["id"] == HOUSE}


def photo_row(photo: dict[str, Any], now: float) -> dict[str, Any]:
    return {"id": photo["id"], "w": photo.get("w"), "h": photo.get("h"), "by": photo.get("by") or "", "at": photo.get("at"),
            "new": is_new(photo.get("at"), now)}


def valid_albums(data: Any) -> list[dict[str, Any]]:
    """The stored albums, repaired: a bad entry is left out, never the whole store."""
    out = []
    seen: set[str] = set()
    for a in (data or {}).get("albums") or [] if isinstance(data, dict) else []:
        if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not ALBUM_ID.match(a["id"]) or a["id"] in seen:
            continue
        seen.add(a["id"])
        photos = [p for p in a.get("photos") or [] if isinstance(p, dict) and isinstance(p.get("id"), str) and PHOTO_ID.match(p["id"])]
        out.append({"id": a["id"], "name": album_name(a.get("name")) or "", "created": a.get("created") or 0, "photos": photos[:PHOTOS_MAX]})
    return out[:ALBUMS_MAX]


# --- The upload link of the QR code ------------------------------------------------------------------------------
TOKEN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def link_ok(link: dict[str, Any] | None, now: float) -> bool:
    """A link that is still good: known and less than 10 minutes old."""
    return isinstance(link, dict) and now < float(link.get("expires") or 0)


def is_home_address(address: Any) -> bool:
    """True for an address of the home's own network: a private IPv4 (10/8, 172.16/12, 192.168/16), loopback, link-local,
    or an IPv6 unique local or link-local one. Never a public address, never the carrier's shared range (100.64/10,
    which VPNs such as Tailscale use too)."""
    try:
        ip = ipaddress.ip_address(str(address or "").strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_link_local:
        return True
    if ip.version == 4:
        return any(ip in ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    return ip in ipaddress.ip_network("fc00::/7")


def remaining_minutes(link: dict[str, Any] | None, now: float) -> int:
    """The whole minutes left (rounded up), 0 when it ended."""
    if not link_ok(link, now):
        return 0
    return int(-(-(float(link["expires"]) - now) // 60))


# The upload page's words (the phone's language, from its Accept-Language; English for any other)
PAGE_LANGS = ("en", "ro", "de", "fr", "nl", "es", "it", "pl", "sv", "cs", "da", "pt-BR")


def page_language(accept: Any) -> str:
    """The first of the phone's languages that the page speaks, else English."""
    for part in str(accept or "").split(","):
        tag = part.split(";", 1)[0].strip()
        if not tag:
            continue
        low = tag.lower()
        if low.startswith("pt"):
            return "pt-BR"
        base = low.split("-", 1)[0]
        if base in PAGE_LANGS:
            return base
    return "en"


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """A JPEG's width and height from its frame header (SOF0-SOF15 but DHT, JPG and DAC), or None."""
    if not isinstance(data, (bytes, bytearray)) or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        i += 2 + length
    return None
