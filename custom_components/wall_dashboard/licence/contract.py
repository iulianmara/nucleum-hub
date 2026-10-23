"""The licence server's contract, in Python (server/README.md §10.9 and §7.1): its address, its routes and
limits, and the shapes of what goes to it and comes back.

Pure: the standard library only and no relative import, so the tests load this file on its own, without
Home Assistant. Each rule names the server's side of it (``server/worker/licence-api.mjs``,
``server/worker/crypto.mjs``, ``server/lib/licence-state.mjs``); a change there is a change here, and the
tests hold both to the same examples.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any, Final
from urllib.parse import urlsplit

# --- Where the server is ---------------------------------------------------------------------------
# The API lives on https://api.<domain> (docs/DNS.md §2 and §7). A name under .invalid would never resolve (RFC 6761),
# and the client refuses one before any request (api.ApiNotConfigured): tools/package.mjs refuses a sale package
# that still has it.
API_DOMAIN: Final = "nucleumhub.com"
API_BASE: Final = f"https://api.{API_DOMAIN}"

# The site, for the one link the dashboard shows next to a state (buy, renew, the payment that failed): the domain of
# site/site.config.json. Under .invalid no link would be given at all.
SITE_BASE: Final = "https://nucleumhub.com"
# The pages, as the server's e-mails name them (server/worker/config.mjs SITE_PAGES) and as the site builds them.
SITE_PAGES: Final = {"pricing": "/pricing", "account": "/account"}
# The dashboard's 12 languages (D-003, D-014): English at the site's root, the others under /<code>/ in lower case
# (pt-BR under /pt-br/), as config.mjs sitePrefix writes them. Any other language gets the English page.
SITE_LANGUAGES: Final = ("en", "ro", "de", "fr", "nl", "es", "it", "pl", "sv", "cs", "da", "pt-BR")

ROUTE_TRIAL: Final = "/v1/trial"
ROUTE_ACTIVATE: Final = "/v1/activate"
ROUTE_REFRESH: Final = "/v1/refresh"
ROUTE_RELEASE: Final = "/v1/release"
ROUTES: Final = (ROUTE_TRIAL, ROUTE_ACTIVATE, ROUTE_REFRESH, ROUTE_RELEASE)

# --- Limits ------------------------------------------------------------------------------------------
# The whole request, connection and answer included. The Worker answers in well under a second; the rest is
# for a home on a slow link. The requests never run in the panel's path (plan §4.2), so waiting costs nothing.
TIMEOUT: Final = 15.0
# The server refuses a larger body (licence-api.mjs API_LIMIT, 413 too_large); ours are a few hundred bytes.
MAX_REQUEST_BYTES: Final = 4096
# A refresh carries the licence's revocation statements, about 900 bytes each with the certificate; a home
# rarely has more than a few. Anything larger is not the server talking.
MAX_ANSWER_BYTES: Final = 64 * 1024
# A token, or a statement, with its certificate (T10 A): the verifier refuses anything longer anyway.
MAX_TOKEN_BYTES: Final = 4096
# Retry-After of a 429, in seconds: what the server says, within these bounds (60 when it says nothing usable).
RETRY_AFTER_DEFAULT: Final = 60
RETRY_AFTER_MAX: Final = 24 * 3600

# --- The home id's Store (plan §4.3) ------------------------------------------------------------------------
# .storage/wall_dashboard.home: only the home id, written once. Removing the config entry never touches it,
# so a reinstall keeps the same home (no second trial, no extra activation), and it is in every backup, so a
# restore keeps the licence. The token, the nonce and the statements live elsewhere: a file written once a
# day or once a week is not the place for the one value that must never be lost.
STORE_HOME_KEY: Final = "wall_dashboard.home"
STORE_HOME_VERSION: Final = 1
# .storage/wall_dashboard.licence: what the server handed over (server/README.md §10.9: the token, the nonce, lic,
# role, the revocation statements) and when to ask it next. Never deleted either: a reinstall keeps the licence, and
# a statement must outlive every token it covers.
STORE_LICENCE_KEY: Final = "wall_dashboard.licence"
STORE_LICENCE_VERSION: Final = 1

# --- Shapes ------------------------------------------------------------------------------------------------
ROLES: Final = ("prod", "staging")  # licence-state.mjs ROLES
LICENCE_STATUSES: Final = ("refunded", "revoked", "disputed")  # licence_inactive's status
HOME_PREFIX: Final = "h_"
HOME_BYTES: Final = 16  # 128 random bits: "h_" + 22 base64url characters

_HOME_RE = re.compile(r"h_[A-Za-z0-9_-]{16,62}")  # licence-state.mjs isHome: what the server accepts
_IIDH_RE = re.compile(r"[0-9a-f]{64}")  # licence-api.mjs IIDH_RE
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # licence-api.mjs NONCE_RE: 32 random bytes
_LIC_RE = re.compile(r"L-[0-9A-HJKMNP-TV-Z]{6}")  # licence-state.mjs isLic
_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,39}")  # validate.mjs version
_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")  # active_elsewhere's last_seen


def _full(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and value.isascii() and pattern.fullmatch(value) is not None


def is_home_id(value: Any) -> bool:
    """A home id the server accepts: ``h_`` and 16 to 62 base64url characters (ours have 22)."""
    return _full(_HOME_RE, value)


def is_iidh(value: Any) -> bool:
    return _full(_IIDH_RE, value)


def is_nonce(value: Any) -> bool:
    return _full(_NONCE_RE, value)


def is_lic(value: Any) -> bool:
    return _full(_LIC_RE, value)


def is_version(value: Any) -> bool:
    return _full(_VERSION_RE, value)


def is_role(value: Any) -> bool:
    return isinstance(value, str) and value in ROLES


def is_day(value: Any) -> bool:
    return _full(_DAY_RE, value)


def is_statement(value: Any) -> bool:
    """A token or a revocation statement as it travels: ASCII, not empty, at most MAX_TOKEN_BYTES. Only the
    shape: the signature is the verifier's to check, and nothing unverified ever changes the state."""
    return isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_BYTES and value.isascii()


