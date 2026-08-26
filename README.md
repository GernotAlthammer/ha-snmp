# ha-snmp

Home Assistant SNMP integration with UI setup, distributed as a [HACS](https://hacs.xyz/) custom integration.

It is based on Home Assistant's built-in `snmp` sensor platform, with a config flow added so devices and sensors can be set up from **Settings → Devices & Services** instead of `configuration.yaml`.

## Installation (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/GernotAlthammer/ha-snmp` as type *Integration*.
2. Install "SNMP Integration UI" and restart Home Assistant.

## Adding a device

1. **Settings → Devices & Services → Add Integration → SNMP**.
2. Enter the device's IP address (or hostname), port (default `161`) and SNMP version (`1`, `2c` or `3`).
   * For SNMP v3 you'll be asked for the username, auth/priv protocols and keys on the next screen.
3. Submitting creates a device entry with no sensors yet.

## Adding sensors to a device

1. On the SNMP integration/device, click **Configure**.
2. Choose **Add a sensor** and fill in:
   * **Name** – the sensor's display name.
   * **Community** – the SNMP community string (used for v1/v2c; ignored for v3, which uses the device's user credentials).
   * **Base OID** – the OID to poll, e.g. `1.3.6.1.2.1.43.11.1.1.9.1.1`.
3. Repeat for every OID you want to expose. Each sensor is polled every 10 seconds.
4. Use **Remove a sensor** in the same **Configure** menu to delete sensors again.

All sensors added for a device are grouped under that device in Home Assistant.

## YAML configuration

The original YAML-based `snmp:` sensor platform still works unchanged for existing setups; the config flow is purely additive.
