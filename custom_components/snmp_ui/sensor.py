"""Support for displaying collected data over SNMP."""

from datetime import timedelta
import logging
from struct import unpack
from typing import override

from pyasn1.codec.ber import decoder
from pysnmp.error import PySnmpError
import pysnmp.hlapi.v3arch.asyncio as hlapi
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    Udp6TransportTarget,
    UdpTransportTarget,
    UsmUserData,
    get_cmd,
)
from pysnmp.proto.rfc1902 import Opaque
from pysnmp.proto.rfc1905 import NoSuchObject
import voluptuous as vol

from homeassistant.components.sensor import (
    CONF_STATE_CLASS,
    PLATFORM_SCHEMA as SENSOR_PLATFORM_SCHEMA,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_HOST,
    CONF_ICON,
    CONF_NAME,
    CONF_PORT,
    CONF_UNIQUE_ID,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_USERNAME,
    CONF_VALUE_TEMPLATE,
    STATE_UNKNOWN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.template import Template
from homeassistant.helpers.trigger_template_entity import (
    CONF_AVAILABILITY,
    CONF_PICTURE,
    TEMPLATE_SENSOR_BASE_SCHEMA,
    ManualTriggerSensorEntity,
    ValueTemplate,
)
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from .const import (
    CONF_ACCEPT_ERRORS,
    CONF_AUTH_KEY,
    CONF_AUTH_PROTOCOL,
    CONF_BASEOID,
    CONF_COMMUNITY,
    CONF_DEFAULT_VALUE,
    CONF_MODEL,
    CONF_PRIV_KEY,
    CONF_PRIV_PROTOCOL,
    CONF_SENSOR_ID,
    CONF_SENSORS,
    CONF_SERIAL_NUMBER,
    CONF_UNIT,
    CONF_VALUE_MAP,
    CONF_VALUE_TYPE,
    CONF_VERSION,
    DEFAULT_AUTH_PROTOCOL,
    DEFAULT_COMMUNITY,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_PRIV_PROTOCOL,
    DEFAULT_TIMEOUT,
    DEFAULT_VERSION,
    DOMAIN,
    MAP_AUTH_PROTOCOLS,
    MAP_PRIV_PROTOCOLS,
    SNMP_VERSIONS,
)
from .util import async_build_entry_request_args, async_create_request_cmd_args

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)

TRIGGER_ENTITY_OPTIONS = (
    CONF_AVAILABILITY,
    CONF_DEVICE_CLASS,
    CONF_ICON,
    CONF_PICTURE,
    CONF_UNIQUE_ID,
    CONF_STATE_CLASS,
    CONF_UNIT_OF_MEASUREMENT,
)

