"""A new version of Nucleum Hub (D-103): what the dashboard's notice and the loader need to know, the answer of
``wall_dashboard/update/status`` (websocket.py; the contract, v2: docs/ACTUALIZARE-INTEGRARE.md).

Nucleum Hub is installed from a custom repository of HACS, which Home Assistant 2026.11 ships built in as the
Marketplace. Neither this module nor the integration asks the internet, installs or restarts anything: the provider's
update entity knows the versions (HACS and the Marketplace read our GitHub releases with the user's GitHub account),
Home Assistant's ``update.install`` installs, ``homeassistant.restart`` restarts, and the dashboard calls both itself,
as an admin; the provider's refresh is sent by the one browser that claimed it here. This only reads, in Home
Assistant's memory:

- the update entity: the one whose entity-registry entry has the provider's platform (``marketplace``, then
  ``hacs``) and the unique_id ``const.UPDATE_REPOSITORY_ID``, GitHub's id of our repository (both providers give their
  entity the repository's id; its entity_id may be anything, and the Marketplace keeps a migrated one's);
- its state and attributes, as Home Assistant's update entity writes them: ``installed_version``, ``latest_version``
  (the release tags, "v1.0.3", said here without the "v", and the latter also exactly as written), ``in_progress`` and
  ``update_percentage`` (HACS 2.0.5 reports progress as a number in ``in_progress``, which Home Assistant turns into
  the two), ``release_summary``, ``supported_features``; the state "on" when Home Assistant offers the update (newer
  and not skipped), "unavailable" when the Marketplace could not reach the repository;
- whether Home Assistant needs a restart to run the version installed, and which version that restart starts: the
  provider's repair issue ``restart_required_<repository id>_<ref>`` (raised by HACS 2.0.5 and by the Marketplace after
  an update), or HACS 2.0.5's ``release_summary`` while it waits for one. Never a version on disk compared with the one
  running: after a rollback by zip the entity keeps the newer one, and no restart would ever clear that;
- on the Marketplace, whether the calling user accepted its warning (it refuses that user's install until then): the
  Marketplace's own config entry, read defensively (another integration's data, no contract with us);
- in the sale edition, whether the licence covers the new version's dashboard (licence/manager.py coverage: the state
  rule itself, run with the new build's release day and LTS line). The owner's edition is never offered the update:
  the public repository holds only the sale package.

It keeps one thing, in hass.data: when the provider's refresh was last claimed in this run (const.DATA_UPDATE_REFRESH).

Imports Home Assistant (its registries) but never the licence: the owner's package has no licence/ (D-037).
"""

from __future__ import annotations

from collections.abc import Mapping
import datetime
import logging
import math
import re
import sys
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import (
    DATA_UPDATE_REFRESH,
    EDITION_SALE,
    UPDATE_PROVIDERS,
    UPDATE_REFRESH_EARLY_AFTER,
    UPDATE_REFRESH_EARLY_MAX,
    UPDATE_REFRESH_EVERY,
    UPDATE_REPOSITORY_ID,
)
from .models import WallDashboardData

_LOGGER = logging.getLogger(__name__)

UPDATE_DOMAIN = "update"
# The repair issue HACS 2.0.5 and the Marketplace raise once an update waits for a restart (in their own domain:
# hacs/repositories/integration.py and marketplace/repositories/integration.py async_post_installation), one per ref:
# the tag ("v1.0.4"), or "tags/v1.0.4" as a ref may be written. The Marketplace removes its own at the next start.
RESTART_ISSUE_PREFIX = f"restart_required_{UPDATE_REPOSITORY_ID}_"
TAGS_PREFIX = "tags/"
# HACS 2.0.5's release_summary while its repository waits for a restart (hacs/update.py); the Marketplace has none.
HACS_RESTART_SUMMARY = "restart of home assistant required"
# UpdateEntityFeature.SPECIFIC_VERSION: update.install takes a version (both providers have it)
FEATURE_SPECIFIC_VERSION = 2
# The Marketplace (Home Assistant 2026.11.0b1, homeassistant/components/marketplace): the acceptances of its warning are
# in its config entry's data, under CONF_WARNING_ACCEPTED ("warning_accepted"): {user id: {"version", "accepted_at"}},
# one that counts being of WARNING_VERSION or later with a date that reads (base.py MarketplaceManager
# .warning_acceptances, warning_accepted). Both names come from its own const module, loaded already wherever its
# update entity is: looked up in sys.modules, never imported from here.
MARKETPLACE = "marketplace"
MARKETPLACE_CONST = "homeassistant.components.marketplace.const"

