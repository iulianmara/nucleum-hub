"""Adding Nucleum Hub in Settings › Devices & services: one per Home Assistant, nothing to fill in.

In the sale edition's package the one step is "trial" (D-081, the EULA's trial clause): the same text, then that the
60-minute trial starts at Submit, that the integration then contacts the licence server, and links to the EULA and the
privacy notice; Submit accepts the EULA for the trial. The owner's package keeps "user" (no licence, no trial).

Configure, in the sale edition's package only, is the licence (licence/flow.py): the key pasted, the licence checked
or removed from this home. The owner's package has no licence folder (D-037), so it has no Configure either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import DOMAIN, NAME

# Whether this package carries the licence (tools/package.mjs leaves the folder out of the owner's). Read once, when
# Home Assistant imports this module (in its executor), so the answer below needs no disk.
LICENCE_IN_PACKAGE = (Path(__file__).parent / "licence" / "__init__.py").is_file()


class WallDashboardConfigFlow(ConfigFlow, domain=DOMAIN):
    """One step, no fields: the description says what the integration does (strings.json).

    ``single_config_entry`` in manifest.json already stops a second one; the check below keeps the
    answer the same on a Home Assistant that would not.
    """

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if LICENCE_IN_PACKAGE:
            return await self.async_step_trial()
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})
        return self.async_show_form(step_id="user")

    async def async_step_trial(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The sale edition's step: its text names the trial, the licence server, the EULA and the privacy notice."""
        if user_input is not None:
            return self.async_create_entry(title=NAME, data={})
        return self.async_show_form(step_id="trial")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """The licence's flow, which the licence put in the entry's runtime data when it started (never imported
        here: plan §4.2); without it, a flow that only says the licence is not running."""
        flow = getattr(getattr(config_entry, "runtime_data", None), "licence_flow", None)
        return flow() if flow is not None else LicenceNotRunning()

    @classmethod
    @callback
    def async_supports_options_flow(cls, config_entry: ConfigEntry) -> bool:
        """Configure only where there is a licence to configure: the sale edition's package."""
        return LICENCE_IN_PACKAGE


class LicenceNotRunning(OptionsFlow):
    """Configure while the licence is not running: Nucleum Hub not loaded, or its licence could not start (the log
    says why; the dashboard then stays fully usable)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="licence_not_running")