def site_link(page: str, language: Any = None, base: str | None = None) -> str | None:
    """A page of the site in the reader's language (``https://<domain>/ro/pricing``), or None while the site's address
    is the placeholder. ``language`` is the dashboard's; one the site does not have gets the English page. ``base`` is
    SITE_BASE, read at each call (not bound as a default), so the address set in one place reaches every caller."""
    if base is None:
        base = SITE_BASE
    host = (urlsplit(base).hostname or "").lower()
    if host == "invalid" or host.endswith(".invalid"):
        return None
    lang = language if isinstance(language, str) and language in SITE_LANGUAGES else "en"
    prefix = "" if lang == "en" else "/" + lang.lower()
    return base.rstrip("/") + prefix + SITE_PAGES[page]


def new_home_id(token_bytes: Any = secrets.token_bytes) -> str:
    """A new home id: ``h_`` and 128 random bits in base64url, without padding (22 characters)."""
    raw = token_bytes(HOME_BYTES)
    if not isinstance(raw, bytes) or len(raw) != HOME_BYTES:
        raise ValueError("a home id needs 16 random bytes")
    return HOME_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def iidh_of(instance_uuid: str, owner_id: str) -> str:
    """The instance's hash (server/README.md §10.9): SHA-256, lower-case hex, of ``core.uuid + ":" + the id of
    Home Assistant's owner``. The UUID alone is public on the local network (mDNS), so a hash of it alone
    could be computed by anyone there and use this installation's trial up; the owner's id is private."""
    if not isinstance(instance_uuid, str) or not instance_uuid or not isinstance(owner_id, str) or not owner_id:
        raise ValueError("the instance's hash needs the instance's UUID and the owner's id")
    return hashlib.sha256(f"{instance_uuid}:{owner_id}".encode("utf-8")).hexdigest()


# --- Licence keys (crypto.mjs): WD-XXXXX-XXXXX-XXXXX-XXXXX, Crockford base32 with a check character ----------
CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
KEY_PREFIX: Final = "WD"
KEY_BODY: Final = 19  # random characters, then one check character
_KEY_CHARS_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]+")


def canonical_key(key: Any) -> str | None:
    """The key as the server hashes it (crypto.mjs canonicalKey): upper case, letters and digits only, O read as
    0 and I and L as 1, so a key copied by hand from a screen still finds its licence; None when its length
    cannot be a key's."""
    if not isinstance(key, str):
        return None
    k = re.sub(r"[^A-Z0-9]", "", key.upper()).replace("O", "0").replace("I", "1").replace("L", "1")
    return k if 16 <= len(k) <= 40 else None


def _luhn32(chars: str, with_check: bool) -> int:
    total, factor = 0, 1 if with_check else 2
    for char in reversed(chars):
        add = factor * CROCKFORD.index(char)
        total += add // 32 + add % 32
        factor = 1 if factor == 2 else 2
    return total % 32


def key_well_formed(key: Any) -> bool:
    """Whether a typed key has the shape of one of ours and its check character holds (crypto.mjs
    licenceKeyWellFormed). It may still be unknown to the server; a mistyped one never leaves the house."""
    k = canonical_key(key)
    if k is None or not k.startswith(KEY_PREFIX) or len(k) != len(KEY_PREFIX) + KEY_BODY + 1:
        return False
    body = k[len(KEY_PREFIX):]
    return _KEY_CHARS_RE.fullmatch(body) is not None and _luhn32(body, True) == 0


def key_to_send(key: Any) -> str:
    """The canonical key in its printed groups, ``WD-XXXXX-XXXXX-XXXXX-XXXXX``. Sending the form both sides
    already agree on means no case rule of one language can turn it into another key on the server. Raises
    ValueError for a key that is not well formed."""
    if not key_well_formed(key):
        raise ValueError("not a well-formed licence key")
    body = canonical_key(key)[len(KEY_PREFIX):]  # type: ignore[index]
    return "-".join([KEY_PREFIX] + [body[i : i + 5] for i in range(0, len(body), 5)])