# A version as the integration's manifest and the release tags write it, the leading "v" taken off ("1.0", "1.0.3",
# "1.1.0-beta.1"): licence/contract.py is_version's characters, starting with a digit (a tag of ours always does).
_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+-]{0,39}")
_KEY_RE = re.compile(r"([0-9]{1,9})\.([0-9]{1,9})(?:\.([0-9]{1,9}))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?")
_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# The request's own forms (contract v2): an LTS line "<1-3 digits>.<1-3 digits>"; a candidate's version this long at most
_LINE_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}")
MAX_REQUEST_VERSION = 64


# --- pure ---------------------------------------------------------------------------------------------------------
def plain_version(value: Any) -> str | None:
    """A version without the release tag's "v" ("v1.0.3" -> "1.0.3"), or None when it is not one."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text[:1] in ("v", "V") and text[1:2].isdigit():
        text = text[1:]
    return text if _VERSION_RE.fullmatch(text) else None


def version_key(value: Any) -> tuple[Any, ...] | None:
    """A version as something to compare, semver's way ("1.0" is "1.0.0"; a pre-release comes before its release),
    or None when it is not of that form."""
    text = plain_version(value)
    match = _KEY_RE.fullmatch(text) if text else None
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    numbers = (int(major), int(minor), int(patch or 0))
    if pre is None:
        return (*numbers, 1, ())
    return (*numbers, 0, tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")))


def is_day(value: Any) -> bool:
    """A real calendar date "YYYY-MM-DD", year 2000 or later (as licence/state.py is_day)."""
    if not isinstance(value, str) or _DAY_RE.fullmatch(value) is None:
        return False
    try:
        return datetime.date(int(value[0:4]), int(value[5:7]), int(value[8:10])).year >= 2000
    except ValueError:
        return False


def _day_form(value: Any) -> bool:
    return isinstance(value, str) and _DAY_RE.fullmatch(value) is not None


def request_candidate(value: Any) -> dict[str, Any] | None:
    """The request's ``candidate`` as the command's schema takes it (contract v2): null, or exactly {"version": a
    string, "released": "YYYY-MM-DD", "lts": null or exactly {"line": "<1-3 digits>.<1-3 digits>", "started":
    "YYYY-MM-DD"}}. Anything else (a key more or less, another type, an lts nested deeper) raises vol.Invalid, which
    Home Assistant answers with invalid_format before the command runs. Only the form: a version or a day that is not
    a real one passes here and is the command's no_candidate (candidate_of)."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"version", "released", "lts"}:
        raise vol.Invalid("candidate: null or {version, released, lts}")
    version, released, lts = value["version"], value["released"], value["lts"]
    if not isinstance(version, str) or not 0 < len(version) <= MAX_REQUEST_VERSION:
        raise vol.Invalid("candidate.version: a version")
    if not _day_form(released):
        raise vol.Invalid("candidate.released: YYYY-MM-DD")
    if lts is not None:
        if not isinstance(lts, dict) or set(lts) != {"line", "started"}:
            raise vol.Invalid("candidate.lts: null or {line, started}")
        if not isinstance(lts["line"], str) or _LINE_RE.fullmatch(lts["line"]) is None:
            raise vol.Invalid("candidate.lts.line: a line, 1.0")
        if not _day_form(lts["started"]):
            raise vol.Invalid("candidate.lts.started: YYYY-MM-DD")
        lts = {"line": lts["line"], "started": lts["started"]}
    return {"version": version, "released": released, "lts": lts}


