"""The licence server's client (server/README.md §10.9): /v1/trial, /v1/activate, /v1/refresh, /v1/release.

- Home Assistant's shared HTTP session (``async_get_clientsession``): its TLS checks and its user agent,
  nothing that names this product on the connection. POST with a JSON body, never an Origin header (the
  server refuses a request that has one: only a browser sends it), no cookies, nothing in the address.
  Redirects are not followed: the API never sends one, so a redirect is a network that is not the server
  (a captive portal, a filtering proxy) and it gets nothing.
- One timeout (contract.TIMEOUT) for the whole request. No retries here: when to ask again belongs to the
  caller's schedule (plan §4.8 p.5), which reads ``err.transient`` and, after a 429, ``err.retry_after``.
- Every error code of the contract is an exception below, carrying the facts the server sends with it (who
  holds the licence and when it was last seen, the moves left, the revocation statements). The statements
  are only carried: nothing unsigned ever changes the licence's state (plan §4.8 p.8); the verifier decides
  which of them apply.
- Never logged and never in an exception: the key, the home id, the instance's hash, the nonce, a token, a
  body. The debug line has the route, the HTTP status and the error code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import hdrs

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .contract import (
    API_BASE,
    LICENCE_STATUSES,
    MAX_ANSWER_BYTES,
    MAX_REQUEST_BYTES,
    RETRY_AFTER_DEFAULT,
    RETRY_AFTER_MAX,
    ROUTE_ACTIVATE,
    ROUTE_REFRESH,
    ROUTE_RELEASE,
    ROUTE_TRIAL,
    TIMEOUT,
    is_day,
    is_home_id,
    is_iidh,
    is_lic,
    is_nonce,
    is_role,
    is_statement,
    is_version,
    key_to_send,
    key_well_formed,
)
from .state import is_moment

# The e-mail at the trial (D-095): what the server takes, so that a field it would refuse (400 invalid) never stops the
# trial: an address as server/worker/validate.mjs reads it, the 12 languages, the box's text version.
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@(?:(?!-)[A-Za-z0-9-]{1,63}(?<!-)\.)+(?:[A-Za-z]{2,63}|xn--[A-Za-z0-9-]{2,59})$")
_LANGS = ("en", "ro", "de", "fr", "nl", "es", "it", "pl", "sv", "cs", "da", "pt-BR")
_CONSENT = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")


def trial_contact(contact: dict[str, Any] | None) -> dict[str, Any]:
    """The optional fields of the trial's request (D-095) from what the dashboard sent: the address only when the server
    would take it, the language when it is one of the 12, the news box only with an address and its text's version."""
    if not contact:
        return {}
    email = contact.get("email")
    email = email.strip() if isinstance(email, str) else ""
    local = email.split("@")[0] if "@" in email else ""
    if not (6 <= len(email) <= 254 and _EMAIL.match(email) and not local.startswith(".") and not local.endswith(".") and ".." not in local):
        return {}
    out: dict[str, Any] = {"email": email}
    lang = contact.get("lang")
    if isinstance(lang, str):
        found = next((code for code in _LANGS if code.lower() == lang.strip().replace("_", "-").lower()), None)
        if found:
            out["lang"] = found
    version = contact.get("consent_version")
    if contact.get("news") is True and isinstance(version, str) and _CONSENT.match(version):
        out["news"] = True
        out["consent_version"] = version
    return out

_LOGGER = logging.getLogger(__name__)

_HEADERS = {hdrs.CONTENT_TYPE: "application/json", hdrs.ACCEPT: "application/json"}
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


# --- Errors ------------------------------------------------------------------------------------------------
class LicenceApiError(Exception):
    """A request that brought no answer the integration can use as it is.

    ``code`` is the server's error code, or one of ours when there was no usable answer; ``http_status`` the
    HTTP status, when there was one; ``transient`` whether asking again later can change anything (keep
    what is held meanwhile, server/README.md §10.9); ``revocations`` the statements the server handed over
    with the refusal, unverified. The message is the code and the status only.
    """

    code = "error"
    transient = False

    def __init__(self, code: str | None = None, *, http_status: int | None = None,
                 revocations: tuple[str, ...] = ()) -> None:
        self.code = code or type(self).code
        self.http_status = http_status
        self.revocations = revocations
        super().__init__(self.code if http_status is None else f"{self.code} (HTTP {http_status})")

    @classmethod
    def from_answer(cls, code: str, http_status: int, body: dict[str, Any]) -> LicenceApiError:
        return cls(code, http_status=http_status, revocations=_statements(body.get("revocations")))


