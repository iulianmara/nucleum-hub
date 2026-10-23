"""The pure part of the artwork conversion (D-050): no Home Assistant, no network, no event loop.

What is here, and why it is apart:
- ``sniff``: the format of a picture by its first bytes (the content type a server sends is not
  trusted; Home Assistant hands Apple TV artwork over as HEIC, ``ftypheic`` / ``ftypmif1``).
- ``round_size``: the sizes the dashboard may ask for (80, 160, 256, 512, 1024).
- ``ImageLRU`` and ``FailureMemo``: the in-memory caches (pictures, and refusals for a short while).
- ``is_public_ip`` / ``all_public`` / ``validate_target``: the SSRF guard's decisions, on addresses
  already resolved (the network side, which resolves and pins, is in ``artwork.py``).
- ``to_jpeg``: bytes in, JPEG bytes out. It imports Pillow and pillow_heif lazily, so this module
  loads with the standard library only (the tests in ``integration/tests``), and it is blocking
  work: Home Assistant runs it in the executor (``hass.async_add_executor_job``).

Written for the Python of Home Assistant and still loadable by an older Python 3.9 (the tests run
on the Mac's system Python): no ``match``, no ``type`` statements, annotations as strings.
The contract: ``docs/ARTWORK-INTEGRARE.md``.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Hashable, Iterable
import io
import ipaddress
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --- The contract's numbers ----------------------------------------------------------------------
# The formats the dashboard is told about, in this order; ``supported_formats`` keeps the ones this
# Home Assistant can really decode.
FORMATS: tuple[str, ...] = ("heic", "heif", "avif", "webp", "png", "jpeg", "gif", "bmp")
SIZES: tuple[int, ...] = (80, 160, 256, 512, 1024)
MAX_SIZE = SIZES[-1]
DEFAULT_SIZE = 512  # when ``size`` is missing (the dashboard always sends it)

JPEG_QUALITY = 85
BACKGROUND = (30, 30, 30)  # neutral dark: what a transparent cover is flattened on
MAX_PIXELS = 36_000_000  # 6000 x 6000; larger pictures are refused (memory on a small HA box)

CACHE_MAX_ENTRIES = 64
CACHE_MAX_BYTES = 24 * 1024 * 1024


class ConvertError(Exception):
    """A picture that cannot be turned into a JPEG (the views answer 415)."""


class NotAnImage(ConvertError):
    """The bytes are not one of the image formats we know (by their magic bytes)."""


class NotDecodable(ConvertError):
    """A known format that would not decode here (broken, too large, or no decoder)."""


class BadTarget(ValueError):
    """An address the url view refuses before any request (the views answer 400)."""


# --- Sniffing ----------------------------------------------------------------------------------
_HEIC_BRANDS = frozenset((b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"hevm", b"hevs"))
_AVIF_BRANDS = frozenset((b"avif", b"avis"))
_HEIF_BRANDS = frozenset((b"mif1", b"msf1", b"mif2", b"miaf"))
_BMP_DIB_SIZES = frozenset((12, 40, 52, 56, 64, 108, 124))


def _sniff_isobmff(data: bytes) -> str | None:
    """HEIC / HEIF / AVIF: an ISO BMFF ``ftyp`` box, by its major brand, then its compatible ones."""
    if data[4:8] != b"ftyp":
        return None
    box = int.from_bytes(data[0:4], "big")
    end = len(data) if box in (0, 1) else box  # 0: to the end; 1: a 64-bit size follows
    end = min(end, len(data), 1024)
    major = data[8:12]
    compatible = {data[i : i + 4] for i in range(16, end - 3, 4)}
    if major in _AVIF_BRANDS:
        return "avif"
    if major in _HEIC_BRANDS:
        return "heic"
    if major in _HEIF_BRANDS or compatible & _HEIF_BRANDS:
        if compatible & _AVIF_BRANDS:
            return "avif"
        if compatible & _HEIC_BRANDS:
            return "heic"
        if major in _HEIF_BRANDS:
            return "heif"
    return None


def sniff(data: bytes) -> str | None:
    """The format of a picture by its magic bytes: one of FORMATS, or None."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        return None
    head = bytes(data[:1024])
    if len(head) < 12:
        return None
    if head[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[:2] == b"BM" and len(head) >= 18 and int.from_bytes(head[14:18], "little") in _BMP_DIB_SIZES:
        return "bmp"
    return _sniff_isobmff(head)


def is_image_type(content_type: str | None) -> bool:
    """True for an ``image/...`` content type (parameters ignored)."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower().startswith("image/")


# --- Sizes ---------------------------------------------------------------------------------------
_DIGITS = re.compile(r"[0-9]{1,9}")


def round_size(raw: str | None) -> int:
    """The size to answer with: one of SIZES, rounded up, at most MAX_SIZE.

    Missing -> DEFAULT_SIZE. Not a positive whole number -> ValueError (the views answer 400).
    """
    if raw is None:
        return DEFAULT_SIZE
    text = str(raw).strip()
    if not _DIGITS.fullmatch(text):
        raise ValueError("size must be a positive whole number")
    value = int(text)
    if value <= 0:
        raise ValueError("size must be a positive whole number")
    for size in SIZES:
        if value <= size:
            return size
    return MAX_SIZE


# --- The picture a player shows ------------------------------------------------------------------
def picture_identity(picture: str) -> str:
    """What names a player's current picture, for the cache.

    Home Assistant's proxy address (``/api/media_player_proxy/<id>?token=..&cache=<hash>``) changes
    its token every few minutes while the picture stays the same; its ``cache=`` names the picture.
    Any other address is kept whole, without a ``token`` parameter.
    """
    parts = urlsplit(picture)
    query = parse_qsl(parts.query, keep_blank_values=True)
    cache = [value for key, value in query if key == "cache"]
    if cache:
        return f"{parts.path}#cache={cache[0]}"
    kept = [(key, value) for key, value in query if key != "token"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


# --- Caches --------------------------------------------------------------------------------------
class ImageLRU:
    """Converted pictures, least recently used out first, bounded by count and by bytes.

    Used from the event loop only (no lock). A picture larger than the whole budget is not kept.
    """

    def __init__(self, max_entries: int = CACHE_MAX_ENTRIES, max_bytes: int = CACHE_MAX_BYTES) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[Hashable, bytes] = OrderedDict()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: Hashable) -> bool:
        return key in self._items

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def get(self, key: Hashable) -> bytes | None:
        data = self._items.get(key)
        if data is not None:
            self._items.move_to_end(key)
        return data

    def put(self, key: Hashable, data: bytes) -> bool:
        """Keep ``data`` under ``key``; False when it is too large to keep at all."""
        self.discard(key)
        if len(data) > self.max_bytes or self.max_entries <= 0:
            return False
        self._items[key] = data
        self._bytes += len(data)
        while len(self._items) > self.max_entries or self._bytes > self.max_bytes:
            _, dropped = self._items.popitem(last=False)
            self._bytes -= len(dropped)
        return True

    def discard(self, key: Hashable) -> None:
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= len(old)

    def clear(self) -> None:
        self._items.clear()
        self._bytes = 0


class FailureMemo:
    """A refusal (its HTTP status) remembered for ``ttl`` seconds, so a dead address is not fetched
    again by every tablet that asks within that time. Bounded; the oldest goes first."""

    def __init__(self, ttl: float = 60.0, max_entries: int = 256, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[int, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Hashable) -> int | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        status, until = entry
        if self._clock() >= until:
            del self._items[key]
            return None
        return status

    def put(self, key: Hashable, status: int) -> None:
        self._items.pop(key, None)
        self._items[key] = (status, self._clock() + self.ttl)
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


# --- The SSRF guard's decisions ------------------------------------------------------------------
_V4_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # private
        "100.64.0.0/10",  # CGNAT, also VPNs such as Tailscale
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local
        "172.16.0.0/12",  # private
        "192.0.0.0/24",  # IETF protocol assignments
        "192.0.2.0/24",  # documentation
        "192.88.99.0/24",  # 6to4 relay (deprecated)
        "192.168.0.0/16",  # private
        "198.18.0.0/15",  # benchmarking
        "198.51.100.0/24",  # documentation
        "203.0.113.0/24",  # documentation
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, and the broadcast address
    )
)
_V6_GLOBAL = ipaddress.ip_network("2000::/3")  # the only global unicast block
_V6_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in (
        "2001::/23",  # IETF protocol assignments (Teredo, benchmarking, ORCHID)
        "2001:db8::/32",  # documentation
        "2002::/16",  # 6to4 (carries an IPv4 address that could be private)
        "3fff::/20",  # documentation (RFC 9637)
    )
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # well-known NAT64: the IPv4 address it carries counts


def _public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 4:
        return not any(ip in net for net in _V4_BLOCKED)
    mapped = ip.ipv4_mapped
    if mapped is not None:
        return _public(mapped)
    if ip in _NAT64:
        return _public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    if ip not in _V6_GLOBAL:  # loopback, unspecified, ULA fc00::/7, link-local fe80::/10, multicast ff00::/8, ...
        return False
    return not any(ip in net for net in _V6_BLOCKED)


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = str(text).strip().strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def is_ip_literal(host: str) -> bool:
    """True when ``host`` is an IPv4 or IPv6 address (brackets and a zone allowed)."""
    return _ip(host) is not None


def is_public_ip(address: str) -> bool:
    """True only for an address on the public internet.

    False for loopback, private, link-local, CGNAT, multicast, reserved, documentation, IPv6 ULA and
    link-local, an IPv4-mapped or NAT64 address that carries one of those, and for anything that is
    not an address at all.
    """
    ip = _ip(address)
    return ip is not None and _public(ip)


def all_public(addresses: Iterable[str]) -> bool:
    """True when there is at least one address and every one is public (a name that resolves to a
    public and a private address is refused: the connection could land on either)."""
    found = list(addresses)
    return bool(found) and all(is_public_ip(a) for a in found)


_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home.arpa", ".internal", ".localdomain")


def validate_target(scheme: str | None, host: str | None, has_userinfo: bool = False) -> None:
    """Refuse (BadTarget) an address the url view must not fetch, before any request.

    ``host`` is the host as the HTTP client will connect to it (yarl's ``raw_host``). A name is not
    resolved here: the resolver of the guarded session checks every address it resolves to, at
    connect time. An IP literal never reaches a resolver, so it is checked here.
    """
    if (scheme or "").lower() not in ("http", "https"):
        raise BadTarget("only http and https")
    if has_userinfo:
        raise BadTarget("no credentials in the address")
    name = (host or "").strip().strip("[]").rstrip(".").lower()
    if not name:
        raise BadTarget("no host")
    if is_ip_literal(name):
        if not is_public_ip(name):
            raise BadTarget("not a public address")
        return
    if name == "localhost" or name.endswith(_LOCAL_SUFFIXES):
        raise BadTarget("a name of the local network")


# --- Conversion (blocking: run it in the executor) ----------------------------------------------
_PIL_FORMATS = {
    "jpeg": ["JPEG", "MPO"],
    "png": ["PNG"],
    "gif": ["GIF"],
    "bmp": ["BMP"],
    "webp": ["WEBP"],
    "avif": ["AVIF"],
    "heic": ["HEIF"],
    "heif": ["HEIF"],
}
_ICC_MODES = frozenset(("RGB", "RGBA", "RGBX", "RGBa", "P", "PA"))
_ALPHA_MODES = frozenset(("RGBA", "RGBa", "LA", "La", "PA"))
_WIDE_GREY_MODES = frozenset(("I", "I;16", "I;16B", "I;16L", "I;16N"))

_heif_lock = threading.Lock()
_heif_ready: bool | None = None


def _register_heif() -> bool:
    """Teach Pillow HEIC / HEIF through pillow_heif, once; False when it is not available."""
    global _heif_ready  # noqa: PLW0603
    with _heif_lock:
        if _heif_ready is None:
            try:
                import pillow_heif  # noqa: PLC0415 (lazy on purpose)

                pillow_heif.register_heif_opener()
                _heif_ready = True
            except Exception:  # noqa: BLE001 (ImportError, or a native library that fails to load)
                _heif_ready = False
        return _heif_ready


def supported_formats() -> list[str]:
    """The FORMATS this Python can decode, in FORMATS order. Imports Pillow: run it in the executor."""
    try:
        from PIL import features  # noqa: PLC0415
    except ImportError:
        return []
    found = {"gif", "bmp"}
    if features.check_codec("zlib"):
        found.add("png")
    if features.check_codec("jpg"):
        found.add("jpeg")
    if "webp" in features.modules and features.check_module("webp"):
        found.add("webp")
    if "avif" in features.modules and features.check_module("avif"):  # Pillow's own AVIF (11.2 and later)
        found.add("avif")
    if _register_heif():
        found.update(("heic", "heif"))
    return [name for name in FORMATS if name in found]


def to_jpeg(
    data: bytes,
    size: int,
    *,
    quality: int = JPEG_QUALITY,
    background: tuple[int, int, int] = BACKGROUND,
    max_pixels: int = MAX_PIXELS,
) -> bytes:
    """A JPEG of ``data`` that fits in ``size`` x ``size``, ratio kept, never enlarged.

    Baseline (not progressive), EXIF orientation applied and EXIF dropped, transparency flattened on
    ``background``, an RGB colour profile kept. Raises NotAnImage (unknown magic bytes) or
    NotDecodable (anything that will not decode, or more than ``max_pixels``).
    """
    kind = sniff(data)
    if kind is None:
        raise NotAnImage("not an image this integration knows")
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
    except ImportError as err:
        raise NotDecodable("Pillow is not available") from err
    if kind in ("heic", "heif") and not _register_heif():
        raise NotDecodable("no HEIF decoder (pillow-heif) here")
    size = max(1, min(int(size), MAX_SIZE))
    try:
        with Image.open(io.BytesIO(bytes(data)), formats=_PIL_FORMATS[kind]) as src:
            if src.format in ("JPEG", "MPO"):
                src.draft("RGB", (size, size))  # decode at 1/2, 1/4 or 1/8 when that still covers size
            if src.width * src.height > max_pixels:
                raise NotDecodable("too many pixels")
            src.load()
            icc = src.info.get("icc_profile") if src.mode in _ICC_MODES else None
            image = src
            try:
                transposed = ImageOps.exif_transpose(src)
            except Exception:  # noqa: BLE001 (a broken EXIF block: keep the picture as it is)
                transposed = None
            if transposed is not None:
                image = transposed
            image = _prepare(image)
            image.thumbnail((size, size), Image.Resampling.LANCZOS)
            image = _flatten(image, background)
            out = io.BytesIO()
            params = {"quality": quality, "optimize": True, "progressive": False}
            if icc:
                params["icc_profile"] = icc
            image.save(out, format="JPEG", **params)
            return out.getvalue()
    except ConvertError:
        raise
    except Exception as err:  # noqa: BLE001 (decoders raise many kinds; every one means "cannot show it")
        raise NotDecodable(type(err).__name__) from err


def _prepare(image):  # noqa: ANN001, ANN202 (a PIL image; PIL is imported lazily)
    """A mode that resizes well: RGBA when there is transparency, RGB or L otherwise."""
    mode = image.mode
    if mode in _WIDE_GREY_MODES:  # a 16-bit grey PNG: down to 8 bits, not clipped to white
        return image.convert("I").point(lambda v: v * (1 / 256)).convert("L")
    if mode in _ALPHA_MODES or (mode in ("P", "L", "RGB") and "transparency" in image.info):
        return image.convert("RGBA")
    if mode not in ("RGB", "L"):
        return image.convert("RGB")  # P without transparency, CMYK, YCbCr, 1, ...
    return image


def _flatten(image, background: tuple[int, int, int]):  # noqa: ANN001, ANN202
    """RGB: a transparent picture laid on the neutral dark background."""
    from PIL import Image  # noqa: PLC0415

    if image.mode == "RGBA":
        base = Image.new("RGB", image.size, background)
        base.paste(image, mask=image.getchannel("A"))
        return base
    if image.mode != "RGB":
        return image.convert("RGB")
    return image