def candidate_of(raw: Any) -> dict[str, Any] | None:
    """The new version as the dashboard read it from its release notes (docs/ACTUALIZARE-INTEGRARE.md §3):
    {"version", "released", "lts"}, or None when there is none or it is not one (a version that is not one, released
    not a date, lts neither None nor an object). The licence reads lts itself (manager.candidate_stamps)."""
    if not isinstance(raw, Mapping):
        return None
    version, released, lts = plain_version(raw.get("version")), raw.get("released"), raw.get("lts")
    if version is None or not is_day(released) or not (lts is None or isinstance(lts, Mapping)):
        return None
    return {"version": version, "released": released, "lts": dict(lts) if lts is not None else None}


def progress_of(attributes: Mapping[str, Any]) -> tuple[bool, int | float | None]:
    """(in_progress, percentage) from an update entity's attributes. Home Assistant writes in_progress as a boolean
    and update_percentage beside it; an integration that still reports a number in in_progress (HACS 2.0.5 does) is
    turned into the two by Home Assistant, and read here the same way in case it was not."""
    in_progress, percentage = attributes.get("in_progress"), attributes.get("update_percentage")
    if not isinstance(in_progress, bool) and isinstance(in_progress, (int, float)):
        in_progress, percentage = True, in_progress
    if isinstance(percentage, bool) or not isinstance(percentage, (int, float)) or not math.isfinite(percentage) \
            or not 0 <= percentage <= 100:
        percentage = None
    elif isinstance(percentage, float) and percentage.is_integer():
        percentage = int(percentage)
    return in_progress is True, percentage if in_progress is True else None


def newer(a: Any, b: Any) -> bool:
    """Whether version a is newer than version b (False when either cannot be compared)."""
    ka, kb = version_key(a), version_key(b)
    return ka is not None and kb is not None and ka > kb


def ref_version(ref: Any) -> str | None:
    """The version of a restart issue's ref ("v1.0.4" or "tags/v1.0.4" -> "1.0.4"), or None (a branch, a commit)."""
    if not isinstance(ref, str):
        return None
    return plain_version(ref[len(TAGS_PREFIX):] if ref.startswith(TAGS_PREFIX) else ref)


def pending_of(refs: list[str], installed: Any, summary_waits: bool) -> str | None:
    """The version a restart of Home Assistant will start, or None when no restart waits (or it says no version):
    the ref of the provider's restart issue (the one the entity says is installed, when several wait: what is on disk
    now; else the newest), else, with HACS 2.0.5's summary or an issue whose ref is no version, the entity's
    installed_version."""
    on_disk = plain_version(installed)
    if refs:
        versions = [v for v in (ref_version(ref) for ref in refs) if v is not None]
        if on_disk is not None and on_disk in versions:
            return on_disk
        if versions:
            return max(versions, key=lambda v: version_key(v) or ())
        return on_disk
    return on_disk if summary_waits else None


def supports_version(attributes: Mapping[str, Any]) -> bool:
    """Whether the entity's supported_features has UpdateEntityFeature.SPECIFIC_VERSION (update.install's version)."""
    features = attributes.get("supported_features")
    return isinstance(features, int) and not isinstance(features, bool) and bool(features & FEATURE_SPECIFIC_VERSION)


