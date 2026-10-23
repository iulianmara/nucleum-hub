"""What the licence keeps between restarts (server/README.md §10.9 "Ce ține integrarea"), in a Store of its own:
``.storage/wall_dashboard.licence``.

- ``token``: the last token received (the trial's or the licence's), kept only once its signature verified for this
  home;
- ``nonce``: the last nonce the server gave, sent with the next refresh and replaced only by a 200 that brings another
  one, even after a backup was restored (never one made here: the server counts a stale one, plan §4.3);
- ``lic`` and ``role``: from the activation (the token has them too, but a token this build cannot verify does not
  lose the binding);
- ``revocations``: every revocation statement that verified, never deleted (a statement covers only the tokens issued
  up to it, D-067, so one kept forever never hurts a licence put back by support);
- ``trial``: True once the server answered the trial request (a token, trial_used, or licensed), so it is asked once;
- ``lost``: True when the server answered the trial with licensed: this home holds a licence, but what this Home
  Assistant kept of it is gone (the file was damaged or removed). The key is then asked for again, and the dashboard
  and Configure say why (a first start would offer to buy one). Cleared by the next token kept;
- the facts of the last refresh (``cloned``, ``moves_left``), and what the schedule needs to survive a restart:
  ``checked`` (the last 200), ``tried`` and ``failures`` (the last request and the failures in a row),
  ``not_before`` (a 429's Retry-After), ``bound`` (False after not_bound) and ``sent`` (the requests of the last
  day, by kind, for the budget).

Never the key: the customer has it in the e-mail and pastes it again if ever needed. The home id lives in its own
Store (identity.py), written once; this one is written after every request, so it is never the only copy of the one
value that must not be lost.

Like the home id's Store, nothing deletes this file: removing the config entry keeps it, so a reinstall keeps the
licence. One that cannot be read is never written over (the licence then stays out of the way: FULL with
broken_environment, plan §4.1 p.5), and one written by a newer version raises in Home Assistant's Store, which counts
as unreadable. A file that is not JSON any more (a power cut on an SD card) is another case: Home Assistant moves it
aside itself (``wall_dashboard.licence.corrupt.<time>``, with a repair issue) and hands over nothing, so the licence
starts as on a first start and the server's answer to the trial tells a lost licence apart (``lost`` above).

The file is read once per Home Assistant run, and what was read is shared by every setup of the entry with one lock
(``async_get_storage``, like the home id): a request that was on its way when the entry was reloaded lands in the
same memory the next setup works with, and two setups never hold two copies that write over each other's nonce.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .contract import STORE_LICENCE_KEY, STORE_LICENCE_VERSION, is_lic, is_nonce, is_role, is_statement

MAX_STATEMENTS = 256  # far more than a home ever gets (a move or a refund each); a bound on a file read at every start
KINDS = ("refresh", "trial")

# One LicenceStorage per Home Assistant run, shared by every setup of the entry (see the module's docstring).
DATA_STORAGE = "wall_dashboard_licence_storage"


@dataclass
class Held:
    """The licence's stored state. Every field is checked when read (``from_stored``): a value of the wrong shape is
    dropped, never trusted."""

    token: str | None = None
    nonce: str | None = None
    lic: str | None = None
    role: str | None = None
    revocations: list[str] = field(default_factory=list)
    trial: bool = False
    lost: bool = False
    cloned: bool = False
    moves_left: int | None = None
    checked: int | None = None
    tried: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    not_before: dict[str, int] = field(default_factory=dict)
    sent: dict[str, list[int]] = field(default_factory=dict)
    bound: bool = True

    @property
    def licensed(self) -> bool:
        """A licence is bound here as far as this home knows: it can be refreshed and released."""
        return self.lic is not None and self.nonce is not None

    def forget_licence(self) -> None:
        """After "free this home" (the release): the token, the nonce and the binding go; the statements, the trial's
        answer and the budget stay."""
        self.token = self.nonce = self.lic = self.role = None
        self.cloned = False
        self.checked = None
        self.bound = True
        self.failures.pop("refresh", None)
        self.not_before.pop("refresh", None)

    def stored(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_stored(cls, data: Any) -> Held:
        if not isinstance(data, dict):
            return cls()
        held = cls()
        if is_statement(data.get("token")):
            held.token = data["token"]
        if is_nonce(data.get("nonce")):
            held.nonce = data["nonce"]
        if is_lic(data.get("lic")):
            held.lic = data["lic"]
        if is_role(data.get("role")):
            held.role = data["role"]
        statements = data.get("revocations")
        if isinstance(statements, list):
            held.revocations = list(dict.fromkeys(s for s in statements if is_statement(s)))[-MAX_STATEMENTS:]
        held.trial = data.get("trial") is True
        held.lost = data.get("lost") is True
        held.cloned = data.get("cloned") is True
        held.moves_left = _count(data.get("moves_left"))
        held.checked = _moment(data.get("checked"))
        held.bound = data.get("bound") is not False
        for name in ("tried", "not_before"):
            value = data.get(name)
            if isinstance(value, dict):
                setattr(held, name, {k: m for k in KINDS if (m := _moment(value.get(k))) is not None})
        failures = data.get("failures")
        if isinstance(failures, dict):
            held.failures = {k: n for k in KINDS if (n := _count(failures.get(k))) is not None and n > 0}
        sent = data.get("sent")
        if isinstance(sent, dict):
            held.sent = {k: [m for m in sent[k][-200:] if _moment(m) is not None] for k in KINDS
                         if isinstance(sent.get(k), list)}
        return held


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100_000 else None


def _moment(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1e10 else None


class LicenceStorage:
    """The Store: private (Home Assistant's user only) and written atomically, like the home id's. With it, what was
    read from it and the lock every request of the licence takes, shared by the setups of one Home Assistant run."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_LICENCE_VERSION, STORE_LICENCE_KEY, private=True, atomic_writes=True
        )
        self.lock = asyncio.Lock()
        self._held: Held | None = None

    async def async_load(self) -> Held:
        """What was stored (an empty Held when there is no file), read the first time and then the same object for
        every caller. Raises when the file cannot be read: nothing is kept then, so the next call reads it again, and
        the caller must never save over it."""
        if self._held is None:
            self._held = Held.from_stored(await self._store.async_load())
        return self._held

    async def async_save(self, held: Held) -> None:
        await self._store.async_save(held.stored())


@callback
def async_get_storage(hass: HomeAssistant) -> LicenceStorage:
    """The one LicenceStorage of this Home Assistant run."""
    storage = hass.data.get(DATA_STORAGE)
    if not isinstance(storage, LicenceStorage):
        storage = hass.data[DATA_STORAGE] = LicenceStorage(hass)
    return storage
