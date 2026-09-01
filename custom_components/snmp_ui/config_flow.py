"""Config flow for the SNMP integration.

Two-tier setup, matching how SNMP is actually used on a network:

* A **config entry** represents one device, identified by its IP address
  (host) plus the SNMP settings needed to talk to it (port, protocol
  version, and - for SNMPv3 - the user credentials).
* Each config entry's **options** hold a list of *sensors* that belong to
  that device. Sensors are added/removed via the "Configure" flow and each
  one is parametrized with exactly the three fields requested: Name,
  Community and Base OID.

On top of the manual path, devices can also be found automatically:

* A network scan for **printers** (`async_step_discover_printers`) probes a
  subnet for devices that answer SNMP's Host Resources "printer status" OID,
  and builds a full sensor set for each selected printer automatically
  (model, serial number, status, page count and one "Level"/"Max" sensor
  pair per toner/ink marker found via an SNMP table walk).
* A network scan for **network switches** (`async_step_discover_switches`)
  probes a subnet for devices that support the standard Bridge-MIB (i.e.
  answer `dot1dBaseNumPorts`), and builds a sensor set for each selected
  switch automatically (description, port count and one "Status" sensor per
  physical port, discovered via an IF-MIB table walk).

Either way, the user never has to type in an OID by hand.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigFlow, OptionsFlow
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
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    CONF_AUTH_KEY,
    CONF_AUTH_PROTOCOL,
    CONF_BASEOID,
    CONF_COMMUNITY,
    CONF_MODEL,
    CONF_PRIV_KEY,
    CONF_PRIV_PROTOCOL,
    CONF_SENSOR_ID,
    CONF_SENSORS,
    CONF_SERIAL_NUMBER,
    CONF_STATE_CLASS,
    CONF_SUBNET,
    CONF_UNIT,
    CONF_VALUE_MAP,
    CONF_VALUE_TYPE,
    CONF_VERSION,
    DEFAULT_COMMUNITY,
    DEFAULT_PORT,
    DOMAIN,
    MAP_AUTH_PROTOCOLS,
    MAP_PRIV_PROTOCOLS,
    OID_CONSOLE_DISPLAY_TEXT,
    OID_DETECTED_ERROR_STATE,
    OID_DEVICE_MODEL,
    OID_DOT1D_BASE_NUM_PORTS,
    OID_IF_DESCR,
    OID_IF_OPER_STATUS,
    OID_MARKER_SUPPLIES_DESCRIPTION,
    OID_MARKER_SUPPLIES_LEVEL,
    OID_PRINTER_NAME,
    OID_PRINTER_STATUS,
    OID_SERIAL_NUMBER,
    OID_SYS_DESCR,
    OID_SYS_NAME,
    OID_TOTAL_PAGES,
    SCAN_CONCURRENCY,
    SCAN_MAX_HOSTS,
    SCAN_TIMEOUT,
    UNIT_PAGES,
    UNIT_PERCENT,
)
from .util import async_snmp_probe, async_snmp_walk_table

_LOGGER = logging.getLogger(__name__)

VERSION_OPTIONS = ["1", "2c", "3"]
DISCOVERY_VERSION_OPTIONS = ["1", "2c"]  # SNMPv3 needs per-device credentials, not scannable
AUTH_PROTOCOL_OPTIONS = list(MAP_AUTH_PROTOCOLS)
PRIV_PROTOCOL_OPTIONS = list(MAP_PRIV_PROTOCOLS)

# Standard Host Resources MIB (RFC 2790) enum for hrPrinterStatus - the same
# meaning on every vendor's printer, since it's part of the SNMP standard
# itself rather than anything device-specific.
PRINTER_STATUS_TEXT: dict[str, str] = {
    "1": "Other",
    "2": "Unknown",
    "3": "Idle",
    "4": "Printing",
    "5": "Warming Up",
}

# prtMarkerSuppliesLevel (RFC 3805) reserves negative values as sentinels
# instead of a percentage - e.g. some printers report -1/-2/-3 for a
# cartridge whose exact fill level they can't measure. Percentages (0-100)
# pass through this map untouched and keep the numeric % display; only
# these special values get replaced with text.
MARKER_LEVEL_SENTINEL_TEXT: dict[str, str] = {
    "-1": "Not Applicable",
    "-2": "Some Remaining (Unknown Level)",
    "-3": "Unknown",
}

# Standard IF-MIB (RFC 2863) enum for ifOperStatus - likewise the same
# meaning for any switch/router port regardless of vendor.
IF_OPER_STATUS_TEXT: dict[str, str] = {
    "1": "Up",
    "2": "Down",
    "3": "Testing",
    "4": "Unknown",
    "5": "Dormant",
    "6": "Not Present",
    "7": "Lower Layer Down",
}


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
            vol.Optional(CONF_UNIT, default=""): TextSelector(),
        }
    )


def _discover_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_SUBNET, default=defaults.get(CONF_SUBNET, "")
            ): TextSelector(),
            vol.Optional(
                CONF_VERSION, default=defaults.get(CONF_VERSION, "2c")
            ): SelectSelector(
                SelectSelectorConfig(
                    options=DISCOVERY_VERSION_OPTIONS, mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Optional(
                CONF_COMMUNITY, default=defaults.get(CONF_COMMUNITY, DEFAULT_COMMUNITY)
            ): TextSelector(),
            vol.Optional(
                CONF_PORT, default=defaults.get(CONF_PORT, int(DEFAULT_PORT))
            ): cv.port,
        }
    )


def _discover_select_schema(scan_results: list[dict[str, Any]]) -> vol.Schema:
    options = [
        {"value": result[CONF_HOST], "label": f"{result['label']} ({result[CONF_HOST]})"}
        for result in scan_results
    ]
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=[o["value"] for o in options]): SelectSelector(
                SelectSelectorConfig(
                    options=options, multiple=True, mode=SelectSelectorMode.LIST
                )
            )
        }
    )


async def _build_printer_sensors(
    hass: Any, host: str, port: int, version: str, community: str
) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
    """Probe one printer and build its sensor set automatically.

    Returns a display name for the device, a ready-to-store list of sensor
    dicts (same shape as sensors added manually via the options flow) - just
    Status, Error State, Display, Total Pages and one "Level" sensor (in %)
    per toner/ink marker found via an SNMP table walk - plus a `device_data`
    dict with the printer's Model and Serial Number, meant to be stored on
    the config entry itself and shown under "Device info" rather than as
    sensors.

    Max-capacity sensors are intentionally left out by default (the level is
    already a percentage in practice); add one manually via the options flow
    if a specific printer needs it. Any field the printer doesn't support is
    silently skipped.
    """

    async def probe(oid: str) -> str | None:
        return await async_snmp_probe(hass, host, port, version, community, oid)

    model, serial, printer_name, status, pages, error_state, display_text = (
        await asyncio.gather(
            probe(OID_DEVICE_MODEL),
            probe(OID_SERIAL_NUMBER),
            probe(OID_PRINTER_NAME),
            probe(OID_PRINTER_STATUS),
            probe(OID_TOTAL_PAGES),
            probe(OID_DETECTED_ERROR_STATE),
            probe(OID_CONSOLE_DISPLAY_TEXT),
        )
    )

    display_name = (printer_name or model or host or "").strip() or host

    device_data: dict[str, str] = {}
    if model:
        device_data[CONF_MODEL] = model
    if serial:
        device_data[CONF_SERIAL_NUMBER] = serial

    sensors: list[dict[str, Any]] = []

    def add_sensor(
        label: str,
        value: str | None,
        oid: str,
        unit: str | None = None,
        value_map: dict[str, str] | None = None,
        value_type: str | None = None,
        state_class: str | None = None,
    ) -> None:
        if value is None:
            return
        sensor: dict[str, Any] = {
            CONF_SENSOR_ID: uuid4().hex[:8],
            CONF_NAME: f"{display_name} {label}",
            CONF_COMMUNITY: community,
            CONF_BASEOID: oid,
        }
        if unit:
            sensor[CONF_UNIT] = unit
        if value_map:
            sensor[CONF_VALUE_MAP] = value_map
        if value_type:
            sensor[CONF_VALUE_TYPE] = value_type
        if state_class:
            sensor[CONF_STATE_CLASS] = state_class
        sensors.append(sensor)

    add_sensor("Status", status, OID_PRINTER_STATUS, value_map=PRINTER_STATUS_TEXT)
    add_sensor(
        "Total Pages",
        pages,
        OID_TOTAL_PAGES,
        unit=UNIT_PAGES,
        value_type="int",
        state_class="total_increasing",
    )
    # hrPrinterDetectedErrorState is a bitmask - decoded into a plain-text
    # list of active conditions (e.g. "Low Toner, Door Open") or "OK".
    add_sensor(
        "Error State", error_state, OID_DETECTED_ERROR_STATE, value_type="error_bits"
    )
    # prtConsoleDisplayBufferText is already free text as shown on the
    # printer's own front-panel display (e.g. "Ready", "Paper Jam") - no
    # mapping needed, it comes straight from the device.
    add_sensor("Display", display_text, OID_CONSOLE_DISPLAY_TEXT)

    marker_rows = await async_snmp_walk_table(
        hass, host, port, version, community, OID_MARKER_SUPPLIES_DESCRIPTION
    )

    async def build_marker(suffix: str, description: str) -> None:
        clean_description = (description or "").strip(" \x00") or f"Marker {suffix}"
        level_oid = f"{OID_MARKER_SUPPLIES_LEVEL}.{suffix}"
        level = await probe(level_oid)
        add_sensor(
            f"{clean_description} Level",
            level,
            level_oid,
            unit=UNIT_PERCENT,
            value_map=MARKER_LEVEL_SENTINEL_TEXT,
        )

    await asyncio.gather(
        *(build_marker(suffix, description) for suffix, description in marker_rows)
    )

    return display_name, sensors, device_data


async def _build_switch_sensors(
    hass: Any, host: str, port: int, version: str, community: str
) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
    """Probe one network switch and build its full sensor set automatically.

    Returns a display name for the device, a ready-to-store list of sensor
    dicts, covering the switch's description, port count and one "Status"
    sensor per physical port found via an IF-MIB table walk (raw
    ifOperStatus values: 1=up, 2=down, 3=testing), plus an empty
    `device_data` dict (switches have no Model/Serial Number equivalent
    handled here). Any field the switch doesn't support is silently skipped.
    """

    async def probe(oid: str) -> str | None:
        return await async_snmp_probe(hass, host, port, version, community, oid)

    sys_name, sys_descr, num_ports = await asyncio.gather(
        probe(OID_SYS_NAME),
        probe(OID_SYS_DESCR),
        probe(OID_DOT1D_BASE_NUM_PORTS),
    )

    display_name = (sys_name or sys_descr or host or "").strip() or host

    sensors: list[dict[str, Any]] = []

    def add_sensor(
        label: str,
        value: str | None,
        oid: str,
        value_map: dict[str, str] | None = None,
    ) -> None:
        if value is None:
            return
        sensor: dict[str, Any] = {
            CONF_SENSOR_ID: uuid4().hex[:8],
            CONF_NAME: f"{display_name} {label}",
            CONF_COMMUNITY: community,
            CONF_BASEOID: oid,
        }
        if value_map:
            sensor[CONF_VALUE_MAP] = value_map
        sensors.append(sensor)

    add_sensor("Description", sys_descr, OID_SYS_DESCR)
    add_sensor("Port Count", num_ports, OID_DOT1D_BASE_NUM_PORTS)

    # Switches can have many ports (24/48+), so allow a larger walk than the
    # printer marker walk (which is typically at most 4-5 rows).
    port_rows = await async_snmp_walk_table(
        hass, host, port, version, community, OID_IF_DESCR, max_rows=64
    )

    async def build_port(suffix: str, descr: str) -> None:
        clean_descr = (descr or "").strip(" \x00") or f"Port {suffix}"
        status_oid = f"{OID_IF_OPER_STATUS}.{suffix}"
        status = await probe(status_oid)
        add_sensor(
            f"Port {clean_descr} Status",
            status,
            status_oid,
            value_map=IF_OPER_STATUS_TEXT,
        )

    await asyncio.gather(
        *(build_port(suffix, descr) for suffix, descr in port_rows)
    )

    return display_name, sensors, {}


class SNMPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle adding a new SNMP device."""

    VERSION = 1

    # Class variable: dedupe zeroconf announcements seen across flow instances
    # (a printer can advertise several service types, each triggering a call).
    _zeroconf_seen_hosts: set[str] = set()

    def __init__(self) -> None:
        """Initialize the flow."""
        self._device_data: dict[str, Any] = {}
        self._scan_params: dict[str, Any] = {}
        self._scan_results: list[dict[str, Any]] = []
        self._zeroconf_info: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Let the user pick between manual entry and a network scan."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["manual", "discover_printers", "discover_switches"],
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Identify a single device by IP address and SNMP version."""
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
            step_id="manual", data_schema=_device_schema(), errors=errors
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

    async def async_step_discover_printers(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Scan a subnet for printers (SNMP v1/v2c only)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            subnet = user_input[CONF_SUBNET]
            version = user_input.get(CONF_VERSION, "2c")
            community = user_input.get(CONF_COMMUNITY, DEFAULT_COMMUNITY)
            port = user_input.get(CONF_PORT, int(DEFAULT_PORT))

            try:
                network = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                errors["base"] = "invalid_subnet"
            else:
                hosts = list(network.hosts()) or [network.network_address]
                if len(hosts) > SCAN_MAX_HOSTS:
                    errors["base"] = "subnet_too_large"
                else:
                    results = await self._async_scan_hosts(
                        hosts, version, community, port,
                        detect_oid=OID_PRINTER_STATUS, label_oid=OID_DEVICE_MODEL,
                    )
                    if not results:
                        errors["base"] = "no_printers_found"
                    else:
                        self._scan_results = results
                        self._scan_params = {
                            CONF_VERSION: version,
                            CONF_COMMUNITY: community,
                            CONF_PORT: port,
                        }
                        return await self.async_step_discover_printers_select()

        return self.async_show_form(
            step_id="discover_printers",
            data_schema=_discover_schema(user_input),
            errors=errors,
        )

    async def async_step_discover_switches(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Scan a subnet for network switches (SNMP v1/v2c only)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            subnet = user_input[CONF_SUBNET]
            version = user_input.get(CONF_VERSION, "2c")
            community = user_input.get(CONF_COMMUNITY, DEFAULT_COMMUNITY)
            port = user_input.get(CONF_PORT, int(DEFAULT_PORT))

            try:
                network = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                errors["base"] = "invalid_subnet"
            else:
                hosts = list(network.hosts()) or [network.network_address]
                if len(hosts) > SCAN_MAX_HOSTS:
                    errors["base"] = "subnet_too_large"
                else:
                    results = await self._async_scan_hosts(
                        hosts, version, community, port,
                        detect_oid=OID_DOT1D_BASE_NUM_PORTS, label_oid=OID_SYS_DESCR,
                    )
                    if not results:
                        errors["base"] = "no_switches_found"
                    else:
                        self._scan_results = results
                        self._scan_params = {
                            CONF_VERSION: version,
                            CONF_COMMUNITY: community,
                            CONF_PORT: port,
                        }
                        return await self.async_step_discover_switches_select()

        return self.async_show_form(
            step_id="discover_switches",
            data_schema=_discover_schema(user_input),
            errors=errors,
        )

    async def _async_scan_hosts(
        self,
        hosts: list[Any],
        version: str,
        community: str,
        port: int,
        detect_oid: str,
        label_oid: str,
    ) -> list[dict[str, Any]]:
        """Probe every host in `hosts` concurrently for a matching device.

        A host is considered a match if it answers `detect_oid` (the
        device-type indicator - e.g. hrPrinterStatus for printers,
        dot1dBaseNumPorts for switches). `label_oid` is fetched purely for
        display purposes in the selection list.
        """
        semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)
        existing = {
            entry.data.get(CONF_HOST) for entry in self._async_current_entries()
        }

        async def probe(ip_addr: Any) -> dict[str, Any] | None:
            host = str(ip_addr)
            if host in existing:
                return None
            async with semaphore:
                detected = await async_snmp_probe(
                    self.hass, host, port, version, community, detect_oid,
                    timeout=SCAN_TIMEOUT,
                )
                if detected is None:
                    return None
                label = await async_snmp_probe(
                    self.hass, host, port, version, community, label_oid,
                    timeout=SCAN_TIMEOUT,
                )
            return {CONF_HOST: host, "label": label or host}

        results = await asyncio.gather(*(probe(ip) for ip in hosts))
        return [result for result in results if result]

    async def async_step_discover_printers_select(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Let the user pick which discovered printers to add."""
        if user_input is not None:
            selected_hosts = set(user_input[CONF_HOST])
            selected = [
                r for r in self._scan_results if r[CONF_HOST] in selected_hosts
            ]

            version = self._scan_params[CONF_VERSION]
            community = self._scan_params[CONF_COMMUNITY]
            port = self._scan_params[CONF_PORT]

            built = await asyncio.gather(
                *(
                    _build_printer_sensors(
                        self.hass, result[CONF_HOST], port, version, community
                    )
                    for result in selected
                )
            )

            return await self._async_finish_discovery_select(
                selected, built, port, version
            )

        return self.async_show_form(
            step_id="discover_printers_select",
            data_schema=_discover_select_schema(self._scan_results),
            description_placeholders={"count": str(len(self._scan_results))},
        )

    async def async_step_discover_switches_select(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Let the user pick which discovered switches to add."""
        if user_input is not None:
            selected_hosts = set(user_input[CONF_HOST])
            selected = [
                r for r in self._scan_results if r[CONF_HOST] in selected_hosts
            ]

            version = self._scan_params[CONF_VERSION]
            community = self._scan_params[CONF_COMMUNITY]
            port = self._scan_params[CONF_PORT]

            built = await asyncio.gather(
                *(
                    _build_switch_sensors(
                        self.hass, result[CONF_HOST], port, version, community
                    )
                    for result in selected
                )
            )

            return await self._async_finish_discovery_select(
                selected, built, port, version
            )

        return self.async_show_form(
            step_id="discover_switches_select",
            data_schema=_discover_select_schema(self._scan_results),
            description_placeholders={"count": str(len(self._scan_results))},
        )

    async def _async_finish_discovery_select(
        self,
        selected: list[dict[str, Any]],
        built: list[tuple[str, list[dict[str, Any]], dict[str, str]]],
        port: int,
        version: str,
    ) -> Any:
        """Turn selected+built devices into config entries.

        This flow can only finish with a single config entry, so an
        independent background flow is kicked off for every additional
        device and this one finishes with the first.
        """
        prepared = [
            {
                "data": {
                    CONF_HOST: result[CONF_HOST],
                    CONF_PORT: port,
                    CONF_VERSION: version,
                    **device_data,
                },
                "title": display_name,
                "options": {CONF_SENSORS: sensors},
            }
            for result, (display_name, sensors, device_data) in zip(selected, built)
        ]

        for extra in prepared[1:]:
            self.hass.async_create_task(
                self.hass.config_entries.flow.async_init(
                    DOMAIN, context={"source": SOURCE_IMPORT}, data=extra
                )
            )

        return await self.async_step_import(prepared[0])

    async def async_step_import(self, import_data: dict[str, Any]) -> Any:
        """Create one config entry from fully pre-built discovery data.

        Not shown to the user - used internally so that selecting several
        discovered printers at once can create several config entries.
        """
        data = import_data["data"]
        await self.async_set_unique_id(f"{data[CONF_HOST]}:{data[CONF_PORT]}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=import_data["title"],
            data=data,
            options=import_data["options"],
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> Any:
        """Handle an incoming zeroconf/mDNS announcement.

        Many network printers advertise themselves via mDNS (e.g. as
        `_printer._tcp`, `_ipp._tcp`...). This confirms the announcement is
        actually SNMP-capable before showing a discovery card - a printer
        can easily support IPP/AirPrint without SNMP.
        """
        host = discovery_info.host
        if not host:
            return self.async_abort(reason="not_printer")

        if host in SNMPConfigFlow._zeroconf_seen_hosts:
            return self.async_abort(reason="already_in_progress")
        SNMPConfigFlow._zeroconf_seen_hosts.add(host)

        port = int(DEFAULT_PORT)
        model: str | None = None
        working_version: str | None = None

        for version in ("2c", "1"):
            status = await async_snmp_probe(
                self.hass, host, port, version, DEFAULT_COMMUNITY,
                OID_PRINTER_STATUS, timeout=2.5,
            )
            if status is not None:
                working_version = version
                model = await async_snmp_probe(
                    self.hass, host, port, version, DEFAULT_COMMUNITY,
                    OID_DEVICE_MODEL, timeout=2.5,
                )
                break

        if working_version is None:
            # Responds to mDNS but not to SNMP with the default community -
            # nothing we can auto-configure, so don't show a discovery card.
            SNMPConfigFlow._zeroconf_seen_hosts.discard(host)
            return self.async_abort(reason="not_printer")

        await self.async_set_unique_id(f"{host}:{port}")
        self._abort_if_unique_id_configured()

        self._zeroconf_info = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_VERSION: working_version,
            CONF_COMMUNITY: DEFAULT_COMMUNITY,
            "model": model or host,
        }
        self.context["title_placeholders"] = {"name": model or host}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> Any:
        """Confirm adding a printer found via zeroconf, building its sensors."""
        if user_input is not None:
            info = self._zeroconf_info
            display_name, sensors, device_data = await _build_printer_sensors(
                self.hass,
                info[CONF_HOST],
                info[CONF_PORT],
                info[CONF_VERSION],
                info[CONF_COMMUNITY],
            )
            return self.async_create_entry(
                title=display_name,
                data={
                    CONF_HOST: info[CONF_HOST],
                    CONF_PORT: info[CONF_PORT],
                    CONF_VERSION: info[CONF_VERSION],
                    **device_data,
                },
                options={CONF_SENSORS: sensors},
            )

        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "model": self._zeroconf_info.get("model", "Unknown"),
                "host": self._zeroconf_info.get(CONF_HOST, ""),
            },
        )

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
            if unit := user_input.get(CONF_UNIT):
                sensor[CONF_UNIT] = unit
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