class ApiUnreachable(LicenceApiError):
    """No answer at all: the name did not resolve, no connection, TLS refused, or the timeout ran out.
    ``detail`` says which (an exception's type name, or "timeout")."""

    code = "unreachable"
    transient = True

    def __init__(self, code: str | None = None, *, detail: str = "", **kwargs: Any) -> None:
        super().__init__(code, **kwargs)
        self.detail = detail


class ApiNotConfigured(ApiUnreachable):
    """The server's address is still the placeholder (contract.API_BASE): nothing was sent."""

    code = "not_configured"


class ApiUnavailable(LicenceApiError):
    """The server answered, but not with something usable: 500 ``server_error``, 503 ``unavailable`` (a
    setting of the server to fix), or an answer outside the contract (``bad_answer``: not JSON, a field of
    the wrong shape, a proxy's page; ``answer_too_large``)."""

    code = "unavailable"
    transient = True


class RateLimited(LicenceApiError):
    """429 ``rate_limited``: ask again after ``retry_after`` seconds (the Retry-After header)."""

    code = "rate_limited"
    transient = True

    def __init__(self, code: str | None = None, *, retry_after: int = RETRY_AFTER_DEFAULT, **kwargs: Any) -> None:
        super().__init__(code, **kwargs)
        self.retry_after = retry_after