def refresh_timing(last: float | None, early: int, now: float, unavailable: bool) -> str | None:
    """Whether the provider's refresh is due by the clock (the other conditions are async_status's): "due" (never
    claimed in this run, or UPDATE_REFRESH_EVERY since the last claim), "early" (the entity unavailable,
    UPDATE_REFRESH_EARLY_AFTER since the last claim, fewer than UPDATE_REFRESH_EARLY_MAX early claims in this run),
    or None."""
    if last is None or now - last >= UPDATE_REFRESH_EVERY:
        return "due"
    if unavailable and now - last >= UPDATE_REFRESH_EARLY_AFTER and early < UPDATE_REFRESH_EARLY_MAX:
        return "early"
    return None


def _now() -> float:
    """The claim's clock: monotonic, per Home Assistant run (a wall clock set back or forward moves nothing)."""
    return time.monotonic()


# --- in Home Assistant -------------------------------------------------------------------------------------------
@callback
def find_entity(hass: HomeAssistant) -> tuple[str | None, str | None]:
    """(provider, entity_id) of our repository's update entity, or (None, None): none of the providers has it (no
    HACS, no Marketplace, or Nucleum Hub installed from its zip)."""
    registry = er.async_get(hass)
    for provider in UPDATE_PROVIDERS:
        entity_id = registry.async_get_entity_id(UPDATE_DOMAIN, provider, UPDATE_REPOSITORY_ID)
        if entity_id:
            return provider, entity_id
    return None, None


@callback
def restart_refs(hass: HomeAssistant) -> list[str]:
    """The refs of the repair issues "restart required" HACS or the Marketplace raised for our repository in this run
    (active: one loaded from the disk that nothing raised again is not)."""
    issues = ir.async_get(hass).issues
    return [
        issue_id[len(RESTART_ISSUE_PREFIX):]
        for (domain, issue_id), issue in issues.items()
        if domain in UPDATE_PROVIDERS and isinstance(issue_id, str) and issue_id.startswith(RESTART_ISSUE_PREFIX)
        and getattr(issue, "active", True)
    ]


@callback
def warning_accepted(hass: HomeAssistant, provider: str | None, user_id: Any) -> bool | None:
    """On the Marketplace, whether this user accepted its warning, as its own rule reads it; None for HACS, without a
    user, and on any doubt (its module not loaded, no single config entry, a record of another form). Never raises:
    another integration's private data."""
    if provider != MARKETPLACE or not isinstance(user_id, str) or not user_id:
        return None
    try:
        const = sys.modules.get(MARKETPLACE_CONST)
        key, current = getattr(const, "CONF_WARNING_ACCEPTED", None), getattr(const, "WARNING_VERSION", None)
        if not isinstance(key, str) or not isinstance(current, int) or isinstance(current, bool):
            return None
        entries = hass.config_entries.async_entries(MARKETPLACE)
        if len(entries) != 1:
            return None
        acceptances = entries[0].data.get(key, {})
        if not isinstance(acceptances, Mapping):
            return None
        record = acceptances.get(user_id)
        if record is None:
            return False
        if not isinstance(record, Mapping):
            return None
        version, accepted_at = record.get("version"), record.get("accepted_at")
        if not isinstance(version, int) or isinstance(version, bool) or not isinstance(accepted_at, str):
            return None
        return version >= current and dt_util.parse_datetime(accepted_at) is not None
    except Exception:  # noqa: BLE001 (not ours to judge: unknown)
        _LOGGER.debug("The Marketplace's warning could not be read", exc_info=True)
        return None


