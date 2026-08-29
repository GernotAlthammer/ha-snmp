# ha-snmp

Home Assistant SNMP integration with UI setup, distributed as a [HACS](https://hacs.xyz/) custom integration.

It is based on Home Assistant's built-in `snmp` sensor platform, with a config flow added so devices and sensors can be set up from **Settings → Devices & Services** instead of `configuration.yaml`.

## Installation (HACS)

1. HACS → Integrations → ⋮ → Custom repositories → add `https://github.com/GernotAlthammer/ha-snmp` as type *Integration*.
2. Install "SNMP Integration UI" and restart Home Assistant.

## Adding a device

**Settings → Devices & Services → Add Integration → SNMP** offers three ways in:

### Automatically via mDNS (zeroconf)

Many network printers announce themselves via mDNS/Bonjour. When Home Assistant sees such an announcement, it silently probes the device's SNMP printer-status OID (with the default `public` community, v2c then v1) - if that succeeds, a **discovery card** shows up under Settings → Devices & Services with the printer's model, ready to confirm. Confirming builds the full sensor set automatically, exactly like the network scan below. If the printer isn't reachable via SNMP (e.g. AirPrint/IPP-only, or a non-default community), no card is shown - use one of the other two methods instead.

### Manually

1. Choose **Enter a device manually**.
2. Enter the device's IP address (or hostname), port (default `161`) and SNMP version (`1`, `2c` or `3`).
   * For SNMP v3 you'll be asked for the username, auth/priv protocols and keys on the next screen.
3. Submitting creates a device entry with no sensors yet - add them as described below.

### Automatic printer discovery (subnet scan)

1. Choose **Scan the network for printers**.
2. Enter a subnet (e.g. `192.168.1.0/24`), the SNMP version (`1`/`2c`) and community used by your printers.
3. Home Assistant probes every address in that subnet for SNMP's printer-status OID. Devices that answer are listed with their model.
4. Pick which of the found printers to add (all are pre-selected). For each one, a device is created **and all of its sensors are generated automatically** - no OIDs to type in:
   * Model, Serial Number, Status, Total Pages (whichever the printer supports)
   * One **Level** and **Max** sensor per toner/ink marker, discovered by walking the printer's marker-supplies table (works for single-color and multi-color/CMYK printers alike, however many markers a given model has)

## Adding sensors manually

1. On the SNMP integration/device, click **Configure**.
2. Choose **Add a sensor** and fill in:
   * **Name** – the sensor's display name.
   * **Community** – the SNMP community string (used for v1/v2c; ignored for v3, which uses the device's user credentials).
   * **Base OID** – the OID to poll, e.g. `1.3.6.1.2.1.43.11.1.1.9.1.1`.
3. Repeat for every OID you want to expose. Each sensor is polled every 10 seconds.
4. Use **Remove a sensor** in the same **Configure** menu to delete sensors again.

This works the same way for sensors that were added automatically via network discovery - you can always add more or remove ones you don't need.

All sensors added for a device are grouped under that device in Home Assistant.

## YAML configuration

The original YAML-based `snmp:` sensor platform still works unchanged for existing setups; the config flow is purely additive.