PLATFORM_SCHEMA = SENSOR_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_BASEOID): cv.string,
        vol.Optional(CONF_ACCEPT_ERRORS, default=False): cv.boolean,
        vol.Optional(CONF_COMMUNITY, default=DEFAULT_COMMUNITY): cv.string,
        vol.Optional(CONF_DEFAULT_VALUE): cv.string,
        vol.Optional(CONF_HOST, default=DEFAULT_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_VALUE_TEMPLATE): vol.All(
            cv.template, ValueTemplate.from_template
        ),
        vol.Optional(CONF_VERSION, default=DEFAULT_VERSION): vol.In(SNMP_VERSIONS),
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_AUTH_KEY): cv.string,
        vol.Optional(CONF_AUTH_PROTOCOL, default=DEFAULT_AUTH_PROTOCOL): vol.In(
            MAP_AUTH_PROTOCOLS
        ),
        vol.Optional(CONF_PRIV_KEY): cv.string,
        vol.Optional(CONF_PRIV_PROTOCOL, default=DEFAULT_PRIV_PROTOCOL): vol.In(
            MAP_PRIV_PROTOCOLS
        ),
    }
).extend(TEMPLATE_SENSOR_BASE_SCHEMA.schema)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the SNMP sensor."""
    host = config.get(CONF_HOST)
    port = config.get(CONF_PORT)
    community = config.get(CONF_COMMUNITY)
    baseoid: str = config[CONF_BASEOID]
    version = config[CONF_VERSION]
    username = config.get(CONF_USERNAME)
    authkey = config.get(CONF_AUTH_KEY)
    authproto = config[CONF_AUTH_PROTOCOL]
    privkey = config.get(CONF_PRIV_KEY)
    privproto = config[CONF_PRIV_PROTOCOL]
    accept_errors = config.get(CONF_ACCEPT_ERRORS)
    default_value = config.get(CONF_DEFAULT_VALUE)

    try:
        # Try IPv4 first.
        target = await UdpTransportTarget.create((host, port), timeout=DEFAULT_TIMEOUT)
    except PySnmpError:
        # Then try IPv6.
        try:
            target = Udp6TransportTarget((host, port), timeout=DEFAULT_TIMEOUT)
        except PySnmpError as err:
            _LOGGER.error("Invalid SNMP host: %s", err)
            return

    if version == "3":
        if not authkey:
            authproto = "none"
        if not privkey:
            privproto = "none"
        auth_data = UsmUserData(
            username,
            authKey=authkey or None,
            privKey=privkey or None,
            authProtocol=getattr(hlapi, MAP_AUTH_PROTOCOLS[authproto]),
            privProtocol=getattr(hlapi, MAP_PRIV_PROTOCOLS[privproto]),
        )
    else:
        auth_data = CommunityData(community, mpModel=SNMP_VERSIONS[version])

    request_args = await async_create_request_cmd_args(hass, auth_data, target, baseoid)

    name = config.get(CONF_NAME, Template(DEFAULT_NAME, hass))
    trigger_entity_config = {CONF_NAME: name}
    for key in TRIGGER_ENTITY_OPTIONS:
        if key not in config:
            continue
        trigger_entity_config[key] = config[key]

    value_template: ValueTemplate | None = config.get(CONF_VALUE_TEMPLATE)

    data = SnmpData(request_args, baseoid, accept_errors, default_value)
    async_add_entities([SnmpSensor(hass, data, trigger_entity_config, value_template)])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SNMP sensors from a config entry (UI-configured device)."""
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )
    if model := entry.data.get(CONF_MODEL):
        device_info["model"] = model
    if serial_number := entry.data.get(CONF_SERIAL_NUMBER):
        device_info["serial_number"] = serial_number

    entities: list[SnmpSensor] = []
    for sensor_conf in entry.options.get(CONF_SENSORS, []):
        baseoid = sensor_conf[CONF_BASEOID]
        request_args = await async_build_entry_request_args(
            hass, entry.data, baseoid, sensor_conf.get(CONF_COMMUNITY)
        )
        data = SnmpData(
            request_args,
            baseoid,
            accept_errors=True,
            default_value=None,
            value_type=sensor_conf.get(CONF_VALUE_TYPE),
        )

        unique_id = f"{entry.entry_id}_{sensor_conf[CONF_SENSOR_ID]}"
        trigger_entity_config = {
            CONF_NAME: Template(sensor_conf[CONF_NAME], hass),
            CONF_UNIQUE_ID: unique_id,
        }
        if unit := sensor_conf.get(CONF_UNIT):
            trigger_entity_config[CONF_UNIT_OF_MEASUREMENT] = unit
        if state_class := sensor_conf.get(CONF_STATE_CLASS):
            trigger_entity_config[CONF_STATE_CLASS] = state_class
        entities.append(
            SnmpSensor(
                hass,
                data,
                trigger_entity_config,
                value_template=None,
                device_info=device_info,
                value_map=sensor_conf.get(CONF_VALUE_MAP),
            )
        )

    async_add_entities(entities)


class SnmpSensor(ManualTriggerSensorEntity):
    """Representation of a SNMP sensor."""

    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        data: SnmpData,
        config: ConfigType,
        value_template: ValueTemplate | None,
        device_info: DeviceInfo | None = None,
        value_map: dict[str, str] | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(hass, config)
        self.data = data
        self._state = None
        self._value_template = value_template
        self._value_map = value_map
        if device_info is not None:
            self._attr_device_info = device_info

    @override
    async def async_added_to_hass(self) -> None:
        """Handle adding to Home Assistant."""
        await super().async_added_to_hass()
        await self.async_update()

    async def async_update(self) -> None:
        """Get the latest data and updates the states."""
        await self.data.async_update()

        variables = self._template_variables_with_value(self.data.value)
        if (value := self.data.value) is None:
            value = STATE_UNKNOWN
        elif self._value_template is not None:
            value = self._value_template.async_render_as_value_template(
                self.entity_id, variables, STATE_UNKNOWN
            )
        elif self._value_map is not None:
            # Plain-text lookup for a raw SNMP status code, e.g. mapping a
            # printer's hrPrinterStatus "4" to "Printing". Values that
            # aren't in the map (unexpected/vendor-specific codes) are shown
            # as-is rather than hidden.
            value = self._value_map.get(str(value), value)

        self._set_native_value_with_possible_timestamp(value)
        self._process_manual_data(variables)