@callback
def async_status(hass: HomeAssistant, data: WallDashboardData, raw_candidate: Any = None,
                 claim_refresh: bool = False, user_id: Any = None) -> dict[str, Any]:
    """The answer of ``wall_dashboard/update/status`` (docs/ACTUALIZARE-INTEGRARE.md §1). With claim_refresh, a
    refresh that is due is claimed for this caller (refresh_claimed), and due for no one else until it is again."""
    provider, entity_id = find_entity(hass)
    state = hass.states.get(entity_id) if entity_id else None
    attributes: Mapping[str, Any] = state.attributes if state is not None else {}
    running = plain_version(data.version)
    installed = plain_version(attributes.get("installed_version")) or running
    latest_raw = attributes.get("latest_version")
    latest = plain_version(latest_raw)
    in_progress, percentage = progress_of(attributes)
    summary = attributes.get("release_summary")
    summary_waits = isinstance(summary, str) and HACS_RESTART_SUMMARY in summary.lower()
    refs = restart_refs(hass)
    restart_needed = bool(refs) or summary_waits
    edition = "sale" if data.edition == EDITION_SALE else "owner"
    candidate = candidate_of(raw_candidate)
    covered, reason, until = _coverage(data, edition, candidate)
    refresh_due, refresh_claimed = _refresh(hass, provider, state, in_progress, restart_needed, claim_refresh)
    return {
        "provider": provider,
        "repository_id": UPDATE_REPOSITORY_ID,
        "entity_id": entity_id,
        "installed": installed,
        "latest": latest,
        # offered by Home Assistant (newer than the version on disk, not skipped) and newer than the one running: a
        # zip copied over an older install from HACS is not offered its own version again. Never in the owner's
        # edition: the public repository carries only the sale package, so the provider would install the licensed
        # build over the owner's (D-037); the owner's package is copied by hand. An install or a restart that waits
        # is still said (in_progress, restart_needed)
        "available": (edition == "sale" and state is not None and state.state == "on" and latest is not None
                      and (version_key(running) is None or newer(latest, running))),
        "in_progress": in_progress,
        "percentage": percentage,
        "restart_needed": restart_needed,
        "covered": covered,
        "reason": reason,
        "covered_until": until,
        "edition": edition,
        "pending_version": pending_of(refs, attributes.get("installed_version"), summary_waits),
        "latest_raw": latest_raw if isinstance(latest_raw, str) else None,
        "supports_version": supports_version(attributes),
        "warning_accepted": warning_accepted(hass, provider, user_id),
        "refresh_due": refresh_due,
        "refresh_claimed": refresh_claimed,
    }


@callback
def _refresh(hass: HomeAssistant, provider: str | None, state: Any, in_progress: bool, restart_needed: bool,
             claim: bool) -> tuple[bool, bool]:
    """(refresh_due, refresh_claimed): due only with Home Assistant running, a provider whose update entity has a
    state, no install under way and no restart waiting, and then by refresh_timing; claimed (and kept in hass.data)
    when the caller asked to claim a refresh that was due. The claimer's answer still says due: what it found."""
    if (getattr(hass, "state", None) != CoreState.running or provider is None or state is None or in_progress
            or restart_needed):
        return False, False
    kept = hass.data.get(DATA_UPDATE_REFRESH)
    if not isinstance(kept, dict):
        kept = hass.data[DATA_UPDATE_REFRESH] = {"last": None, "early": 0}
    now = _now()
    timing = refresh_timing(kept["last"], kept["early"], now, state.state == "unavailable")
    if timing is None:
        return False, False
    if not claim:
        return True, False
    kept["last"] = now
    if timing == "early":
        kept["early"] += 1
    return True, True


def _coverage(data: WallDashboardData, edition: str,
              candidate: dict[str, Any] | None) -> tuple[bool | None, str | None, str | None]:
    """(covered, reason, covered_until): the owner's edition covers every version; the sale edition asks its licence
    (manager.coverage). A licence that is not running cannot judge: the dashboard stays fully usable (FULL,
    broken_environment, plan §4.1 p.5), so the new version is not held back either."""
    licence = data.licence if edition == "sale" else None
    if licence is not None:
        try:
            said = licence.coverage(candidate)
            return said["covered"], said["reason"], said["until"]
        except Exception:  # noqa: BLE001 (a mistake of ours: answered as a licence that cannot judge)
            _LOGGER.exception("The Nucleum Hub licence could not say whether it covers the new version")
    if candidate is None:
        return None, "no_candidate", None
    return True, None, None
