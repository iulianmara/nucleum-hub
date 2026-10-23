"""Licence tokens, verified in Python: the port of the verifying side of server/lib/token.mjs (D-066; D-067: the
integration verifies the signature and tells the dashboard the state, the dashboard never verifies).

The format is token.mjs's: a compact JWS signed with Ed25519 by an online key, whose certificate (a compact JWS
signed by a root) travels in the token's header as ``crt``; a revocation statement is a compact JWS of the same
kind. The roots are the build's (const.LICENCE_ROOTS, two of them, T9 A); the online keys known to be stolen are
the build's list of revoked kids. The checks, their order and their error codes are token.mjs's, one for one, and
server/lib/token-vectors.json holds the cases both must give (the tests run every one of them):

    bad_format, too_big, bad_base64, bad_json, bad_alg, bad_header, bad_cert, unknown_root, bad_cert_signature,
    kid_mismatch, key_revoked, bad_signature, unknown_version, bad_claims, cert_not_yet, cert_expired,
    from_future, unsupported, verify_failed

Nothing given to verify ever makes these functions raise: every problem is ``{"ok": False, "error": ...}``, so a
broken token cannot make a caller fall open (plan §4.1 p.5). Only the caller's own mistakes (a malformed root, no
now) raise TypeError. exp is not judged here: an expired token still verifies, because what follows it is the
state's rule (state.py).

What makes Python answer exactly as Node, Workers and browsers do (token.mjs's header, and the vectors pin each):
the token is ASCII before its length is counted; base64url is strict (the alphabet only, no padding, the unused
low bits zero, so one value has one spelling); JSON is read from strict UTF-8 without a BOM and without NaN or
Infinity; numbers are read by value and a boolean is never one; a signature whose S is not below L, or whose R is
a point of small order, is refused before the library sees it, and so is a public key of small order or not
canonical, wherever one comes in (libsodium's rules: OpenSSL alone would accept R = identity with S = 0 under such
a key, for any message).

The signature itself is checked by the ``cryptography`` package, which ships with Home Assistant (plan §4.2 [S25]),
imported only when a token is verified. Where it is missing or cannot do Ed25519, the self-test at start fails and
the state is FULL with the reason broken_environment (state.state_of_verified): a broken environment never hides
the dashboard and its alarm keypad.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
import functools
import json
import logging
import math
import re
from types import MappingProxyType
from typing import Any, Final, NamedTuple

from .state import check_claims, check_revocation, is_moment, known_claims, state_of_verified, whole

_LOGGER = logging.getLogger(__name__)

MAX_TOKEN: Final = 4096  # bytes: the whole token with its certificate (T10)
FUTURE_S: Final = 300  # iat may be at most 5 minutes after Home Assistant's time (T10)
CERT_MAX_S: Final = 90 * 86400  # an online key's certificate: at most 90 days (signing_keys CHECK)
TYP: Final = MappingProxyType({"token": "NHL", "cert": "NHC", "revocation": "NHR"})
ERRORS: Final = ("bad_format", "too_big", "bad_base64", "bad_json", "bad_alg", "bad_header", "bad_cert",
                 "unknown_root", "bad_cert_signature", "kid_mismatch", "key_revoked", "bad_signature",
                 "unknown_version", "bad_claims", "cert_not_yet", "cert_expired", "from_future", "unsupported",
                 "verify_failed")
# The vectors' keys are named so, and their seeds are public: a build that trusts one is broken.
TEST_KID_PREFIX: Final = "TEST-"

_KID_RE: Final = re.compile(r"[A-Za-z0-9_-]{4,40}")  # signing_keys.kid


def is_kid(value: Any) -> bool:
    return isinstance(value, str) and _KID_RE.fullmatch(value) is not None


# --- base64url (RFC 4648 §5, without padding) -------------------------------------------------------------------
_B64URL_RE: Final = re.compile(r"[A-Za-z0-9_-]*")


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: Any) -> bytes | None:
    """Strict: only the alphabet, no padding, no impossible length (4n + 1), and the unused low bits of the last
    character zero (the bytes must spell back to the same text), so a token cannot be re-spelled without breaking.
    None for anything else; never raises."""
    if not isinstance(text, str) or len(text) % 4 == 1 or _B64URL_RE.fullmatch(text) is None:
        return None
    raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    return raw if b64url_encode(raw) == text else None


def _no_constant(name: str) -> Any:
    raise ValueError(f"JSON has no {name}")


def _json_object(raw: bytes) -> dict[str, Any] | None:
    """A JSON object read from strict UTF-8: a lone surrogate written as bytes is refused, a leading BOM is kept
    (so json refuses it), and NaN and Infinity, which Python's json would accept, are refused as JSON.parse does."""
    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