class SnmpData:
    """Get the latest data and update the states."""

    def __init__(self, request_args, baseoid, accept_errors, default_value, value_type=None) -> None:
        """Initialize the data object.

        `value_type` optionally forces the decoded value to a native Python
        type instead of leaving it as a string - currently only "int" is
        supported (used for numeric counters like a printer's page count).
        If the conversion fails, the raw string is kept instead.
        """
        self._request_args = request_args
        self._baseoid = baseoid
        self._accept_errors = accept_errors
        self._default_value = default_value
        self._value_type = value_type
        self.value = None

    async def async_update(self):
        """Get the latest data from the remote SNMP capable host."""

        get_result = await get_cmd(*self._request_args)
        errindication, errstatus, errindex, restable = get_result

        if errindication and not self._accept_errors:
            _LOGGER.error("SNMP error: %s", errindication)
        elif errstatus and not self._accept_errors:
            _LOGGER.error(
                "SNMP error: %s at %s",
                errstatus.prettyPrint(),
                restable[-1][int(errindex) - 1] if errindex else "?",
            )
        elif (errindication or errstatus) and self._accept_errors:
            self.value = self._default_value
        else:
            for resrow in restable:
                self.value = self._decode_value(resrow[-1])

    def _decode_value(self, value):
        """Decode the different results we could get into strings."""

        _LOGGER.debug(
            "SNMP OID %s received type=%s and data %s",
            self._baseoid,
            type(value),
            value,
        )
        if isinstance(value, NoSuchObject):
            _LOGGER.error(
                "SNMP error for OID %s: No Such Object currently exists at this OID",
                self._baseoid,
            )
            return self._default_value

        if isinstance(value, Opaque):
            # Float data type is not supported by the pyasn1 library,
            # so we need to decode this type ourselves based on:
            # https://tools.ietf.org/html/draft-perkins-opaque-01
            if bytes(value).startswith(b"\x9f\x78"):
                return str(unpack("!f", bytes(value)[3:])[0])
            # Otherwise Opaque types should be asn1 encoded
            try:
                decoded_value, _ = decoder.decode(bytes(value))
                return str(decoded_value)
            except Exception as decode_exception:  # noqa: BLE001
                _LOGGER.error(
                    "SNMP error in decoding opaque type: %s", decode_exception
                )
                return self._default_value

        if self._value_type == "error_bits":
            return _decode_printer_error_bits(value)

        return self._finalize(str(value))

    def _finalize(self, str_value):
        """Apply the optional `value_type` conversion to a decoded string."""
        if self._value_type == "int":
            try:
                return int(str_value)
            except (TypeError, ValueError):
                return str_value
        return str_value


# hrPrinterDetectedErrorState (RFC 1759 / RFC 3805) is a bitmask OCTET STRING:
# bit 0 (most significant bit of the first octet) is lowPaper, counting down
# to bit 7 = serviceRequested. Only the widely-implemented original 8 bits
# are decoded here - some printers set additional bits in a second octet
# (RFC 3805 extensions) that aren't covered by this list.
_PRINTER_ERROR_BIT_LABELS = (
    "Low Paper",
    "No Paper",
    "Low Toner",
    "No Toner",
    "Door Open",
    "Jammed",
    "Offline",
    "Service Requested",
)


def _decode_printer_error_bits(value) -> str:
    """Decode hrPrinterDetectedErrorState into a comma-separated text list."""
    try:
        raw = bytes(value)
    except (TypeError, ValueError):
        return str(value)
    if not raw:
        return "OK"
    first_byte = raw[0]
    active = [
        label
        for i, label in enumerate(_PRINTER_ERROR_BIT_LABELS)
        if first_byte & (0x80 >> i)
    ]
    return ", ".join(active) if active else "OK"
