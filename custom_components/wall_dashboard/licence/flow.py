"""The licence in Home Assistant's settings: Settings › Devices & services › Nucleum Hub › Configure (the key's e-mail
says so, server/worker/templates). The dashboard's activation screen sends the admin here (D-065).

- Without a licence bound here, the form to paste the key opens at once, with the state above it: the key as it came
  in the e-mail (any case, spaces, O for 0 all work: contract.canonical_key) and whether this Home Assistant is the
  home or its test instance (D-027, D-068). "Active in another home" asks whether to move it here, with the day that
  home was last seen and the moves left (server/README.md §10.9).
- With one, a menu: enter another key, check the licence now, or remove it from this home (the release: the token and
  the nonce go, and the licence can be activated elsewhere).

The texts are in the integration's translations (strings.json, the 12 languages). The state's sentence is put
together here from the ``selector`` part of them, read in Home Assistant's language: a flow's own texts cannot change
with the state, and the dashboard's windows say the same in the dashboard's catalogue. Finished requests end the flow
with a message (an abort, as Home Assistant's own flows do after a re-authentication): nothing is saved in the
entry's options, and the key is never kept beyond this flow.

Imported with the manager, in the executor, only in the sale edition; config_flow.py reaches it through the entry's
runtime data (``licence_flow``), never by an import.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from .contract import ROLES
from .manager import LicenceManager, Outcome

# The errors shown on the key's field; every other one is about the request as a whole.
KEY_ERRORS = ("invalid_key", "unknown_key")
# A check that could not reach a decision: the licence goes on as it was.
CHECK_FAILED = {"cannot_connect": "check_failed", "rate_limited": "check_later", "not_configured": "not_configured",
                "identity": "check_failed", "unknown": "check_failed"}

# The state's sentence (selector.licence_status in strings.json) for each state and reason.
_ACTIVATE = {"invalid_token": "invalid", "no_home": "invalid", "other_home": "moved", "no_token": "no_token",
             "trial_over": "trial_over", "subscription_expired": "subscription_expired",
             "staging_unverified": "staging_unverified", "life_unverified": "life_unverified", "moved": "moved",
             "refunded": "refunded", "revoked": "revoked", "expired": "expired"}


def status_code(payload: dict[str, Any]) -> str:
    """The key of the state's sentence in ``selector.licence_status.options``."""
    state, reason = payload.get("state"), payload.get("reason")
    if payload.get("trial_pending"):
        return "trial_pending"
    if payload.get("lost"):
        return "lost"
    if state == "FULL":
        if reason == "broken_environment":
            return "broken_environment"
        return "full" if payload.get("until") is None else "full_until"
    if state == "TRIAL":
        return "trial"
    if state == "GRACE":
        return reason if reason in ("payment_due", "provisional", "renewal_due") else "renewal_due"
    if state == "FALLBACK":
        return "kept_lts"
    if state == "UNCOVERED":
        return "uncovered"
    return _ACTIVATE.get(reason, "no_token")


def _when(seconds: Any) -> str:
    """A moment in Home Assistant's time zone, the same in every language: 2026-10-21 14:05."""
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return ""
    return dt_util.as_local(dt_util.utc_from_timestamp(seconds)).strftime("%Y-%m-%d %H:%M")


def _moves(value: int | None) -> str:
    return "—" if value is None else str(value)


def _site_language(language: Any) -> str:
    """Home Assistant's language as the site names its own (en-GB is en; pt-BR stays pt-BR); contract.site_link gives
    the English page for any other."""
    if not isinstance(language, str):
        return "en"
    return language if language == "pt-BR" else language.split("-")[0]


async def _words(hass: Any) -> dict[str, str]:
    """The integration's ``selector`` texts in Home Assistant's language (English for a missing one), keyed
    ``<selector>.options.<option>``; empty when they cannot be read (the codes are then shown as they are)."""
    try:
        found = await async_get_translations(hass, hass.config.language, "selector", [DOMAIN])
    except Exception:  # noqa: BLE001 (the texts are a nicety; the flow works without them)
        return {}
    prefix = f"component.{DOMAIN}.selector."
    return {key[len(prefix):]: text for key, text in found.items() if key.startswith(prefix)}


async def describe(hass: Any, manager: LicenceManager) -> str:
    """The state in a few lines: its sentence, the plan and the role, the notice, the "two homes" message, the link to
    buy or renew (the site's page in Home Assistant's language), the last check."""
    payload = manager.payload(_site_language(hass.config.language))
    words = await _words(hass)

    def word(group: str, option: str) -> str:
        return words.get(f"{group}.options.{option}", option)

    code = status_code(payload)
    lines = ["**" + word("licence_status", code).replace("{until}", _when(payload.get("until"))) + "**"]
    plan, role = payload.get("plan"), payload.get("role")
    if plan is not None and code != "broken_environment":
        name = word("licence_plan", plan if plan in ("sub", "life", "trial") else "other")
        lines.append(name if plan == "trial" or role is None else f"{name} · {word('role', role)}")
    if payload.get("notice") == "verify_security":
        lines.append(word("licence_status", "verify_security"))
    if payload.get("cloned"):
        lines.append(word("licence_status", "cloned"))
    if payload.get("link") and payload.get("link_kind"):
        lines.append(word("licence_link", payload["link_kind"]).replace("{link}", payload["link"]))
    if manager.checked is not None:
        lines.append(word("licence_status", "checked").replace("{when}", _when(manager.checked)))
    return "\n\n".join(lines)


