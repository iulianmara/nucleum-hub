r"""The licence state, in Python: the port of server/lib/licence-state.mjs, the one rule that turns a verified
token's claims into what the dashboard shows (plan §4.5, §4.6; D-064 to D-068, D-073).

The JavaScript file is the contract and its header explains every rule; this file copies it check for check, in
the same order, and server/lib/token-vectors.json holds the cases both must give (the tests run every one of them,
and fail if one is left out). A change there is a change here.

Pure: the standard library only and no import of the package, no clock, no cryptography, so the tests load it
alone. The signature is checked before, by ``verifier.py``; this file only reads claims. What the Worker puts in
the claims (subExp, lifeEnt and the other builders at the end of the JavaScript file) is the server's and is not
copied here.

What a port must do the same way (server/lib/token.mjs, its header): numbers are read by value, so a JSON 1.0 is
the number 1, and a boolean is never a number (Python's bool is an int, so every number check below refuses it
first); a member that is missing is not one that is null (``_MISSING`` stands for JavaScript's undefined, None for
null); patterns match the whole string with ASCII classes (re.fullmatch with [0-9], never re.match with $, which
also matches before a final newline, and never \d, which also matches other scripts' digits).

    state(claims, ctx) -> {"state", "reason", "until", "plan", "role", "grace", "notice"}
    ctx = {"now": Home Assistant's time in unix seconds, "build": {"released", "base_released", "lts"},
           "home": this installation's home id, "revocations": the payloads verifier.py accepted}

The caller's own mistakes (no now, no build stamps) raise TypeError, as in JavaScript; claims never make this
file raise, whatever they hold, because a token that made the rule throw must not become a way around it
(plan §4.1 p.5).
"""

from __future__ import annotations

from collections.abc import Mapping
import calendar
import datetime
import math
import re
from types import MappingProxyType
from typing import Any, Final, NamedTuple

DAY_S: Final = 86400
TRIAL_S: Final = 3600  # the trial: 60 minutes of clock time from the first start (D-065)
REFUND_DAYS: Final = 14  # the refund window of a first payment, and the one-time licence's provisional token
STAGING_TOKEN_S: Final = 7 * DAY_S  # the staging instance's token lives at most this long from its iat (D-068)
LIFE_VERIFY_S: Final = 30 * DAY_S  # a permanent one-time token older than this carries verify_security (D-068)

PLANS: Final = ("sub", "life", "trial", "founder", "pro", "nfr", "beta")
ROLES: Final = ("prod", "staging")
STATES: Final = ("FULL", "TRIAL", "GRACE", "FALLBACK", "UNCOVERED", "ACTIVATE")
REASONS: Final = MappingProxyType({
    "FULL": ("broken_environment",),
    "GRACE": ("payment_due", "provisional", "renewal_due"),
    "FALLBACK": ("kept_lts",),
    "UNCOVERED": ("build_after_ent", "not_on_kept_lts"),
    "ACTIVATE": ("no_token", "invalid_token", "no_home", "other_home", "refunded", "revoked", "moved", "trial_over",
                 "subscription_expired", "staging_unverified", "life_unverified", "expired"),
})
NOTICES: Final = ("verify_security",)

# Why a revocation statement was signed, and the reason it gives a token it covers (D-068).
REVOCATION_WHY: Final = ("refund", "chargeback", "leaked_key", "abuse", "owner", "moved")
WHY_REASON: Final = MappingProxyType({"refund": "refunded", "chargeback": "refunded", "leaked_key": "revoked",
                                      "abuse": "revoked", "owner": "revoked", "moved": "moved"})
# Several statements may cover a token: the licence-wide end says more than the end of one home's binding.
_REVOKED_ORDER: Final = ("refunded", "revoked", "moved")

# The claims of v = 1, in the order the server writes them; seats, feat and inst may be left out.
CLAIMS: Final = ("v", "jti", "lic", "home", "plan", "role", "seats", "iat", "exp", "ent", "pf", "feat", "inst")