class RequestRejected(LicenceApiError):
    """The request itself was wrong (the integration's mistake, or a server that changed): 400 ``invalid``
    with ``fields`` (the names of the wrong fields, never their values), ``invalid_json``, ``invalid_body``,
    413 ``too_large``, 415 ``unsupported_media_type``, 403 ``origin_not_allowed``, 404 ``not_found``, 405
    ``method_not_allowed``, 409 ``update_required`` (a trial request without ``ask: true``, which this build always
    sends: D-090), or any other 4xx code this version does not know."""

    code = "invalid"

    def __init__(self, code: str | None = None, *, fields: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(code, **kwargs)
        self.fields = fields

    @classmethod
    def from_answer(cls, code: str, http_status: int, body: dict[str, Any]) -> LicenceApiError:
        fields = _fields(body.get("fields"))
        kind = KeyMistyped if code == "invalid" and "key" in fields else cls
        return kind(code, http_status=http_status, fields=fields,
                    revocations=_statements(body.get("revocations")))


class KeyMistyped(RequestRejected):
    """The key is mistyped ("the key is written wrong"): its check character does not hold. Found before any
    request when possible, so a mistyped key never leaves the house; the server's ``invalid`` with
    ``fields: ["key"]`` comes here too."""

    def __init__(self, code: str | None = None, *, fields: tuple[str, ...] = ("key",), **kwargs: Any) -> None:
        super().__init__(code, fields=fields, **kwargs)


class LicenceRefused(LicenceApiError):
    """The server answered and decided: the answers of the licence itself (below)."""


class TrialLicensed(LicenceRefused):
    """409 ``licensed`` (trial): the home holds a licence that would get a token now; refresh it instead."""

    code = "licensed"


class TrialUsed(LicenceRefused):
    """409 ``trial_used``: the trial is over, or was used on this installation or by this home."""

    code = "trial_used"


class UnknownKey(LicenceRefused):
    """404 ``unknown_key`` (activate): no licence has this key."""

    code = "unknown_key"


class LicenceInactive(LicenceRefused):
    """403 ``licence_inactive``: the licence was refunded, revoked or disputed (``licence_status``)."""

    code = "licence_inactive"

    def __init__(self, code: str | None = None, *, licence_status: str | None = None, **kwargs: Any) -> None:
        super().__init__(code, **kwargs)
        self.licence_status = licence_status

    @classmethod
    def from_answer(cls, code: str, http_status: int, body: dict[str, Any]) -> LicenceApiError:
        status = body.get("status")
        return cls(code, http_status=http_status, revocations=_statements(body.get("revocations")),
                   licence_status=status if status in LICENCE_STATUSES else None)


class SubscriptionEnded(LicenceRefused):
    """403 ``subscription_ended``: the subscription is over; the token held keeps working to its exp."""

    code = "subscription_ended"


class PlanUnsupported(LicenceRefused):
    """403 ``plan_unsupported`` (activate): a plan from before D-064, never sold; support handles it."""

    code = "plan_unsupported"


class ActiveElsewhere(LicenceRefused):
    """409 ``active_elsewhere`` (activate): another home holds the licence in this ``role``, last seen on
    ``last_seen`` (YYYY-MM-DD); ``moves_left`` (None for staging) and ``can_move``. Asked "move it here?" and
    answered yes, the same request goes again with ``move=True``."""

    code = "active_elsewhere"

    def __init__(self, code: str | None = None, *, role: str | None = None, last_seen: str | None = None,
                 moves_left: int | None = None, can_move: bool = False, **kwargs: Any) -> None:
        super().__init__(code, **kwargs)
        self.role = role
        self.last_seen = last_seen
        self.moves_left = moves_left
        self.can_move = can_move

    @classmethod
    def from_answer(cls, code: str, http_status: int, body: dict[str, Any]) -> LicenceApiError:
        return cls(code, http_status=http_status, role=_role(body.get("role")),
                   last_seen=body.get("last_seen") if is_day(body.get("last_seen")) else None,
                   moves_left=_moves(body.get("moves_left")), can_move=body.get("can_move") is True)


class MoveLimit(LicenceRefused):
    """409 ``move_limit`` (activate, release): the 3 moves of the last 365 days are used; support moves it."""

    code = "move_limit"

    def __init__(self, code: str | None = None, *, role: str | None = None, moves_left: int | None = 0,
                 **kwargs: Any) -> None:
        super().__init__(code, **kwargs)
        self.role = role
        self.moves_left = moves_left

    @classmethod
    def from_answer(cls, code: str, http_status: int, body: dict[str, Any]) -> LicenceApiError:
        return cls(code, http_status=http_status, role=_role(body.get("role")),
                   moves_left=_moves(body.get("moves_left")))


class HomeHasLicence(LicenceRefused):
    """409 ``home_has_licence`` (activate): this home holds another licence still in force; release it first."""

    code = "home_has_licence"


class RoleConflict(LicenceRefused):
    """409 ``role_conflict`` (activate): the home holds the licence in the other ``role``; a home has one."""

    code = "role_conflict"

    def __init__(self, code: str | None = None, *, role: str | None = None, **kwargs: Any) -> None:
        super().__init__(code, **kwargs)
        self.role = role

    @classmethod
    def from_answer(cls, code: str, http_status: int, body: dict[str, Any]) -> LicenceApiError:
        return cls(code, http_status=http_status, role=_role(body.get("role")))


class Conflict(LicenceRefused):
    """409 ``conflict`` (activate, release): another request changed the binding meanwhile; ask once more."""

    code = "conflict"
    transient = True


class NotBound(LicenceRefused):
    """409 ``not_bound`` (refresh, release): this home does not hold the licence (moved or released from
    elsewhere). On a refresh, ``revocations`` carries the statement that ended its binding, if it had one."""

    code = "not_bound"


# The server's codes for a 4xx answer, each with its exception. Any other 4xx code is a RequestRejected.
_ERRORS: dict[str, type[LicenceApiError]] = {
    cls.code: cls
    for cls in (TrialLicensed, TrialUsed, UnknownKey, LicenceInactive, SubscriptionEnded, PlanUnsupported,
                ActiveElsewhere, MoveLimit, HomeHasLicence, RoleConflict, Conflict, NotBound)
}
_SERVER_CODES = ("unavailable", "server_error")


# --- Answers ---------------------------------------------------------------------------------------------
# The token and the nonce are kept out of the repr, so an answer printed while debugging shows neither.
@dataclass(frozen=True)
class TrialAnswer:
    token: str = field(repr=False)
    now: int  # the server's clock, unix seconds (every 200 has it)


@dataclass(frozen=True)
class ActivateAnswer:
    token: str = field(repr=False)
    nonce: str = field(repr=False)
    lic: str
    role: str
    now: int


@dataclass(frozen=True)
class RefreshAnswer:
    token: str = field(repr=False)
    nonce: str = field(repr=False)
    role: str
    revocations: tuple[str, ...] = field(repr=False)
    cloned: bool  # "the same licence runs in two homes; move one" (never a block, plan §4.3)
    moves_left: int | None  # None for the staging instance
    now: int


@dataclass(frozen=True)
class ReleaseAnswer:
    role: str
    moves_left: int | None
    now: int


# --- Reading answers -------------------------------------------------------------------------------------
def _statements(value: Any) -> tuple[str, ...]:
    """The statements of a list, each only if it has a statement's shape (the verifier judges the rest)."""
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if is_statement(item))


def _fields(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value[:16] if isinstance(item, str) and 0 < len(item) <= 40)