# --- keys --------------------------------------------------------------------------------------------------------
# The points of small order, with the sign bit of the last byte cleared: these 7 values give all 14 encodings of
# such a point (libsodium's list; the tests multiply each by 8). Under such a public key, R = identity and S = 0
# is a "valid" signature of every message in OpenSSL, so one such key anywhere in the chain would let anyone sign
# anything.
_SMALL_ORDER: Final = frozenset(bytes.fromhex(h) for h in (
    "0000000000000000000000000000000000000000000000000000000000000000",  # order 4 (y = 0)
    "0100000000000000000000000000000000000000000000000000000000000000",  # order 1: the identity
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",  # order 8
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",  # order 8
    "ecffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # order 2 (y = p - 1)
    "edffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # y = p, i.e. 0: not canonical
    "eeffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff7f",  # y = p + 1, i.e. 1: not canonical
))
_P: Final = 2**255 - 19
# The group order L of Ed25519: a signature whose S is not below it is the same signature written another way.
_L: Final = 2**252 + 27742317777372353535851937790883648493


def is_small_order(raw: bytes) -> bool:
    return len(raw) == 32 and raw[:31] + bytes((raw[31] & 0x7F,)) in _SMALL_ORDER


def is_usable_public_key(raw: Any) -> bool:
    """A public key we trust: 32 bytes, y below p (RFC 8032 §5.1.3: some libraries read a larger y modulo p),
    not of small order."""
    if not isinstance(raw, bytes) or len(raw) != 32:
        return False
    y = int.from_bytes(raw, "little") & ((1 << 255) - 1)
    return y < _P and not is_small_order(raw)


def _usable_public_key(value: Any, what: str) -> bytes:
    raw = bytes(value) if isinstance(value, (bytes, bytearray)) else b64url_decode(value)
    if raw is None or len(raw) != 32:
        raise TypeError(f"{what}: exactly 32 bytes (bytes or base64url) are required")
    if not is_usable_public_key(raw):
        raise TypeError(f"{what}: not a usable Ed25519 public key (a point of small order, or not canonical)")
    return raw


# --- the signature: the cryptography package, or what the tests give -----------------------------------------
class Ed25519Unsupported(Exception):
    """This Python cannot verify Ed25519: no cryptography package, or one built without it."""


# (public key, signature, message) -> True when the signature holds; raises Ed25519Unsupported when it cannot tell
Ed25519Verify = Callable[[bytes, bytes, bytes], bool]


@functools.lru_cache(maxsize=32)
def _public_key(raw: bytes) -> Any:
    # a handful of keys: the roots and the current online keys
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: PLC0415

    return Ed25519PublicKey.from_public_bytes(raw)


def cryptography_verify(public: bytes, signature: bytes, message: bytes) -> bool:
    """Ed25519 with the cryptography package that ships with Home Assistant, imported here and not at the top of
    the module, so the module loads (and the self-test can answer) where it is missing."""
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm  # noqa: PLC0415
    except ImportError as err:
        raise Ed25519Unsupported("the cryptography package is missing") from err
    try:
        key = _public_key(public)
        key.verify(signature, message)
    except InvalidSignature:
        return False
    except (ImportError, UnsupportedAlgorithm) as err:
        raise Ed25519Unsupported("this cryptography cannot do Ed25519") from err
    return True


def _verify_bytes(raw_public: bytes, signature: bytes, signing_input: str, verify: Ed25519Verify) -> bool:
    # S below L and R not of small order are checked here, as libsodium does: an honest signer's R = [r]B never is
    # of small order, so refusing it changes nothing for real tokens and keeps every implementation on one answer
    if (len(signature) != 64 or int.from_bytes(signature[32:], "little") >= _L or is_small_order(signature[:32])
            or not is_usable_public_key(raw_public)):
        return False
    return verify(raw_public, signature, signing_input.encode("ascii")) is True