class Grace(NamedTuple):
    """A plan's grace (T5 A: derived from the plan, never carried in the token). "before": the last days before
    exp, which the server put into exp; "after": the days that follow exp."""

    days: int
    side: str
    reason: str


GRACE: Final = MappingProxyType({
    "sub": Grace(7, "before", "payment_due"),
    "life": Grace(90, "after", "provisional"),
    "founder": Grace(90, "after", "provisional"),
    "pro": Grace(30, "after", "renewal_due"),
    "nfr": Grace(30, "after", "renewal_due"),
    "beta": Grace(0, "after", "renewal_due"),
    "trial": Grace(0, "after", "renewal_due"),
})

# --- small checks, shared with verifier.py ---------------------------------------------------------------------
# JavaScript's undefined: a member that is not there. JSON's null is None, and the rules tell the two apart
# (seats may be missing but not null; exp may be null but not missing).
_MISSING: Final = object()
_MAX_SAFE: Final = 2**53 - 1
_EPOCH: Final = datetime.date(1970, 1, 1).toordinal()


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_safe_integer(value: Any) -> bool:
    """JavaScript's Number.isSafeInteger: a number without a fraction, at most 2^53 - 1 either way. A JSON 1.0 is
    one; True, NaN and the infinities are not."""
    if not _number(value):
        return False
    if isinstance(value, float) and not value.is_integer():
        return False
    return abs(value) <= _MAX_SAFE


def whole(value: Any) -> Any:
    """A safe integer as an int (a JSON 1.0 or 1.7917992e9 is read by value), anything else as it is: what this
    integration hands on is then the same JSON the server wrote."""
    return int(value) if is_safe_integer(value) else value


def is_moment(value: Any) -> bool:
    """Whole seconds between 2001 and 2286: a time in milliseconds is refused, not read 1000 times late."""
    return is_safe_integer(value) and 1_000_000_000 <= value < 10_000_000_000


_DAY_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def is_day(value: Any) -> bool:
    """A real calendar date 'YYYY-MM-DD', year 2000 or later ('9999-12-31' included)."""
    if not isinstance(value, str) or _DAY_RE.fullmatch(value) is None:
        return False
    year, month, day = int(value[0:4]), int(value[5:7]), int(value[8:10])
    return year >= 2000 and 1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]


def end_of_day(day: str) -> int:
    """The first moment after a date: its end, exclusive (midnight UTC of the next day)."""
    if not is_day(day):
        raise TypeError("end_of_day: not a date")
    date = datetime.date(int(day[0:4]), int(day[5:7]), int(day[8:10]))
    return (date.toordinal() - _EPOCH + 1) * DAY_S


def day_of(seconds: Any) -> str:
    """The UTC date of a moment."""
    if not is_moment(seconds):
        raise TypeError("day_of: not a moment in unix seconds")
    return datetime.date.fromordinal(_EPOCH + int(seconds) // DAY_S).isoformat()


_LIC_RE: Final = re.compile(r"L-[0-9A-HJKMNP-TV-Z]{6}")  # licences.id, Crockford
_HOME_RE: Final = re.compile(r"h_[A-Za-z0-9_-]{16,62}")  # activations.home_id
_JTI_RE: Final = re.compile(r"[A-Za-z0-9_-]{8,64}")
_TXN_RE: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_FEAT_RE: Final = re.compile(r"[a-z0-9_]{1,32}")
_INST_RE: Final = re.compile(r"A-[A-Za-z0-9_-]{1,38}")
_LTS_LINE_RE: Final = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}")


