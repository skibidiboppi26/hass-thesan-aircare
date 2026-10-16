"""Config flow per Thesan Aircare."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AircareApiError, AircareAuthError, AircareClient
from .const import CONF_EMAIL, CONF_PASSWORD, DOMAIN

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AircareConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Gestisce l'aggiunta dell'integrazione dall'UI."""

    VERSION = 1

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