# --- reading ---------------------------------------------------------------------------------------------------
class _Refused(Exception):
    """What a token, a statement or a certificate is refused for: one of ERRORS, and the claim for bad_claims."""

    def __init__(self, error: str, detail: str | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.detail = detail


class _Compact(NamedTuple):
    header: dict[str, Any]
    payload: bytes
    signature: bytes
    signing_input: str


def _split(text: Any) -> _Compact:
    # ASCII first: for an ASCII string, characters and UTF-8 bytes are the same number, so the 4 KiB limit (T10)
    # means the same thing here as in JavaScript, which counts UTF-16 units
    if not isinstance(text, str) or not text.isascii():
        raise _Refused("bad_format")
    if len(text) > MAX_TOKEN:
        raise _Refused("too_big")
    parts = text.split(".")
    if len(parts) != 3 or "" in parts:
        raise _Refused("bad_format")
    raw = [b64url_decode(part) for part in parts]
    if None in raw:
        raise _Refused("bad_base64")
    header = _json_object(raw[0])  # type: ignore[arg-type]
    if header is None:
        raise _Refused("bad_json")
    return _Compact(header, raw[1], raw[2], f"{parts[0]}.{parts[1]}")  # type: ignore[arg-type]


class _Cert(NamedTuple):
    kid: str
    pub: str
    raw: bytes
    nbf: int
    exp: int
    root: str

    def public(self) -> dict[str, Any]:
        return {"kid": self.kid, "pub": self.pub, "nbf": self.nbf, "exp": self.exp, "root": self.root}


def _cert_header_ok(header: Mapping[str, Any]) -> bool:
    return (header.get("alg") == "EdDSA" and header.get("typ") == TYP["cert"] and "crit" not in header
            and is_kid(header.get("kid")))


def _check_cert(crt: str, roots: Mapping[str, bytes], verify: Ed25519Verify) -> _Cert:
    """The certificate of the online key: its header, a root of the build, the root's signature, and only then its
    fields (read after the signature, like a token's payload)."""
    try:
        cert = _split(crt)
    except _Refused:
        raise _Refused("bad_cert") from None
    if not _cert_header_ok(cert.header):
        raise _Refused("bad_cert")
    root = roots.get(cert.header["kid"])
    if root is None:
        raise _Refused("unknown_root")
    if not _verify_bytes(root, cert.signature, cert.signing_input, verify):
        raise _Refused("bad_cert_signature")
    payload = _json_object(cert.payload)
    if payload is None:
        raise _Refused("bad_cert")
    kid, pub, nbf, exp = (payload.get(name) for name in ("kid", "pub", "nbf", "exp"))
    if (not is_kid(kid) or payload.get("use") != "lic" or not is_moment(nbf) or not is_moment(exp) or exp <= nbf
            or exp - nbf > CERT_MAX_S):
        raise _Refused("bad_cert")
    raw = b64url_decode(pub) if isinstance(pub, str) and len(pub) == 43 else None
    if raw is None or not is_usable_public_key(raw):
        raise _Refused("bad_cert")
    return _Cert(kid, pub, raw, whole(nbf), whole(exp), cert.header["kid"])


class _Trust(NamedTuple):
    roots: Mapping[str, bytes]
    revoked_kids: frozenset[str]


def _trust(roots: Any, revoked_kids: Any, fn: str) -> _Trust:
    """The caller's roots ({kid, pub} each, pub in base64url or bytes) and revoked kids, checked: a mistake here
    is the build's, so it raises instead of refusing tokens one by one."""
    if not isinstance(roots, (list, tuple)):
        raise TypeError(f"{fn}: roots must be a list of {{kid, pub}}")
    found: dict[str, bytes] = {}
    for root in roots:
        kid = root.get("kid") if isinstance(root, Mapping) else None
        if not is_kid(kid):
            raise TypeError(f"{fn}: every root needs a kid")
        if kid in found:
            raise TypeError(f"{fn}: the root kid {kid} is there twice")
        found[kid] = _usable_public_key(root.get("pub"), f"{fn} root {kid}")
    if revoked_kids is None:
        revoked_kids = ()
    if isinstance(revoked_kids, (str, bytes)) or not isinstance(revoked_kids, Iterable):
        raise TypeError(f"{fn}: revoked_kids must be a list of strings")
    revoked = frozenset(revoked_kids)
    if not all(isinstance(kid, str) for kid in revoked):
        raise TypeError(f"{fn}: revoked_kids must be strings")
    return _Trust(MappingProxyType(found), revoked)


def _now(now: Any, fn: str) -> int:
    if (not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now)
            or not 1e9 <= now < 1e10):
        raise TypeError(f"{fn}: now must be Home Assistant's time in unix seconds")
    return math.floor(now)


class _Verified(NamedTuple):
    header: dict[str, Any]
    cert: _Cert
    payload: dict[str, Any]


