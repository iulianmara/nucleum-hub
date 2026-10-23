"""The licence at work (D-064 to D-068, D-073; server/README.md §10.9): the trial at the first start, the activation
and the release that the admin asks for in the options flow (flow.py), the refreshes on their schedule
(schedule.py), the revocation statements kept and applied, and the state the dashboard is told over the WebSocket
(websocket.py: ``wall_dashboard/licence`` and its subscription).

Made once per setup of the entry, only in the sale edition and only after the panel is registered (``__init__``
imports this module lazily, in the executor, inside ``try``): whatever goes wrong here, the panel and its alarm keypad
stay. Every request runs in the background, never in the panel's path, and one at a time (a lock), so a refresh never
races an activation for the nonce. The lock and what the store holds are shared by every setup of one Home Assistant
run (storage.async_get_storage), and each request runs to its end once it is on the wire, whoever stops waiting for
it (``_exchange``): the nonce the server rotated is always kept, because a stale one counts towards "cloned".

The rules it keeps:

- nothing unsigned ever changes the state (plan §4.8 p.8). A token is stored only when it verifies, for this home and
  this licence; a statement only when it verifies, for this licence and this home (or every home); an error answer
  only adds the statements it carries. The token held is replaced by a newer one or deleted when the admin frees the
  home, nothing else;
- Home Assistant's time decides, never a tablet's (plan §4.4), and the state is computed again whenever it can change
  (a period's end, a grace's end, the 30 days of the one-time licence's notice) and at least every hour, so a clock
  that jumped is noticed;
- a store that cannot be read, or a home id that cannot be had, leaves the dashboard fully usable: FULL with the reason
  broken_environment (plan §4.1 p.5), and nothing is written over what could not be read. Both are tried again at
  each wake, so a disk that was not ready at the start does not keep the licence out of the way until a restart;
- the key is never kept: it goes to the server once and is forgotten.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
import logging
import math
import random
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from ..const import LICENCE_REVOKED_KIDS, LICENCE_ROOTS, SIGNAL_LICENCE
from . import schedule
from .api import (
    ActiveElsewhere,
    ApiNotConfigured,
    Conflict,
    KeyMistyped,
    LicenceApi,
    LicenceApiError,
    MoveLimit,
    NotBound,
    RateLimited,
    RequestRejected,
    RoleConflict,
    TrialLicensed,
    TrialUsed,
    async_get_api,
)
from .contract import API_BASE, TIMEOUT, is_role, is_version, site_link
from .identity import HomeIdentity, IdentityUnavailable, async_get_identity
from .state import LIFE_VERIFY_S, state_of_verified, window_of
from .storage import MAX_STATEMENTS, Held, async_get_storage
from .verifier import Verifier

_LOGGER = logging.getLogger(__name__)

# The longest the licence waits before it looks at its state again: a clock that jumped is noticed within this.
WAKE_MAX = 3600.0
# How long stopping waits for a request already on the wire: its whole timeout, then the save of its answer.
STOP_WAIT = TIMEOUT + 5.0

# What the dashboard is told when the licence cannot judge (plan §4.1 p.5): the same shape as every answer.
BROKEN = {"state": "FULL", "reason": "broken_environment", "until": None, "plan": None, "role": None, "grace": 0,
          "notice": None}

# The link the dashboard shows next to a state: (its kind, the site's page). Only these states have one: the others
# are fixed in Home Assistant (activate again, connect it to the internet) or by support (a refund, a revocation).
# The trial's is the "Choose a plan" of its last minutes (D-076, T2).
LINKS: dict[tuple[str, str | None], tuple[str, str]] = {
    ("TRIAL", None): ("buy", "pricing"),
    ("ACTIVATE", "no_token"): ("buy", "pricing"),
    ("ACTIVATE", "trial_over"): ("buy", "pricing"),
    ("ACTIVATE", "expired"): ("buy", "pricing"),
    ("ACTIVATE", "subscription_expired"): ("renew", "pricing"),
    ("GRACE", "payment_due"): ("payment", "account"),
    ("UNCOVERED", "build_after_ent"): ("renew", "pricing"),
    ("UNCOVERED", "not_on_kept_lts"): ("renew", "pricing"),
}


def _ha_now() -> float:
    """Home Assistant's time, in unix seconds (the tests put the fake's clock here)."""
    return time.time()


@dataclass(frozen=True)
class Outcome:
    """What a request the admin asked for came to. ``error`` is None when it was done, else a code of the options
    flow's errors (strings.json): invalid_key, unknown_key, licence_inactive, subscription_ended, plan_unsupported,
    active_elsewhere, move_limit, home_has_licence, role_conflict, conflict, rate_limited, not_configured,
    cannot_connect, identity, invalid_token, not_bound, unavailable, unknown; or, for the trial asked from the settings,
    trial_used and licensed. With it, the facts to show."""

    error: str | None = None
    minutes: int | None = None  # rate_limited: when to try again
    last_seen: str | None = None  # active_elsewhere: the day the other home was last seen
    moves_left: int | None = None  # active_elsewhere, a release: the moves left in 365 days (None for staging)
    role: str | None = None  # active_elsewhere, role_conflict

    @property
    def ok(self) -> bool:
        return self.error is None


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


def _api_error(err: LicenceApiError) -> Outcome:
    """The options flow's error for an answer that is not a 200."""
    if isinstance(err, KeyMistyped):
        return Outcome("invalid_key")
    if isinstance(err, RateLimited):
        return Outcome("rate_limited", minutes=_minutes(err.retry_after))
    if isinstance(err, ApiNotConfigured):
        return Outcome("not_configured")
    if isinstance(err, ActiveElsewhere):
        if not err.can_move:
            return Outcome("move_limit", role=err.role, moves_left=err.moves_left)
        return Outcome("active_elsewhere", last_seen=err.last_seen, moves_left=err.moves_left, role=err.role)
    if isinstance(err, (MoveLimit, RoleConflict)):
        return Outcome(err.code, role=err.role)
    if err.code in ("unknown_key", "licence_inactive", "subscription_ended", "plan_unsupported", "home_has_licence",
                    "conflict", "not_bound", "trial_used", "licensed"):
        return Outcome(err.code)
    if err.transient:
        return Outcome("cannot_connect")
    return Outcome("unknown")


