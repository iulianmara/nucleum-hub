"""Reading a body whole, with a cap (18.11.2026).

aiohttp's ``read(n)`` gives back what has arrived so far, at most n bytes, not n bytes: a 1 MB picture of the idle screen came
back as its first few kilobytes, which no longer opened, so every free picture failed on a real network (a test's fake gave the
whole body at once). ``read_capped`` reads to the end, or stops one byte past the cap so the caller can tell it was too large.
Used for the free pictures and their list, the map tiles and a phone's upload to an album.
"""

from __future__ import annotations

from typing import Any

PIECE = 64 * 1024


async def read_capped(stream: Any, cap: int) -> bytes:
    """The body of ``stream`` (an aiohttp StreamReader), at most ``cap + 1`` bytes: longer than ``cap`` means too large."""
    body = bytearray()
    async for chunk in stream.iter_chunked(PIECE):
        body.extend(chunk)
        if len(body) > cap:
            break
    return bytes(body[: cap + 1])