def _role(value: Any) -> str | None:
    return value if is_role(value) else None


def _moves(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000 else None


def _retry_after(value: Any) -> int:
    """Retry-After in seconds (the only form the server sends), within 1 s and a day; 60 otherwise."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value.strip()):
        return min(max(int(value.strip()), 1), RETRY_AFTER_MAX)
    return RETRY_AFTER_DEFAULT


def _json(raw: bytes) -> dict[str, Any] | None:
    """The answer as a JSON object (strict UTF-8), or None when it is not one."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _error(http_status: int, body: dict[str, Any] | None, retry_after: Any) -> LicenceApiError:
    """The exception for an answer that is not a 200."""
    code = body.get("error") if body is not None else None
    if http_status == 429:
        return RateLimited(http_status=http_status, retry_after=_retry_after(retry_after))
    if http_status >= 500:
        return ApiUnavailable(code if code in _SERVER_CODES else None, http_status=http_status)
    if 400 <= http_status < 500 and body is not None and isinstance(code, str) and code:
        return _ERRORS.get(code, RequestRejected).from_answer(code, http_status, body)
    return ApiUnavailable("bad_answer", http_status=http_status)


class _Answer:
    """A 200's fields, each checked as it is taken: one of the wrong shape makes the whole answer a
    ``bad_answer``, and nothing of it is used."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def take(self, name: str, ok: Any) -> Any:
        value = self.body.get(name)
        if not ok(value):
            raise ApiUnavailable("bad_answer", http_status=200)
        return value

    def now(self) -> int:
        """The server's clock: a moment in unix seconds, as the state rule reads one (state.is_moment). Python reads
        a JSON number of thousands of digits as an int, which no clock arithmetic survives."""
        return self.take("now", lambda v: isinstance(v, int) and not isinstance(v, bool) and is_moment(v))

    def moves_left(self) -> int | None:
        return self.take("moves_left", lambda v: v is None or _moves(v) is not None)


async def _read_capped(resp: aiohttp.ClientResponse) -> bytes:
    """The answer's body, at most MAX_ANSWER_BYTES."""
    if resp.content_length is not None and resp.content_length > MAX_ANSWER_BYTES:
        raise ApiUnavailable("answer_too_large", http_status=resp.status)
    body = bytearray()
    async for chunk in resp.content.iter_chunked(16 * 1024):
        body.extend(chunk)
        if len(body) > MAX_ANSWER_BYTES:
            raise ApiUnavailable("answer_too_large", http_status=resp.status)
    return bytes(body)


def _check_base(base: str) -> tuple[str, bool]:
    """``(the base without its last slash, whether it is the placeholder)``. HTTPS only; plain HTTP only to
    this machine (a development server, server/dev/serve.mjs). Nothing but a scheme and a host (and a port)."""
    parts = urlsplit(base)
    host = (parts.hostname or "").lower()
    secure = parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK)
    if not secure or not host or parts.username or parts.password or parts.query or parts.fragment \
            or parts.path not in ("", "/"):
        raise ValueError("the licence server's address must be https://<host>")
    return base.rstrip("/"), host == "invalid" or host.endswith(".invalid")


def _invalid(**checks: bool) -> None:
    """A field of ours that the server would refuse: raised before anything is sent."""
    wrong = tuple(name for name, ok in checks.items() if not ok)
    if wrong:
        raise RequestRejected("invalid", fields=wrong)