def _activate_schema() -> vol.Schema:
    return vol.Schema({
        vol.Required("key"): str,
        vol.Required("role", default="prod"): SelectSelector(
            SelectSelectorConfig(options=list(ROLES), translation_key="role", mode=SelectSelectorMode.LIST)
        ),
    })


class LicenceOptionsFlow(OptionsFlow):
    """Configure, in the sale edition (see the module's docstring)."""

    def __init__(self) -> None:
        self._key: str | None = None
        self._role = "prod"
        self._elsewhere = Outcome()

    def _manager(self) -> LicenceManager | None:
        manager = getattr(getattr(self.config_entry, "runtime_data", None), "licence", None)
        return manager if isinstance(manager, LicenceManager) else None

    async def _end(self, reason: str, manager: LicenceManager, **placeholders: str) -> ConfigFlowResult:
        return self.async_abort(reason=reason, description_placeholders={
            "status": await describe(self.hass, manager), **placeholders})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="licence_not_running")
        manager.async_request_check()  # the settings opened: a refresh in the background, within the budget
        if not manager.holds_licence:
            return await self.async_step_activate()
        return self.async_show_menu(step_id="init", menu_options=["activate", "check", "release"],
                                    description_placeholders={"status": await describe(self.hass, manager)})

    async def async_step_activate(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="licence_not_running")
        errors: dict[str, str] = {}
        minutes = ""
        if user_input is not None:
            self._key = str(user_input.get("key") or "").strip()
            self._role = user_input.get("role") if user_input.get("role") in ROLES else "prod"
            outcome = await manager.async_activate(self._key, self._role)
            if outcome.ok:
                self._key = None
                return await self._end("activated", manager)
            if outcome.error == "active_elsewhere":
                self._elsewhere = outcome
                return await self._show_move({})
            errors["key" if outcome.error in KEY_ERRORS else "base"] = outcome.error or "unknown"
            minutes = str(outcome.minutes or "")
        schema = self.add_suggested_values_to_schema(_activate_schema(), {"key": self._key or "", "role": self._role})
        return self.async_show_form(step_id="activate", data_schema=schema, errors=errors,
                                    description_placeholders={"status": await describe(self.hass, manager),
                                                              "minutes": minutes})

    async def async_step_move(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """"Move it here?" answered yes: the same activation, with move."""
        manager = self._manager()
        if manager is None or self._key is None:
            return self.async_abort(reason="licence_not_running")
        errors: dict[str, str] = {}
        if user_input is not None:
            outcome = await manager.async_activate(self._key, self._role, move=True)
            if outcome.ok:
                self._key = None
                return await self._end("activated", manager)
            if outcome.error == "active_elsewhere":
                self._elsewhere = outcome  # held by yet another home meanwhile: ask again with its facts
            else:
                errors["base"] = outcome.error or "unknown"
                self._elsewhere = Outcome(minutes=outcome.minutes, last_seen=self._elsewhere.last_seen,
                                          moves_left=self._elsewhere.moves_left)
        return await self._show_move(errors)

    async_step_move_staging = async_step_move

    async def _show_move(self, errors: dict[str, str]) -> ConfigFlowResult:
        facts = self._elsewhere
        return self.async_show_form(
            step_id="move_staging" if self._role == "staging" else "move",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"last_seen": facts.last_seen or "—", "moves_left": _moves(facts.moves_left),
                                      "minutes": str(facts.minutes or "")},
        )

    async def async_step_check(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="licence_not_running")
        outcome = await manager.async_check()
        failed = CHECK_FAILED.get(outcome.error or "")
        if failed is not None:
            return await self._end(failed, manager, minutes=str(outcome.minutes or ""))
        return await self._end("checked", manager)  # a 200, or an answer the state now shows

    async def async_step_release(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        manager = self._manager()
        if manager is None:
            return self.async_abort(reason="licence_not_running")
        if not manager.holds_licence:
            return await self._end("not_licensed", manager)
        errors: dict[str, str] = {}
        minutes = ""
        staging = manager.role == "staging"
        if user_input is not None:
            outcome = await manager.async_release()
            if outcome.ok:
                return await self._end("released_staging" if staging else "released", manager,
                                       moves_left=_moves(outcome.moves_left))
            errors["base"] = outcome.error or "unknown"
            minutes = str(outcome.minutes or "")
        return self.async_show_form(step_id="release_staging" if staging else "release", data_schema=vol.Schema({}),
                                    errors=errors, description_placeholders={
                                        "moves_left": _moves(manager.moves_left), "minutes": minutes})

    async_step_release_staging = async_step_release
