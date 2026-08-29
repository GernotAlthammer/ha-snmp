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
    bulk_walk_cmd,
    get_cmd,
)
from pysnmp.hlapi.v3arch.asyncio.cmdgen import LCD
from pysnmp.proto.rfc1905 import NoSuchInstance, NoSuchObject
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

DATA_SNMP_ENGINE = "snmp_ui_engine"  # namespaced so it never shares hass.data with
# the built-in "snmp" integration's own SnmpEngine singleton, even if both are
# active in the same Home Assistant instance.

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


async def async_snmp_probe(
    hass: HomeAssistant,
    host: str,
    port: int,
    version: str,
    community: str,
    oid: str,
    timeout: float = 2.0,
) -> str | None:
    """Best-effort single SNMP v1/v2c GET, for use during network discovery.

    Returns the decoded value, or None if the host doesn't respond, doesn't
    support this OID, or takes too long. Never raises and never logs errors -
    a non-answer is the expected outcome for most addresses during a scan.
    """
    try:
        target = await UdpTransportTarget.create((host, port), timeout=timeout, retries=0)
    except PySnmpError:
        return None

    auth_data = CommunityData(community, mpModel=SNMP_VERSIONS[version])
    engine = await async_get_snmp_engine(hass)

    try:
        errindication, errstatus, errindex, restable = await get_cmd(
            engine,
            auth_data,
            target,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )
    except Exception:  # noqa: BLE001 - discovery must never crash on a bad host
        return None

    if errindication or errstatus:
        return None

    for resrow in restable:
        value = resrow[-1]
        if isinstance(value, (NoSuchObject, NoSuchInstance)):
            return None
        return str(value)
    return None


async def async_snmp_walk_table(
    hass: HomeAssistant,
    host: str,
    port: int,
    version: str,
    community: str,
    base_oid: str,
    timeout: float = 2.0,
    max_rows: int = 16,
) -> list[tuple[str, str]]:
    """Best-effort SNMP v1/v2c table walk, for use during network discovery.

    Returns a list of (index_suffix, decoded_value) pairs for every row under
    `base_oid`. Stops early once the walk leaves the table, `max_rows` is
    reached, or anything goes wrong - a partial/empty result is fine here,
    this is only used to auto-populate sensor suggestions.
    """
    try:
        target = await UdpTransportTarget.create((host, port), timeout=timeout, retries=0)
    except PySnmpError:
        return []

    auth_data = CommunityData(community, mpModel=SNMP_VERSIONS[version])
    engine = await async_get_snmp_engine(hass)
    results: list[tuple[str, str]] = []

    try:
        walker = bulk_walk_cmd(
            engine,
            auth_data,
            target,
            ContextData(),
            0,
            25,
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        )
        async for errindication, errstatus, errindex, restable in walker:
            if errindication or errstatus:
                break
            for oid_obj, value in restable:
                oid_str = str(oid_obj)
                if not oid_str.startswith(base_oid + "."):
                    return results
                if isinstance(value, (NoSuchObject, NoSuchInstance)):
                    continue
                results.append((oid_str[len(base_oid) + 1 :], str(value)))
                if len(results) >= max_rows:
                    return results
    except Exception:  # noqa: BLE001 - discovery must never crash on a bad host
        return results

    return results


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
