"""Config flow per Thesan Aircare."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import local as vmc_local
from .api import AircareApiError, AircareAuthError, AircareClient
from .const import CONF_EMAIL, CONF_LOCAL, CONF_PASSWORD, DOMAIN

ONLY_CLOUD = "solo_cloud"

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AircareConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce l'aggiunta dell'integrazione dall'UI."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AircareOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
            self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = AircareClient(
                user_input[CONF_EMAIL], user_input[CONF_PASSWORD], session
            )
            try:
                await client.async_login(raise_auth=True)
            except AircareAuthError:
                errors["base"] = "invalid_auth"
            except AircareApiError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Aircare ({user_input[CONF_EMAIL]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )


class AircareOptionsFlow(config_entries.OptionsFlow):
    """Abbina ogni VMC alla macchina trovata in rete locale (o la lascia sul cloud)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        coord = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coord is None:
            return self.async_abort(reason="not_loaded")
        by_label = {label: uuid for uuid, label in coord.labels.items()}

        if user_input is not None:
            local = {
                by_label[label]: serial
                for label, serial in user_input.items()
                if label in by_label and serial != ONLY_CLOUD
            }
            if len(set(local.values())) != len(local):
                return await self._show(coord, by_label, {"base": "duplicate"})
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CONF_LOCAL: local}
            )
        return await self._show(coord, by_label, {})

    async def _show(self, coord, by_label, errors):
        found = await self.hass.async_add_executor_job(vmc_local.scan_described)
        coord.set_hosts({serial: host for serial, (host, _) in found.items()})
        current = self.config_entry.options.get(CONF_LOCAL) or {}
        choices = {ONLY_CLOUD: "Solo cloud"}
        choices.update({serial: text for serial, (_, text) in found.items()})
        for serial in current.values():
            choices.setdefault(serial, f"seriale {serial} (ora non trovata in rete)")
        schema = vol.Schema({
            vol.Required(label, default=current.get(uuid, ONLY_CLOUD)): vol.In(choices)
            for label, uuid in sorted(by_label.items())
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