def _verify_compact(text: Any, typ: str, trust: _Trust, verify: Ed25519Verify) -> _Verified:
    compact = _split(text)
    header = compact.header
    if header.get("alg") != "EdDSA":
        raise _Refused("bad_alg")
    kid, crt = header.get("kid"), header.get("crt")
    if header.get("typ") != typ or "crit" in header or not is_kid(kid) or not isinstance(crt, str):
        raise _Refused("bad_header")
    cert = _check_cert(crt, trust.roots, verify)
    if cert.kid != kid:
        raise _Refused("kid_mismatch")
    if kid in trust.revoked_kids:
        raise _Refused("key_revoked")
    if not _verify_bytes(cert.raw, compact.signature, compact.signing_input, verify):
        raise _Refused("bad_signature")
    payload = _json_object(compact.payload)  # read only after the signature holds
    if payload is None:
        raise _Refused("bad_json")
    return _Verified({"alg": "EdDSA", "typ": typ, "kid": kid}, cert, payload)


def _time_error(iat: int, cert: _Cert, now: int | None) -> str | None:
    """The certificate is judged at the statement's iat, not at now: a token issued inside the online key's 90 days
    stays valid after them (plan §6.9). now None: no from_future (revocations)."""
    if iat < cert.nbf:
        return "cert_not_yet"
    if iat >= cert.exp:
        return "cert_expired"
    if now is not None and iat > now + FUTURE_S:
        return "from_future"
    return None


def _failed(error: str, detail: str | None, field: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "detail": detail, field: None, "header": None, "cert": None}


def _guarded(check: Callable[[], dict[str, Any]], field: str) -> dict[str, Any]:
    try:
        return check()
    except _Refused as refused:
        return _failed(refused.error, refused.detail, field)
    except Ed25519Unsupported:
        return _failed("unsupported", None, field)
    except Exception as err:  # noqa: BLE001 (whatever a token makes go wrong, it is refused, never let through)
        _LOGGER.debug("A licence statement could not be verified (%s)", type(err).__name__)
        return _failed("verify_failed", None, field)


# --- verifying --------------------------------------------------------------------------------------------------
def _token(token: Any, trust: _Trust, now: int, verify: Ed25519Verify) -> dict[str, Any]:
    verified = _verify_compact(token, TYP["token"], trust, verify)
    payload = verified.payload
    version = payload.get("v")
    if not (isinstance(version, (int, float)) and not isinstance(version, bool) and version == 1):
        raise _Refused("unknown_version")
    bad = check_claims(payload)
    if bad is not None:
        raise _Refused("bad_claims", bad)
    claims = known_claims(payload)
    late = _time_error(claims["iat"], verified.cert, now)
    if late is not None:
        raise _Refused(late)
    return {"ok": True, "error": None, "detail": None, "claims": claims, "header": verified.header,
            "cert": verified.cert.public()}


def _revocation(statement: Any, trust: _Trust, verify: Ed25519Verify) -> dict[str, Any]:
    verified = _verify_compact(statement, TYP["revocation"], trust, verify)
    payload = verified.payload
    bad = check_revocation(payload)
    if bad is not None:
        raise _Refused("bad_claims", bad)
    revocation = {"typ": "rev", "lic": payload["lic"], "home": payload["home"], "txn": payload["txn"],
                  "why": payload["why"], "iat": whole(payload["iat"])}
    late = _time_error(revocation["iat"], verified.cert, None)
    if late is not None:
        raise _Refused(late)
    return {"ok": True, "error": None, "detail": None, "revocation": revocation, "header": verified.header,
            "cert": verified.cert.public()}


def verify_token(token: Any, *, roots: Any, now: Any, revoked_kids: Any = (),
                 backend: Ed25519Verify | None = None) -> dict[str, Any]:
    """{"ok", "error", "detail", "claims", "header", "cert"}: claims are the known v = 1 claims (unknown ones
    dropped, optional ones filled in), header {alg, typ, kid}, cert {kid, pub, nbf, exp, root}. now is Home
    Assistant's time, never a tablet's (plan §4.4)."""
    trust = _trust(roots, revoked_kids, "verify_token")
    moment = _now(now, "verify_token")
    return _guarded(lambda: _token(token, trust, moment, backend or cryptography_verify), "claims")


def verify_revocation(statement: Any, *, roots: Any, revoked_kids: Any = (),
                      backend: Ed25519Verify | None = None) -> dict[str, Any]:
    """{"ok", "error", "detail", "revocation", "header", "cert"}; revocation is what state.state takes in its
    revocations list. No from_future and no now: a statement only takes rights away, so refusing one whose iat is
    ahead of Home Assistant's clock would only help a home whose clock is behind keep a refunded licence. Its iat
    must still be inside the online key's certificate."""
    trust = _trust(roots, revoked_kids, "verify_revocation")
    return _guarded(lambda: _revocation(statement, trust, backend or cryptography_verify), "revocation")


