"""SNMP constants."""

DOMAIN = "snmp"

# Keys used inside a config entry's `options[CONF_SENSORS]` list.
CONF_SENSORS = "sensors"
CONF_SENSOR_ID = "id"

# Discovery / network scan (printer auto-detection)
CONF_SUBNET = "subnet"
SCAN_MAX_HOSTS = 1024  # Refuse to scan networks larger than this
SCAN_CONCURRENCY = 32  # Max simultaneous SNMP probes during a scan
SCAN_TIMEOUT = 2.0  # Seconds per probe during a scan

# Printer MIB OIDs (RFC 3805) used to auto-detect printers and auto-create
# their sensors. Only the "Host Resources printer status" OID is required
# for a device to be *recognized* as a printer - all other OIDs are best
# effort and simply skipped if a given device doesn't support them.
OID_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"  # hrPrinterStatus - printer marker
OID_DEVICE_MODEL = "1.3.6.1.2.1.25.3.2.1.3.1"  # hrDeviceDescr
OID_SERIAL_NUMBER = "1.3.6.1.2.1.43.5.1.1.17.1"  # prtGeneralSerialNumber
OID_PRINTER_NAME = "1.3.6.1.2.1.43.5.1.1.16.1"  # prtGeneralPrinterName
OID_TOTAL_PAGES = "1.3.6.1.2.1.43.10.2.1.4.1.1"  # prtMarkerLifeCount
OID_MARKER_SUPPLIES_DESCRIPTION = "1.3.6.1.2.1.43.11.1.1.6.1"  # prtMarkerSuppliesDescription (walk)
OID_MARKER_SUPPLIES_LEVEL = "1.3.6.1.2.1.43.11.1.1.9.1"  # prtMarkerSuppliesLevel
OID_MARKER_SUPPLIES_MAX = "1.3.6.1.2.1.43.11.1.1.8.1"  # prtMarkerSuppliesMaxCapacity

CONF_ACCEPT_ERRORS = "accept_errors"
CONF_AUTH_KEY = "auth_key"
CONF_AUTH_PROTOCOL = "auth_protocol"
CONF_BASEOID = "baseoid"
CONF_COMMUNITY = "community"
CONF_DEFAULT_VALUE = "default_value"
CONF_PRIV_KEY = "priv_key"
CONF_PRIV_PROTOCOL = "priv_protocol"
CONF_VERSION = "version"
CONF_VARTYPE = "vartype"

DEFAULT_AUTH_PROTOCOL = "none"
DEFAULT_COMMUNITY = "public"
DEFAULT_HOST = "localhost"
DEFAULT_NAME = "SNMP"
DEFAULT_PORT = "161"
DEFAULT_PRIV_PROTOCOL = "none"
DEFAULT_TIMEOUT = 8
DEFAULT_VERSION = "1"
DEFAULT_VARTYPE = "none"

SNMP_VERSIONS = {"1": 0, "2c": 1, "3": None}

MAP_AUTH_PROTOCOLS = {
    "none": "usmNoAuthProtocol",
    "hmac-md5": "usmHMACMD5AuthProtocol",
    "hmac-sha": "usmHMACSHAAuthProtocol",
    "hmac128-sha224": "usmHMAC128SHA224AuthProtocol",
    "hmac192-sha256": "usmHMAC192SHA256AuthProtocol",
    "hmac256-sha384": "usmHMAC256SHA384AuthProtocol",
    "hmac384-sha512": "usmHMAC384SHA512AuthProtocol",
}

MAP_PRIV_PROTOCOLS = {
    "none": "usmNoPrivProtocol",
    "des": "usmDESPrivProtocol",
    "3des-ede": "usm3DESEDEPrivProtocol",
    "aes-cfb-128": "usmAesCfb128Protocol",
    "aes-cfb-192": "usmAesCfb192Protocol",
    "aes-cfb-256": "usmAesCfb256Protocol",
}
