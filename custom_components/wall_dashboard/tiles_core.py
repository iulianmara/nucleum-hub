"""The map of the places (D-116), pure: which OpenStreetMap tiles may be asked for, where each one is kept, when one
is old enough to go, and how many new ones may be fetched in an hour.

OpenStreetMap's tile policy (read 03.11.2026): a User-Agent of our own, the attribution on the map, a cache of at least
7 days, no bulk download and no offline maps. Ours: each tile is kept at least KEEP_S (6 months) from the last time a
screen showed it (the owner: "at least 6 months, the counter reset at every use of the place"), only the tiles a screen
asks for are fetched, at most RATE_MAX new ones an hour for the whole house.

Nothing here imports Home Assistant; tests/test_tiles_core.py runs it as it is.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
Z_MIN = 2
Z_MAX = 19  # OpenStreetMap's own last zoom
KEEP_S = 183 * 86400  # at least 6 months from the last use
RATE_MAX = 600  # new tiles fetched an hour, for the whole house
RATE_S = 3600
DOWN_S = 300  # after OpenStreetMap refused or did not answer, nothing new is asked for 5 minutes
MAX_BYTES = 512 * 1024  # an OpenStreetMap tile is 5–60 KB
PNG = b"\x89PNG\r\n\x1a\n"


def valid(z: object, x: object, y: object) -> bool:
    """A tile that exists: whole numbers, Z_MIN <= z <= Z_MAX, 0 <= x, y < 2**z."""
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (z, x, y)):
        return False
    if not Z_MIN <= z <= Z_MAX:  # type: ignore[operator]
        return False
    n = 1 << z  # type: ignore[operator]
    return 0 <= x < n and 0 <= y < n  # type: ignore[operator]


def rel_path(z: int, x: int, y: int) -> str:
    """Where a tile is kept in the integration's folder: ``<z>/<x>/<y>.png``."""
    return f"{z}/{x}/{y}.png"


def url(z: int, x: int, y: int) -> str:
    return TILE_URL.format(z=z, x=x, y=y)


def user_agent(version: str) -> str:
    """Our own User-Agent, as OpenStreetMap asks: the product, its version and where to reach us."""
    return f"NucleumHub/{version or '0'} (Home Assistant integration; +https://nucleumhub.com)"


def is_png(body: bytes) -> bool:
    return len(body) <= MAX_BYTES and body[:8] == PNG


def expired(files: Iterable[tuple[str, float]], now: float) -> list[str]:
    """The tiles not shown for KEEP_S: ``files`` is (path, last use) pairs, the last use being the file's mtime."""
    edge = now - KEEP_S
    return [p for p, used in files if used < edge]


class RateWindow:
    """At most ``limit`` new tiles in any ``span`` seconds (the tiles already kept are never counted)."""

    def __init__(self, limit: int = RATE_MAX, span: float = RATE_S) -> None:
        self.limit, self.span = limit, span
        self._at: deque[float] = deque()

    def allow(self, now: float) -> bool:
        while self._at and self._at[0] <= now - self.span:
            self._at.popleft()
        if len(self._at) >= self.limit:
            return False
        self._at.append(now)
        return True