def verify_cert(crt: Any, *, roots: Any, backend: Ed25519Verify | None = None) -> dict[str, Any]:
    """{"ok", "error", "cert"}: the root's signature and the fields, no time check."""
    trust = _trust(roots, (), "verify_cert")

    def check() -> dict[str, Any]:
        if not isinstance(crt, str) or len(crt) > MAX_TOKEN:
            raise _Refused("bad_cert")
        cert = _check_cert(crt, trust.roots, backend or cryptography_verify)
        return {"ok": True, "error": None, "cert": cert.public()}

    result = _guarded(check, "cert")
    return {"ok": result["ok"], "error": result["error"], "cert": result["cert"]}


# RFC 8032 §7.1, TEST 1 (the empty message)
_RFC8032_PUB: Final = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
_RFC8032_SIG: Final = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a3"
                                    "3bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


def ed25519_supported(backend: Ed25519Verify | None = None) -> bool:
    """The self-test, run once at start (plan §4.1 p.5): RFC 8032's first signature holds and the same signature
    with one bit changed does not. False means a broken environment, not an invalid token. Never raises."""
    verify = backend or cryptography_verify
    forged = bytearray(_RFC8032_SIG)
    forged[0] ^= 1
    try:
        return verify(_RFC8032_PUB, _RFC8032_SIG, b"") is True and verify(_RFC8032_PUB, bytes(forged), b"") is False
    except Exception:  # noqa: BLE001 (any failure of the library is a broken environment)
        return False


class Verifier:
    """The build's trust, made once when the licence starts (the sale edition only): its roots, its revoked online
    keys and the self-test's answer, with a line in the log when the build has no root or the self-test fails.

    The integration makes it from const.LICENCE_ROOTS and const.LICENCE_REVOKED_KIDS. A root whose kid starts with
    TEST- is the vectors' (its seed is public) and is dropped, with an error in the log, unless the caller is a
    test that says so (allow_test_roots).
    """

    def __init__(self, roots: Iterable[Mapping[str, Any]], revoked_kids: Iterable[str] = (), *,
                 backend: Ed25519Verify | None = None, allow_test_roots: bool = False) -> None:
        roots = list(roots)
        if not allow_test_roots:
            kept = [r for r in roots if not (isinstance(r, Mapping) and isinstance(r.get("kid"), str)
                                             and r["kid"].startswith(TEST_KID_PREFIX))]
            if len(kept) != len(roots):
                _LOGGER.error("The build trusts a licence root made for the tests (TEST-): it is ignored; "
                              "a release needs the owner's own roots (tools/keys.mjs)")
            roots = kept
        self._trust = _trust(roots, revoked_kids, "Verifier")
        self._verify = backend or cryptography_verify
        self.self_test = ed25519_supported(self._verify)
        if not self._trust.roots:
            _LOGGER.warning("No licence root key is configured in this build (const.LICENCE_ROOTS is empty): "
                            "every licence token is refused until the owner's root public keys are added")
        if not self.self_test:
            _LOGGER.warning("The Ed25519 self-test failed: licence tokens cannot be verified on this Home "
                            "Assistant, so the dashboard stays fully usable (broken_environment)")

    @property
    def root_kids(self) -> tuple[str, ...]:
        return tuple(self._trust.roots)

    def verify_token(self, token: Any, now: Any) -> dict[str, Any]:
        moment = _now(now, "verify_token")
        return _guarded(lambda: _token(token, self._trust, moment, self._verify), "claims")

    def verify_revocation(self, statement: Any) -> dict[str, Any]:
        return _guarded(lambda: _revocation(statement, self._trust, self._verify), "revocation")

    def state(self, token: str | None, statements: Iterable[Any], *, now: Any, build: Mapping[str, Any],
              home: str | None) -> dict[str, Any]:
        """The state for the dashboard: the stored token verified at Home Assistant's now (None: no token), the
        stored statements verified (one that does not verify is left out: nothing unsigned changes the state,
        plan §4.8 p.8), then state.state_of_verified with the self-test's answer."""
        verified = None if token is None else self.verify_token(token, now)
        revocations = [r["revocation"] for r in map(self.verify_revocation, statements) if r["ok"]]
        context = {"now": now, "build": build, "home": home, "revocations": revocations}
        return state_of_verified(verified, context, self.self_test)