# --- The client ------------------------------------------------------------------------------------------
class LicenceApi:
    """The four routes, on the session given (Home Assistant's shared one: async_get_api)."""

    def __init__(self, session: aiohttp.ClientSession, base: str = API_BASE, timeout: float = TIMEOUT) -> None:
        self._session = session
        self._base, self._placeholder = _check_base(base)
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        """False while the server's address is the placeholder: every request raises ApiNotConfigured."""
        return not self._placeholder

    async def trial(self, home: str, iidh: str, contact: dict[str, Any] | None = None) -> TrialAnswer:
        """The free trial's token (D-090: 3 days from the server's yes, once per installation and per home), asked
        only when a Home Assistant admin starts it from the dashboard. ``ask: true`` says so: the server answers a
        request without it (the 1.0 builds that asked by themselves at the first start) with 409
        ``update_required`` and uses nothing up. ``contact``: the optional e-mail and news box of D-095, only what the
        server takes (trial_contact)."""
        _invalid(home=is_home_id(home), iidh=is_iidh(iidh))
        answer = _Answer(await self._post(ROUTE_TRIAL, {"home": home, "iidh": iidh, "ask": True, **trial_contact(contact)}))
        return TrialAnswer(token=answer.take("token", is_statement), now=answer.now())

    async def activate(self, key: str, home: str, iidh: str, *, role: str = "prod", version: str | None = None,
                       move: bool = False) -> ActivateAnswer:
        """Bind the licence of ``key`` to this home in ``role``: its first token and nonce. ``move=True`` only
        after the admin said yes to "it is active in another home; move it here?" (ActiveElsewhere)."""
        if not key_well_formed(key):
            raise KeyMistyped()
        _invalid(home=is_home_id(home), iidh=is_iidh(iidh), role=is_role(role),
                 version=version is None or is_version(version), move=isinstance(move, bool))
        payload: dict[str, Any] = {"key": key_to_send(key), "home": home, "iidh": iidh, "role": role}
        if version is not None:
            payload["version"] = version
        if move:
            payload["move"] = True
        answer = _Answer(await self._post(ROUTE_ACTIVATE, payload))
        return ActivateAnswer(token=answer.take("token", is_statement), nonce=answer.take("nonce", is_nonce),
                              lic=answer.take("lic", is_lic), role=answer.take("role", is_role), now=answer.now())

    async def refresh(self, home: str, lic: str, nonce: str, *, version: str | None = None,
                      reverted_from: str | None = None) -> RefreshAnswer:
        """The next token, a new nonce and the licence's revocation statements. ``nonce`` is the last one the
        server gave, even after a backup was restored: never one made here (server/README.md §10.9)."""
        _invalid(home=is_home_id(home), lic=is_lic(lic), nonce=is_nonce(nonce),
                 version=version is None or is_version(version),
                 reverted_from=reverted_from is None or is_version(reverted_from))
        payload: dict[str, Any] = {"home": home, "lic": lic, "nonce": nonce}
        if version is not None:
            payload["version"] = version
        if reverted_from is not None:
            payload["reverted_from"] = reverted_from
        answer = _Answer(await self._post(ROUTE_REFRESH, payload))
        return RefreshAnswer(
            token=answer.take("token", is_statement),
            nonce=answer.take("nonce", is_nonce),
            role=answer.take("role", is_role),
            revocations=_statements(answer.take("revocations", lambda v: isinstance(v, list))),
            cloned=answer.take("cloned", lambda v: isinstance(v, bool)),
            moves_left=answer.moves_left(),
            now=answer.now(),
        )

    async def release(self, home: str, lic: str) -> ReleaseAnswer:
        """"Free this home": the licence can be activated elsewhere (a move, for the production home)."""
        _invalid(home=is_home_id(home), lic=is_lic(lic))
        answer = _Answer(await self._post(ROUTE_RELEASE, {"home": home, "lic": lic}))
        answer.take("released", lambda v: v is True)
        return ReleaseAnswer(role=answer.take("role", is_role), moves_left=answer.moves_left(), now=answer.now())

    async def _post(self, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        """One POST: the 200's JSON object, or the exception for anything else."""
        if self._placeholder:
            raise ApiNotConfigured()
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(data) > MAX_REQUEST_BYTES:
            raise RequestRejected("too_large")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session.post(
                    self._base + route, data=data, headers=_HEADERS, allow_redirects=False
                ) as resp:
                    status = resp.status
                    retry_after = resp.headers.get(hdrs.RETRY_AFTER)
                    raw = await _read_capped(resp)
        except LicenceApiError as err:
            _LOGGER.debug("Licence server %s: %s", route, err.code)
            raise
        except (TimeoutError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Licence server %s: no answer (timeout)", route)
            raise ApiUnreachable(detail="timeout") from err
        except (aiohttp.ClientError, OSError) as err:
            _LOGGER.debug("Licence server %s: no answer (%s)", route, type(err).__name__)
            raise ApiUnreachable(detail=type(err).__name__) from err
        body = _json(raw)
        if status == 200 and body is not None:
            _LOGGER.debug("Licence server %s: HTTP 200", route)
            return body
        err = _error(status, body, retry_after) if status != 200 else ApiUnavailable("bad_answer", http_status=200)
        _LOGGER.debug("Licence server %s: HTTP %s %s", route, status, err.code)
        raise err


def async_get_api(hass: HomeAssistant, base: str = API_BASE) -> LicenceApi:
    """The client on Home Assistant's shared HTTP session."""
    return LicenceApi(async_get_clientsession(hass), base)
