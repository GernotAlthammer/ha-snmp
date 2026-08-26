"""Support for displaying collected data over SNMP."""

import logging

import pysnmp.hlapi.v3arch.asyncio as hlapi
from pysnmp.error import PySnmpError
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    Udp6TransportTarget,
    UdpTransportTarget,
    UsmUserData,
)
from pysnmp.hlapi.v3arch.asyncio.cmdgen import LCD
from pysnmp.smi import view

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.singleton import singleton

from .const import (
    CONF_AUTH_KEY,
    CONF_AUTH_PROTOCOL,
    CONF_COMMUNITY,
    CONF_PRIV_KEY,
    CONF_PRIV_PROTOCOL,
    CONF_VERSION,
    DEFAULT_AUTH_PROTOCOL,
    DEFAULT_COMMUNITY,
    DEFAULT_PORT,
    DEFAULT_PRIV_PROTOCOL,
    DEFAULT_TIMEOUT,
    DEFAULT_VERSION,
    MAP_AUTH_PROTOCOLS,
    MAP_PRIV_PROTOCOLS,
    SNMP_VERSIONS,
)

DATA_SNMP_ENGINE = "snmp_engine"

_LOGGER = logging.getLogger(__name__)

type CommandArgsType = tuple[
    SnmpEngine,
    UsmUserData | CommunityData,
    UdpTransportTarget | Udp6TransportTarget,
    ContextData,
]


type RequestArgsType = tuple[
    SnmpEngine,
    UsmUserData | CommunityData,
    UdpTransportTarget | Udp6TransportTarget,
    ContextData,
    ObjectType,
]


async def async_create_command_cmd_args(
    hass: HomeAssistant,
    auth_data: UsmUserData | CommunityData,
    target: UdpTransportTarget | Udp6TransportTarget,
) -> CommandArgsType:
    """Create command arguments.

    The ObjectType needs to be created dynamically by the caller.
    """
    engine = await async_get_snmp_engine(hass)
    return (engine, auth_data, target, ContextData())


async def async_create_request_cmd_args(
    hass: HomeAssistant,
    auth_data: UsmUserData | CommunityData,
    target: UdpTransportTarget | Udp6TransportTarget,
    object_id: str,
) -> RequestArgsType:
    """Create request arguments.

    The same ObjectType is used for all requests.
    """
    engine, auth_data, target, context_data = await async_create_command_cmd_args(
        hass, auth_data, target
    )
    object_type = ObjectType(ObjectIdentity(object_id))
    return (engine, auth_data, target, context_data, object_type)


@singleton(DATA_SNMP_ENGINE)
async def async_get_snmp_engine(hass: HomeAssistant) -> SnmpEngine:
    """Get the SNMP engine."""
    engine = await hass.async_add_executor_job(_get_snmp_engine)

    @callback
    def _async_shutdown_listener(ev: Event) -> None:
        _LOGGER.debug("Unconfiguring SNMP engine")
        LCD.unconfigure(engine, None)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_shutdown_listener)
    return engine


async def async_build_entry_request_args(
    hass: HomeAssistant,
    entry_data: dict,
    baseoid: str,
    community: str | None = None,
) -> RequestArgsType:
    """Build request arguments for a sensor that belongs to a config entry.

    `entry_data` holds the device-level connection settings collected in the
    config flow (host/port/version/[SNMPv3 credentials]). `community` is the
    per-sensor override collected in the options flow; it is only used for
    SNMP v1/v2c, since v3 always authenticates with the device's user
    credentials.
    """
    host = entry_data[CONF_HOST]
    port = int(entry_data.get(CONF_PORT, DEFAULT_PORT))
    version = entry_data.get(CONF_VERSION, DEFAULT_VERSION)

    try:
        # Try IPv4 first.
        target = await UdpTransportTarget.create((host, port), timeout=DEFAULT_TIMEOUT)
    except PySnmpError:
        # Then try IPv6.
        target = Udp6TransportTarget((host, port), timeout=DEFAULT_TIMEOUT)

    if version == "3":
        authproto = entry_data.get(CONF_AUTH_PROTOCOL, DEFAULT_AUTH_PROTOCOL)
        privproto = entry_data.get(CONF_PRIV_PROTOCOL, DEFAULT_PRIV_PROTOCOL)
        authkey = entry_data.get(CONF_AUTH_KEY)
        privkey = entry_data.get(CONF_PRIV_KEY)
        if not authkey:
            authproto = "none"
        if not privkey:
            privproto = "none"
        auth_data = UsmUserData(
            entry_data.get(CONF_USERNAME),
            authKey=authkey or None,
            privKey=privkey or None,
            authProtocol=getattr(hlapi, MAP_AUTH_PROTOCOLS[authproto]),
            privProtocol=getattr(hlapi, MAP_PRIV_PROTOCOLS[privproto]),
        )
    else:
        auth_data = CommunityData(
            community or entry_data.get(CONF_COMMUNITY, DEFAULT_COMMUNITY),
            mpModel=SNMP_VERSIONS[version],
        )

    return await async_create_request_cmd_args(hass, auth_data, target, baseoid)


def _get_snmp_engine() -> SnmpEngine:
    """Return a cached instance of SnmpEngine."""
    engine = SnmpEngine()
    # Actually load the MIBs from disk so we do not do it in the event loop
    mib_view_controller = view.MibViewController(
        engine.message_dispatcher.mib_instrum_controller.get_mib_builder()
    )
    engine.cache["mibViewController"] = mib_view_controller
    mib_view_controller.mibBuilder.load_modules()
    return engine
