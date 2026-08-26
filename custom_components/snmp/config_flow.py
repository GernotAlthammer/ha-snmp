"""Config flow for the SNMP integration.

Two-tier setup, matching how SNMP is actually used on a network:

* A **config entry** represents one device, identified by its IP address
  (host) plus the SNMP settings needed to talk to it (port, protocol
  version, and - for SNMPv3 - the user credentials).
* Each config entry's **options** hold a list of *sensors* that belong to
  that device. Sensors are added/removed via the "Configure" flow and each
  one is parametrized with exactly the three fields requested: Name,
  Community and Base OID.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_AUTH_KEY,
    CONF_AUTH_PROTOCOL,
    CONF_BASEOID,
    CONF_COMMUNITY,
    CONF_PRIV_KEY,
    CONF_PRIV_PROTOCOL,
    CONF_SENSOR_ID,
    CONF_SENSORS,
    CONF_VERSION,
    DEFAULT_COMMUNITY,
    DEFAULT_PORT,
    DOMAIN,
    MAP_AUTH_PROTOCOLS,
    MAP_PRIV_PROTOCOLS,
)

VERSION_OPTIONS = ["1", "2c", "3"]
AUTH_PROTOCOL_OPTIONS = list(MAP_AUTH_PROTOCOLS)
PRIV_PROTOCOL_OPTIONS = list(MAP_PRIV_PROTOCOLS)


def _device_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): TextSelector(),
            vol.Optional(
                CONF_PORT, default=defaults.get(CONF_PORT, int(DEFAULT_PORT))
            ): cv.port,
            vol.Required(
                CONF_VERSION, default=defaults.get(CONF_VERSION, "2c")
            ): SelectSelector(
                SelectSelectorConfig(options=VERSION_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
        }
    )


def _auth_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): TextSelector(),
            vol.Optional(
                CONF_AUTH_PROTOCOL, default=defaults.get(CONF_AUTH_PROTOCOL, "none")
            ): SelectSelector(
                SelectSelectorConfig(options=AUTH_PROTOCOL_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_AUTH_KEY, default=defaults.get(CONF_AUTH_KEY, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_PRIV_PROTOCOL, default=defaults.get(CONF_PRIV_PROTOCOL, "none")
            ): SelectSelector(
                SelectSelectorConfig(options=PRIV_PROTOCOL_OPTIONS, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_PRIV_KEY, default=defaults.get(CONF_PRIV_KEY, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
        }
    )


def _sensor_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME): TextSelector(),
            vol.Optional(CONF_COMMUNITY, default=DEFAULT_COMMUNITY): TextSelector(),
            vol.Required(CONF_BASEOID): TextSelector(),
        }
    )


class SNMPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding a new SNMP device."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._device_data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """First step: identify the device by IP address and SNMP version."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            self._device_data = dict(user_input)

            if user_input[CONF_VERSION] == "3":
                return await self.async_step_auth()

            return self.async_create_entry(
                title=host,
                data=self._device_data,
                options={CONF_SENSORS: []},
            )

        return self.async_show_form(
            step_id="user", data_schema=_device_schema(), errors=errors
        )

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Collect SNMPv3 user credentials."""
        if user_input is not None:
            # Empty strings mean "not set" - store as None so downstream
            # logic can fall back to "no auth"/"no privacy".
            cleaned = {k: (v or None) for k, v in user_input.items()}
            self._device_data.update(cleaned)
            return self.async_create_entry(
                title=self._device_data[CONF_HOST],
                data=self._device_data,
                options={CONF_SENSORS: []},
            )

        return self.async_show_form(step_id="auth", data_schema=_auth_schema())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SNMPOptionsFlow:
        """Get the options flow for managing this device's sensors."""
        return SNMPOptionsFlow()


class SNMPOptionsFlow(OptionsFlow):
    """Manage the list of sensors that belong to an SNMP device."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._sensors: list[dict[str, Any]] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Show the sensor-management menu for this device."""
        self._sensors = list(self.config_entry.options.get(CONF_SENSORS, []))

        menu_options = ["add_sensor"]
        if self._sensors:
            menu_options.append("remove_sensor")

        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_add_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Add a sensor: Name, Community and Base OID."""
        errors: dict[str, str] = {}

        if user_input is not None:
            sensor = {
                CONF_SENSOR_ID: uuid4().hex[:8],
                CONF_NAME: user_input[CONF_NAME],
                CONF_COMMUNITY: user_input.get(CONF_COMMUNITY, DEFAULT_COMMUNITY),
                CONF_BASEOID: user_input[CONF_BASEOID],
            }
            self._sensors.append(sensor)
            return self.async_create_entry(
                title="", data={CONF_SENSORS: self._sensors}
            )

        return self.async_show_form(
            step_id="add_sensor", data_schema=_sensor_schema(), errors=errors
        )

    async def async_step_remove_sensor(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Remove one or more existing sensors."""
        if user_input is not None:
            to_remove = set(user_input["sensors"])
            self._sensors = [
                s for s in self._sensors if s[CONF_SENSOR_ID] not in to_remove
            ]
            return self.async_create_entry(
                title="", data={CONF_SENSORS: self._sensors}
            )

        schema = vol.Schema(
            {
                vol.Required("sensors"): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {
                                "value": s[CONF_SENSOR_ID],
                                "label": f"{s[CONF_NAME]} ({s[CONF_BASEOID]})",
                            }
                            for s in self._sensors
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="remove_sensor", data_schema=schema)