def build_stamps(info: Any) -> dict[str, Any] | None:
    """The build's stamps the state needs (state.py ctx.build) from the package's frontend/build-info.json:
    ``released`` (the build's own ``released`` when it has one, else ``date``, the UTC day of the commit it was made
    from), ``base_released`` and ``lts`` when the build carries them. None when there is no usable date. An ``lts``
    the state rule would refuse is left out rather than failing every answer."""
    if not isinstance(info, Mapping):
        return None
    stamps = {"released": None, "base_released": None, "lts": None}
    for name in ("released", "date"):
        stamps["released"] = info.get(name)
        if _usable(stamps):
            break
    else:
        return None
    for name in ("base_released", "lts"):
        value = info.get(name)
        if value is not None and _usable({**stamps, name: value}):
            stamps[name] = dict(value) if isinstance(value, Mapping) else value
    return stamps


def _usable(stamps: Mapping[str, Any]) -> bool:
    """Whether the state rule accepts these stamps: asked of the rule itself, so the two never disagree."""
    try:
        state_of_verified(None, {"now": 2_000_000_000, "build": stamps, "home": None, "revocations": []}, True)
    except TypeError:
        return False
    return True


def make_verifier() -> Verifier:
    """The build's trust (const.LICENCE_ROOTS, const.LICENCE_REVOKED_KIDS) and the Ed25519 self-test. Blocking the
    first time (the cryptography package is imported): run it in the executor."""
    return Verifier(LICENCE_ROOTS, LICENCE_REVOKED_KIDS)