def _full(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def is_lic(value: Any) -> bool:
    return _full(_LIC_RE, value)


def is_home(value: Any) -> bool:
    return _full(_HOME_RE, value)


# --- the claims ---------------------------------------------------------------------------------------------------
def check_claims(claims: Any) -> str | None:
    """None when the claims are a well-formed v = 1 token's, else the name of the first claim that is not, in the
    order v, jti, plan, lic, home, role, seats, iat, exp, ent, pf, feat, inst. Unknown claims are ignored (T10 A).
    D-068: a staging token always ends, at most STAGING_TOKEN_S after its iat, and a subscription never has pf."""
    if not isinstance(claims, Mapping):
        return "claims"

    def get(name: str) -> Any:
        return claims.get(name, _MISSING)

    v = get("v")
    if not (_number(v) and v == 1):
        return "v"
    if not _full(_JTI_RE, get("jti")):
        return "jti"
    plan = get("plan")
    if not (isinstance(plan, str) and plan in PLANS):
        return "plan"
    if (get("lic") is not None) if plan == "trial" else not is_lic(get("lic")):  # a trial has no licence
        return "lic"
    if not is_home(get("home")):
        return "home"
    role = get("role")
    if not (isinstance(role, str) and role in ROLES):
        return "role"
    seats = get("seats")
    if seats is not _MISSING and not (is_safe_integer(seats) and 1 <= seats <= 1000):
        return "seats"
    iat = get("iat")
    if not is_moment(iat):
        return "iat"
    exp = get("exp")
    if exp is None:
        # these always end; so does every staging token (D-068)
        if plan in ("sub", "trial", "nfr", "beta") or role == "staging":
            return "exp"
    elif (not is_moment(exp) or exp <= iat or (plan == "trial" and exp - iat > TRIAL_S)
          or (role == "staging" and exp - iat > STAGING_TOKEN_S)):
        return "exp"
    ent = get("ent")
    if not is_day(ent):
        return "ent"
    pf = get("pf")
    if pf is not None and not (is_day(pf) and pf <= ent and plan not in ("trial", "sub")):
        return "pf"
    feat = get("feat")
    if feat is not _MISSING and not (isinstance(feat, list) and len(feat) <= 32
                                     and all(_full(_FEAT_RE, item) for item in feat)):
        return "feat"
    inst = get("inst")
    if inst is not _MISSING and inst is not None and not _full(_INST_RE, inst):
        return "inst"
    return None


def known_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    """The known claims only, in their order, with the defaults of the optional ones: what a verifier hands on.
    Numbers come back as ints, so the dashboard gets the JSON the server wrote."""
    feat = claims.get("feat")
    seats = claims.get("seats")
    exp = claims.get("exp")
    return {
        "v": 1, "jti": claims.get("jti"), "lic": claims.get("lic"), "home": claims.get("home"),
        "plan": claims.get("plan"), "role": claims.get("role"), "seats": 1 if seats is None else whole(seats),
        "iat": whole(claims.get("iat")), "exp": None if exp is None else whole(exp), "ent": claims.get("ent"),
        "pf": claims.get("pf"), "feat": list(feat) if isinstance(feat, list) else [], "inst": claims.get("inst"),
    }


# --- revocation statements ------------------------------------------------------------------------------------------
def check_revocation(rev: Any) -> str | None:
    """A statement's payload { typ: 'rev', lic, home | null, txn | null, why, iat }: None when well formed, else the
    name of the first member that is not. home null is every home of the licence; a refund or a chargeback names its
    transaction; 'moved' ends one home's binding, never with a transaction."""
    if not isinstance(rev, Mapping):
        return "revocation"
    if rev.get("typ", _MISSING) != "rev":
        return "typ"
    if not is_lic(rev.get("lic", _MISSING)):
        return "lic"
    home = rev.get("home", _MISSING)
    if home is not None and not is_home(home):
        return "home"
    txn = rev.get("txn", _MISSING)
    if txn is not None and not _full(_TXN_RE, txn):
        return "txn"
    why = rev.get("why", _MISSING)
    if not (isinstance(why, str) and why in REVOCATION_WHY):
        return "why"
    if why in ("refund", "chargeback") and txn is None:
        return "why"
    if why == "moved" and (home is None or txn is not None):
        return "why"
    if not is_moment(rev.get("iat", _MISSING)):
        return "iat"
    return None


def revocation_reason(why: Any) -> str:
    """What a covering statement makes of the token: refunded, revoked or moved."""
    return WHY_REASON.get(why, "revoked") if isinstance(why, str) else "revoked"


def revokes(rev: Any, claims: Mapping[str, Any]) -> bool:
    """Whether a verified statement applies to a token: the same licence, the same home or every home, and issued
    no earlier than the token (the same second counts). A token issued after it (support put the licence back) is
    not covered, so a stored statement never has to be deleted by hand."""
    return (check_revocation(rev) is None and rev["lic"] == claims["lic"]
            and (rev["home"] is None or rev["home"] == claims["home"]) and rev["iat"] >= claims["iat"])


class Window(NamedTuple):
    """When a token stops being FULL and when its grace ends (unix seconds; None = never)."""

    grace: int
    full_until: int | None
    grace_until: int | None


def window_of(claims: Mapping[str, Any]) -> Window:
    """The GRACE table applied to known claims. A staging token has no grace whatever its plan (D-068)."""
    rule = GRACE[claims["plan"]]
    exp = claims["exp"]
    if exp is None:
        return Window(0, None, None)
    if claims["role"] == "staging":
        return Window(0, exp, exp)
    span = rule.days * DAY_S
    if rule.side == "before":
        # the server put the grace into exp only for a subscription that renews: exp after the end of ent's day
        if exp <= end_of_day(claims["ent"]):
            return Window(0, exp, exp)
        return Window(rule.days, exp - span, exp)
    return Window(rule.days, exp, exp + span)


# --- the state --------------------------------------------------------------------------------------------------
class _Context(NamedTuple):
    now: int
    released: str
    lts: Mapping[str, Any] | None
    home: str | None
    revocations: list[Any]


def _context(ctx: Any) -> _Context:
    if not isinstance(ctx, Mapping):
        raise TypeError("state: the context {now, build, home, revocations} is required")
    now = ctx.get("now")
    if not _number(now) or not math.isfinite(now) or not 1e9 <= now < 1e10:
        raise TypeError("state: now must be Home Assistant's time in unix seconds")
    build = ctx.get("build")
    if not isinstance(build, Mapping) or not is_day(build.get("released")):
        raise TypeError("state: build.released (WD_RELEASED) must be a YYYY-MM-DD date")
    if build.get("base_released") is not None and not is_day(build.get("base_released")):
        raise TypeError("state: build.base_released must be a date or None")
    lts = build.get("lts")
    if lts is not None and not (isinstance(lts, Mapping) and _full(_LTS_LINE_RE, lts.get("line"))
                                and is_day(lts.get("started")) and lts["started"] <= build["released"]):
        raise TypeError('state: build.lts (WD_LTS) must be None or {"line": "1.0", "started": "YYYY-MM-DD"}, '
                        "started on or before the release")
    revocations = ctx.get("revocations")
    if revocations is None:
        revocations = []
    if not isinstance(revocations, (list, tuple)):
        raise TypeError("state: revocations must be a list")
    home = ctx.get("home")
    return _Context(math.floor(now), build["released"], lts, home if isinstance(home, str) and home else None,
                    list(revocations))


def _result(state_: str, reason: str | None, claims: Mapping[str, Any] | None = None, until: int | None = None,
            grace: int = 0, notice: str | None = None) -> dict[str, Any]:
    return {"state": state_, "reason": reason, "until": until, "plan": claims["plan"] if claims else None,
            "role": claims["role"] if claims else None, "grace": grace, "notice": notice}


def _over_reason(claims: Mapping[str, Any]) -> str:
    """The reason of a token whose time is over with nothing left to cover the build (D-068, D-073). A one-time
    licence gets here only with its provisional token, so 'life' means "never confirmed"."""
    plan = claims["plan"]
    if plan == "trial":
        return "trial_over"
    if plan == "sub":
        return "subscription_expired"
    return "life_unverified" if plan == "life" else "expired"


def _notice_of(claims: Mapping[str, Any], now: int) -> str | None:
    """verify_security (D-068): the one-time licence's permanent token on the production home, issued more than
    LIFE_VERIFY_S before now. It never changes the state."""
    if (claims["plan"] == "life" and claims["exp"] is None and claims["role"] == "prod"
            and now - claims["iat"] > LIFE_VERIFY_S):
        return "verify_security"
    return None


def state(claims: Any, ctx: Any) -> dict[str, Any]:
    """The state of a token's claims (None: no token) in a context; the order of the checks is licence-state.mjs's
    and the vectors pin it: no token, invalid claims, no home id, another home, a covering revocation, then time
    and coverage."""
    now, released, lts, home, revocations = _context(ctx)
    if claims is None:
        return _result("ACTIVATE", "no_token")
    if check_claims(claims) is not None:
        return _result("ACTIVATE", "invalid_token")
    c = known_claims(claims)
    w = window_of(c)
    if home is None:
        return _result("ACTIVATE", "no_home", c, None, w.grace)
    if c["home"] != home:
        return _result("ACTIVATE", "other_home", c, None, w.grace)
    covering = {revocation_reason(rev["why"]) for rev in revocations if revokes(rev, c)}
    if covering:
        return _result("ACTIVATE", next(r for r in _REVOKED_ORDER if r in covering), c, None, w.grace)

    if w.full_until is None or now < w.full_until:
        phase = "live"
    else:
        phase = "grace" if now < w.grace_until else "over"  # type: ignore[operator]
    # ent covers the builds released on or before it, while the token lives; pf covers an LTS line started by pf,
    # with all its patches, even after the token ends (D-024); base_released plays no part (T8 A).
    by_ent = released <= c["ent"]
    by_pf = c["pf"] is not None and lts is not None and lts["started"] <= c["pf"]

    if phase != "over" and by_ent:
        if c["plan"] == "trial":
            return _result("TRIAL", None, c, c["exp"], w.grace)
        if phase == "live":
            return _result("FULL", None, c, w.full_until, w.grace, _notice_of(c, now))
        return _result("GRACE", GRACE[c["plan"]].reason, c, w.grace_until, w.grace)
    # the staging instance works only while the server keeps confirming it: pf does not carry it on (D-068)
    if phase == "over" and c["role"] == "staging":
        return _result("ACTIVATE", "trial_over" if c["plan"] == "trial" else "staging_unverified", c, None, w.grace)
    if by_pf:
        return _result("FALLBACK", "kept_lts", c, None, w.grace, _notice_of(c, now))
    if phase != "over":
        return _result("UNCOVERED", "build_after_ent", c, None, w.grace)
    if c["pf"] is not None:
        return _result("UNCOVERED", "not_on_kept_lts", c, None, w.grace)
    return _result("ACTIVATE", _over_reason(c), c, None, w.grace)


def state_of_verified(verified: Any, ctx: Any, self_test: Any) -> dict[str, Any]:
    """The same from a verifier result, with the self-test's answer, which is required (plan §4.1 p.5):
    self_test False means the environment is broken (no Ed25519 here): FULL, reason broken_environment, whatever
    the token, never the blocking screen that would hide the alarm keypad too. self_test True: any token that did
    not verify is invalid, 'unsupported' included, so a token cannot choose the fail-open. None is no token."""
    if self_test is not True and self_test is not False:
        raise TypeError("state_of_verified: self_test (the Ed25519 self-test's answer, run once at start) must be "
                        "True or False")
    if self_test is False:
        _context(ctx)
        return _result("FULL", "broken_environment")
    if verified is None:
        return state(None, ctx)
    if not isinstance(verified, Mapping) or verified.get("ok") is not True:
        _context(ctx)
        return _result("ACTIVATE", "invalid_token")
    return state(verified.get("claims"), ctx)


def shows_activation(state_: str) -> bool:
    """The states in which the dashboard shows only the activation screen (D-065)."""
    return state_ in ("ACTIVATE", "UNCOVERED")