async def async_start(hass: HomeAssistant, *, build_info: Any, verifier: Verifier | None = None,
                      api: LicenceApi | None = None, clock: Callable[[], float] | None = None,
                      rng: schedule.Random | None = None) -> LicenceManager:
    """The licence of this setup, started: its store read, the trial or the next refresh planned."""
    if verifier is None:
        verifier = await hass.async_add_executor_job(make_verifier)
    manager = LicenceManager(
        hass,
        verifier=verifier,
        api=api if api is not None else async_get_api(hass, API_BASE),
        identity=async_get_identity(hass),
        build_info=build_info,
        clock=clock or _ha_now,
        rng=rng or random.SystemRandom(),
    )
    await manager.async_start()
    return manager


class LicenceManager:
    """The licence of one setup of the entry (see the module's docstring)."""

    def __init__(self, hass: HomeAssistant, *, verifier: Verifier, api: LicenceApi, identity: HomeIdentity,
                 build_info: Any, clock: Callable[[], float], rng: schedule.Random) -> None:
        self._hass = hass
        self._verifier = verifier
        self._api = api
        self._identity = identity
        self._storage = async_get_storage(hass)
        self._clock = clock
        self._rng = rng
        self._build = build_stamps(build_info)
        version = build_info.get("version") if isinstance(build_info, Mapping) else None
        self._version = version if is_version(version) else None  # the dashboard's version, told to the server
        self._held = Held()
        self._home: str | None = None
        self._broken: str | None = None if self._build is not None else "build"
        self._lock = self._storage.lock
        self._due: float | None = None
        self._unsub_wake: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()  # the wakes and the checks the settings asked for
        self._exchanges: set[asyncio.Task[Outcome]] = set()  # the requests themselves (_exchange)
        self._stopped = False
        self._said: Any = None
        self._token_cache: dict[str, dict[str, Any]] = {}
        self._statement_cache: dict[str, dict[str, Any] | None] = {}
        self._clock_warned = False
        self._rule_failed = False

    # --- start and stop ---------------------------------------------------------------------------------------------
    async def async_start(self) -> None:
        if self._broken is None:
            await self._async_load()
        if self._broken == "build":
            _LOGGER.error("The Nucleum Hub package has no usable build date (frontend/build-info.json): the licence "
                          "cannot judge it, so the dashboard stays fully usable; install the package again")
        self._plan(starting=True)
        self._arm()
        # what the subscriptions are told from here on is measured against this; the start itself is told by setup,
        # once the entry holds this manager (__init__)
        self._said = self._said_now(self._clock())

    async def _async_load(self) -> None:
        """The home id and what the store holds; when either cannot be had, ``_broken`` says why and nothing is
        written over what could not be read. Tried at the start, then at each wake until it works (_async_wake)."""
        try:
            self._home = await self._identity.async_home_id()
            self._held = await self._storage.async_load()
        except IdentityUnavailable as err:
            self._broken = f"identity_{err.code}"
        except Exception as err:  # noqa: BLE001 (any reason the file cannot be read: it is kept as it is)
            if self._broken != "store":
                _LOGGER.warning(
                    "The Nucleum Hub licence store could not be read (%s); it is left as it is, the dashboard stays "
                    "fully usable, and it is read again every hour",
                    type(err).__name__,
                )
            self._broken = "store"
        else:
            self._broken = None

    async def async_stop(self) -> None:
        """Nothing is deleted: the store stays for the next setup (a reload, a reinstall). A request already on the
        wire is let finish and keep its answer (up to its timeout); one still waiting for the lock is never sent."""
        self._stopped = True
        if self._unsub_wake is not None:
            self._unsub_wake()
            self._unsub_wake = None
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._exchanges:
            await asyncio.wait(list(self._exchanges), timeout=STOP_WAIT)

    # --- what others read -------------------------------------------------------------------------------------------
    @property
    def broken(self) -> str | None:
        """Why the licence cannot judge (build, store, identity_<code>), or None."""
        return self._broken

    @property
    def holds_licence(self) -> bool:
        """A licence is bound to this home, as far as it knows: it can be checked and released."""
        return self._broken is None and self._held.licensed

    @property
    def role(self) -> str | None:
        return self._held.role

    @property
    def moves_left(self) -> int | None:
        return self._held.moves_left

    @property
    def checked(self) -> int | None:
        """The last time the server answered a refresh or an activation."""
        return self._held.checked if self._held.licensed else None

    @property
    def due(self) -> float | None:
        """When the next request of the schedule goes (the trial or a refresh), or None."""
        return self._due

    @property
    def trial_pending(self) -> bool:
        """The first start's trial is on its way: no token yet, and its request has not been answered or failed. The
        dashboard can then say so instead of showing the activation screen for those seconds."""
        return self._wants_trial() and self._held.tried.get("trial") is None

    @property
    def lost(self) -> bool:
        """The server said this home holds a licence that this Home Assistant has nothing of (its store was damaged or
        removed): the key is asked for again, which brings the same binding back and is not a move."""
        return self._broken is None and self._held.lost and not self._held.licensed

    def payload(self, language: Any = None) -> dict[str, Any]:
        """What the dashboard is told (``wall_dashboard/licence``): the state (state, reason, until, plan, role,
        grace, notice), Home Assistant's time (``now``), the link to show with its kind (buy, renew, payment; None
        while the site's address is a placeholder or the state has none), ``cloned`` ("the same licence runs in two
        homes; move one", never a block), ``lost`` ("the licence data on this Home Assistant was lost: enter your key
        again"; no link to buy another then) and ``trial_pending``. Never the home id, the nonce, the token or lic
        (D-067)."""
        now = self._clock()
        said = self._said_now(now)
        kind_page = None if said["lost"] else LINKS.get((said["state"], said["reason"]))
        return {
            **said,
            "now": int(now),
            "link": site_link(kind_page[1], language) if kind_page else None,
            "link_kind": kind_page[0] if kind_page else None,
        }

    def _said_now(self, now: float) -> dict[str, Any]:
        """The payload without what does not change with the state (``now``, the link's language)."""
        return {**self._result(now), "cloned": self.holds_licence and self._held.cloned, "lost": self.lost,
                "trial_pending": self.trial_pending}

    # --- what the admin asks for (the options flow) ---------------------------------------------------------------
    async def async_activate(self, key: str, role: str = "prod", *, move: bool = False) -> Outcome:
        """Bind the licence of ``key`` to this home in ``role`` (prod or staging). ``move`` only after the admin said
        yes to "active in another home: move it here?" (Outcome active_elsewhere)."""
        if self._broken is not None:
            return Outcome("unavailable")
        if not is_role(role):
            role = "prod"
        return await self._exchange(self._activate(key, role, move))

    async def _activate(self, key: str, role: str, move: bool) -> Outcome:
        async with self._lock:
            if self._stopped:
                return Outcome("unavailable")
            now = self._clock()
            try:
                iidh = await self._identity.async_iidh()
            except IdentityUnavailable:
                return Outcome("identity")
            for attempt in range(2):
                try:
                    answer = await self._api.activate(key, self._home, iidh, role=role, version=self._version,
                                                      move=move)
                    break
                except Conflict:
                    if attempt:
                        return Outcome("conflict")  # another request changed the binding meanwhile: once more only
                except LicenceApiError as err:
                    return _api_error(err)
            held = self._held
            held.lic, held.role, held.nonce = answer.lic, answer.role, answer.nonce
            held.trial = True  # a home with a licence never needs the trial
            held.lost = False
            held.cloned, held.bound, held.checked = False, True, int(now)
            held.failures.pop("refresh", None)
            held.not_before.pop("refresh", None)
            error = self._accept_token(answer.token, now, lic=answer.lic)
            await self._async_save()
            self._check_clock(answer.now, now)
            self._plan()
            self._arm()
            self._changed()
        return Outcome() if error is None else Outcome("invalid_token")

    async def async_release(self) -> Outcome:
        """"Free this home": the licence can then be activated elsewhere (one of the 3 moves a year for the production
        home). The token and the nonce go; a home the server says holds nothing is freed here too."""
        if self._broken is not None:
            return Outcome("unavailable")
        return await self._exchange(self._release())

    async def _release(self) -> Outcome:
        async with self._lock:
            if self._stopped:
                return Outcome("unavailable")
            held = self._held
            if held.lic is None:
                return Outcome()
            moves_left = held.moves_left
            for attempt in range(2):
                try:
                    answer = await self._api.release(self._home, held.lic)
                    moves_left = answer.moves_left
                    break
                except Conflict:
                    if attempt:
                        return Outcome("conflict")
                except NotBound:
                    break  # nothing bound there: the admin wants this home free, and it is
                except LicenceApiError as err:
                    return _api_error(err)
            held.forget_licence()
            held.moves_left = moves_left
            await self._async_save()
            self._plan()
            self._arm()
            self._changed()
        return Outcome(moves_left=moves_left)

    async def async_check(self) -> Outcome:
        """"Check the licence now" in the options flow: a refresh at once (within the budget and any Retry-After),
        or the trial when the first start could not get it."""
        if self._broken is not None:
            return Outcome("unavailable")
        if self._held.licensed:
            return await self._async_refresh(forced=True)
        if self._wants_trial():
            return await self._async_trial(forced=True)
        return Outcome("not_bound")

    @callback
    def async_request_check(self) -> None:
        """The settings were opened (the options flow, or the dashboard's own, server/README.md §10.9): a refresh in
        the background when the last request is older than SETTINGS_GAP and the budget allows it."""
        if self._broken is not None or self._stopped:
            return
        now = self._clock()
        kind = "refresh" if self._held.licensed else "trial" if self._wants_trial() else None
        if kind is None:
            return
        last = self._held.tried.get(kind)
        if last is not None and now - schedule.SETTINGS_GAP < last <= now:
            return
        if not self._budget(kind).allows(now) or self._held.not_before.get(kind, 0) > now:
            return
        if kind == "refresh":
            self._spawn(self._async_refresh(forced=True), "nucleum_hub_licence_refresh")
        else:
            self._spawn(self._async_trial(forced=True), "nucleum_hub_licence_trial")

    # --- the requests -----------------------------------------------------------------------------------------------
    def _wants_trial(self) -> bool:
        held = self._held
        return self._broken is None and not held.trial and held.token is None and not held.licensed

    async def _async_trial(self, *, forced: bool = False) -> Outcome:
        """The trial's token (D-065: 60 minutes from the first start, once per installation and per home)."""
        return await self._exchange(self._trial(forced))

    async def _trial(self, forced: bool) -> Outcome:
        async with self._lock:
            if self._stopped:
                return Outcome("unavailable")
            if not self._wants_trial():
                return Outcome()
            now = self._clock()
            wait = self._wait(now, "trial", forced)
            if wait is not None:
                self._plan()
                self._arm()
                return wait
            outcome = Outcome()
            server_now: int | None = None
            try:
                iidh = await self._identity.async_iidh()
                answer = await self._api.trial(self._home, iidh)
            except IdentityUnavailable as err:
                # Home Assistant gave no instance id or has no owner yet: nothing was sent, asked again on the backoff
                _LOGGER.debug("Nucleum Hub trial: no identity yet (%s)", err.code)
                self._sent("trial", now, counted=False)
                self._fail("trial", now, transient=True)
                outcome = Outcome("identity")
            except (TrialUsed, TrialLicensed) as err:
                # the server keeps the trial (once per installation and per home)
                self._sent("trial", now)
                self._held.trial = True
                self._held.failures.pop("trial", None)
                if isinstance(err, TrialLicensed):
                    # the server holds a licence for this home, and this Home Assistant has nothing of it: its store
                    # was lost (a damaged file Home Assistant moved aside, or one removed). Only the key brings it
                    # back (the same binding with a new nonce, not a move), so the admin is told that, not "buy one"
                    self._held.lost = True
                    _LOGGER.warning("This home holds a Nucleum Hub licence, but what this Home Assistant kept of it "
                                    "was lost; enter the key again in Settings › Devices & services › Nucleum Hub › "
                                    "Configure (it does not count as a move)")
                outcome = Outcome(err.code)
            except LicenceApiError as err:
                self._sent("trial", now, counted=not isinstance(err, ApiNotConfigured))
                self._fail("trial", now, err=err)
                outcome = _api_error(err)
            else:
                self._sent("trial", now)
                server_now = answer.now
                if self._accept_token(answer.token, now, trial=True) is None:
                    self._held.trial = True
                    self._held.failures.pop("trial", None)
                else:
                    # kept unanswered: while the trial lasts the server hands the same trial over again, so a build
                    # whose clock or roots were wrong still gets it once they are right; the slow rhythm meanwhile
                    self._fail("trial", now)
                    outcome = Outcome("invalid_token")
            await self._async_save()
            if server_now is not None:
                self._check_clock(server_now, now)
            self._plan()
            self._arm()
            self._changed()
            return outcome

    async def _async_refresh(self, *, forced: bool = False) -> Outcome:
        """The next token, a new nonce and the licence's statements (the 200), or the statements an error carries."""
        return await self._exchange(self._refresh(forced))

    async def _refresh(self, forced: bool) -> Outcome:
        async with self._lock:
            if self._stopped:
                return Outcome("unavailable")
            held = self._held
            if not held.licensed or self._broken is not None:
                return Outcome("not_bound")
            now = self._clock()
            wait = self._wait(now, "refresh", forced)
            if wait is not None:
                self._plan()
                self._arm()
                return wait
            server_now: int | None = None
            try:
                answer = await self._api.refresh(self._home, held.lic, held.nonce, version=self._version)
            except LicenceApiError as err:
                self._sent("refresh", now, counted=not isinstance(err, ApiNotConfigured))
                self._keep_statements(err.revocations)
                outcome = _api_error(err)
                if isinstance(err, RateLimited) or err.transient:
                    self._fail("refresh", now, err=err)
                else:
                    # an answer, not a failure: the rhythm goes on from here (a subscription that ended, a licence
                    # refunded or moved away); the token held keeps what it says until its own end
                    held.checked = int(now)
                    held.failures.pop("refresh", None)
                    if isinstance(err, NotBound):
                        held.bound = False
                    if isinstance(err, RequestRejected):
                        _LOGGER.warning("The licence server refused the refresh as malformed (%s); it is asked again "
                                        "later", err.code)
            else:
                self._sent("refresh", now)
                held.nonce = answer.nonce  # the server rotated it: the next refresh must send this one
                held.role, held.cloned, held.moves_left = answer.role, answer.cloned, answer.moves_left
                held.checked, held.bound = int(now), True
                held.failures.pop("refresh", None)
                held.not_before.pop("refresh", None)
                self._keep_statements(answer.revocations)
                server_now = answer.now
                outcome = Outcome()
                if self._accept_token(answer.token, now, lic=held.lic) is not None:
                    outcome = Outcome("invalid_token")
            await self._async_save()
            if server_now is not None:
                self._check_clock(server_now, now)
            self._plan()
            self._arm()
            self._changed()
            return outcome

    async def _exchange(self, unit: Coroutine[Any, Any, Outcome]) -> Outcome:
        """One request, from taking the lock to saving what came back, in a task of its own: a caller that stops
        waiting (the entry unloading, Home Assistant stopping, the settings' dialog closed) does not cancel a request
        already on the wire, so its answer is still kept. Lost, a nonce the server rotated would make the next refresh
        send a stale one, which the server counts towards "cloned" (server/README.md §10.9). Not a background task:
        Home Assistant cancels those as it stops. async_stop waits for it."""
        task = self._hass.async_create_task(unit, "nucleum_hub_licence_request")
        self._exchanges.add(task)
        task.add_done_callback(self._exchanges.discard)
        return await asyncio.shield(task)

    def _wait(self, now: float, kind: str, forced: bool) -> Outcome | None:
        """rate_limited when this request would go over the budget or before the server's Retry-After (or, for a
        request nobody asked for, closer than MIN_GAP to the last); None when it may go."""
        free = max(self._budget(kind).next_free(now), self._held.not_before.get(kind, 0))
        last = self._held.tried.get(kind)
        if not forced and last is not None and last <= now:
            free = max(free, last + schedule.MIN_GAP)
        return Outcome("rate_limited", minutes=_minutes(free - now)) if free > now else None

    def _sent(self, kind: str, now: float, *, counted: bool = True) -> None:
        """A request of ``kind`` was tried (``counted`` False: nothing reached the server, the address being the
        placeholder or the identity missing, so the budget is not spent)."""
        self._held.tried[kind] = int(now)
        if counted:
            budget = self._budget(kind)
            budget.note(now)
            self._held.sent[kind] = budget.stored()

    def _fail(self, kind: str, now: float, *, err: LicenceApiError | None = None, transient: bool = False) -> None:
        held = self._held
        if isinstance(err, RateLimited):
            held.not_before[kind] = int(now + err.retry_after)
            return
        if transient or (err is not None and err.transient):
            held.failures[kind] = held.failures.get(kind, 0) + 1
        else:
            # our own request was wrong (RequestRejected), the server changed, or its token did not verify here:
            # asked again on the slow rhythm (the backoff of 7 failures, about 5 hours)
            held.failures[kind] = max(held.failures.get(kind, 0) + 1, 7)

    def _budget(self, kind: str) -> schedule.Budget:
        return schedule.Budget(*schedule.BUDGETS[kind], self._held.sent.get(kind, ()))

    # --- tokens and statements --------------------------------------------------------------------------------------
    def _accept_token(self, token: str, now: float, *, lic: str | None = None, trial: bool = False) -> str | None:
        """Store ``token`` when it verifies at now for this home (and this licence, or as the trial's): None, or why
        it was not stored. The token held stays when a new one is refused."""
        verified = self._verifier.verify_token(token, now)
        error = verified["error"]
        if verified["ok"]:
            claims = verified["claims"]
            ours = claims["home"] == self._home and (claims["plan"] == "trial") == trial
            if not ours or (not trial and claims["lic"] != lic):
                error = "not_ours"
        if error is not None:
            _LOGGER.warning("A licence token from the server did not verify on this Home Assistant (%s); the one "
                            "held is kept", error)
            if error == "from_future":
                _LOGGER.warning("Home Assistant's clock seems to be behind: the licence server's tokens are dated "
                                "after it; check the time and the time zone in Settings › System › General")
            return error
        self._held.token = token
        self._held.lost = False
        self._token_cache[token] = verified
        return None

    def _keep_statements(self, statements: tuple[str, ...]) -> bool:
        """Add the statements that verify and concern this licence and this home (home null: every home of the
        licence). True when one was new."""
        added = False
        lic = self._held.lic
        for statement in statements:
            if statement in self._held.revocations:
                continue
            revocation = self._statement(statement)
            if (revocation is None or revocation["home"] not in (None, self._home)
                    or (lic is not None and revocation["lic"] != lic)):
                continue
            self._held.revocations.append(statement)
            added = True
        if len(self._held.revocations) > MAX_STATEMENTS:
            del self._held.revocations[:-MAX_STATEMENTS]
        return added

    def _statement(self, statement: str) -> dict[str, Any] | None:
        if statement not in self._statement_cache:
            verified = self._verifier.verify_revocation(statement)
            self._statement_cache[statement] = verified["revocation"] if verified["ok"] else None
        return self._statement_cache[statement]

    def _verified(self, now: float) -> dict[str, Any] | None:
        """The held token, verified (once: only a token refused as dated after now is verified again)."""
        token = self._held.token
        if token is None:
            return None
        cached = self._token_cache.get(token)
        if cached is None or cached["error"] == "from_future":
            cached = self._verifier.verify_token(token, now)
            self._token_cache[token] = cached
        return cached

    def _claims(self, now: float) -> dict[str, Any] | None:
        verified = self._verified(now)
        return verified["claims"] if verified is not None and verified["ok"] else None

    def _result(self, now: float) -> dict[str, Any]:
        if self._broken is not None:
            return dict(BROKEN)
        try:
            revocations = [r for r in map(self._statement, self._held.revocations) if r is not None]
            context = {"now": now, "build": self._build, "home": self._home, "revocations": revocations}
            return state_of_verified(self._verified(now), context, self._verifier.self_test)
        except Exception as err:  # noqa: BLE001 (only a mistake of ours gets here: never the blocking screen for it)
            if not self._rule_failed:
                self._rule_failed = True
                _LOGGER.error("The Nucleum Hub licence state could not be computed (%s); the dashboard stays fully "
                              "usable", type(err).__name__)
            return dict(BROKEN)

    def _check_clock(self, server_now: int, now: float) -> None:
        if not self._clock_warned and abs(server_now - now) > schedule.HOUR:
            self._clock_warned = True
            _LOGGER.warning("Home Assistant's clock differs from the licence server's by more than an hour; the "
                            "licence follows Home Assistant's, so check the time in Settings › System › General")

    # --- the schedule -------------------------------------------------------------------------------------------------
    def _plan(self, *, starting: bool = False) -> None:
        """When the next request goes (the trial, or the next refresh), or None."""
        now = self._clock()
        held = self._held
        if self._broken is not None:
            self._due = None
        elif held.licensed:
            due, spread = schedule.planned(self._claims(now), held.role, checked=held.checked, now=now,
                                           bound=held.bound)
            self._due = schedule.next_attempt(due, spread, self._attempt("refresh"), self._budget("refresh"),
                                              now=now, rng=self._rng, starting=starting)
        elif self._wants_trial():
            self._due = schedule.next_attempt(now + schedule.TRIAL_DELAY, 0.0, self._attempt("trial"),
                                              self._budget("trial"), now=now, rng=self._rng)
        else:
            self._due = None

    def _attempt(self, kind: str) -> schedule.Attempt:
        held = self._held
        return schedule.Attempt(held.tried.get(kind), held.failures.get(kind, 0), held.not_before.get(kind))

    def _next_change(self, now: float) -> float | None:
        """The next moment the state can change by time alone: a period's end, a grace's end, the moment a
        permanent one-time token is old enough for verify_security."""
        claims = self._claims(now)
        if claims is None:
            return None
        window = window_of(claims)
        moments = [window.full_until, window.grace_until, claims["exp"]]
        if claims["exp"] is None:
            moments.append(claims["iat"] + LIFE_VERIFY_S + 1)
        future = [m for m in moments if m is not None and m > now]
        return min(future) + 1 if future else None

    def _arm(self) -> None:
        """One timer, for the earliest of: the next request, the next change of the state, an hour from now."""
        if self._unsub_wake is not None:
            self._unsub_wake()
            self._unsub_wake = None
        if self._stopped:
            return
        now = self._clock()
        moments = [now + WAKE_MAX, self._due, self._next_change(now)]
        when = min(m for m in moments if m is not None)
        self._unsub_wake = async_call_later(self._hass, max(1.0, when - now), self._on_wake)

    @callback
    def _on_wake(self, _now: Any) -> None:
        self._unsub_wake = None
        if not self._stopped:
            self._spawn(self._async_wake(), "nucleum_hub_licence")

    async def _async_wake(self) -> None:
        try:
            if self._broken is not None and self._broken != "build":
                await self._async_load()  # the store or the home id again: readable now, the licence carries on
                if self._broken is None:
                    self._plan(starting=True)
            now = self._clock()
            if self._due is not None and now >= self._due:
                if self._held.licensed:
                    await self._async_refresh()
                elif self._wants_trial():
                    await self._async_trial()
            self._changed()
        except Exception as err:  # noqa: BLE001 (the schedule must go on: the next wake tries again)
            _LOGGER.error("The Nucleum Hub licence could not check its state (%s); it tries again later",
                          type(err).__name__)
        finally:
            if self._unsub_wake is None:
                self._arm()

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = self._hass.async_create_background_task(coro, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- telling the dashboard ----------------------------------------------------------------------------------------
    def _changed(self) -> None:
        """Tell the WebSocket subscriptions (websocket.py) when what they would send has changed."""
        if self._stopped:
            return
        said = self._said_now(self._clock())
        if said != self._said:
            self._said = said
            async_dispatcher_send(self._hass, SIGNAL_LICENCE)

    async def _async_save(self) -> None:
        try:
            await self._storage.async_save(self._held)
        except Exception as err:  # noqa: BLE001 (a full disk: the licence goes on from memory)
            _LOGGER.warning("The Nucleum Hub licence store could not be saved (%s); the licence goes on from memory",
                            type(err).__name__)
