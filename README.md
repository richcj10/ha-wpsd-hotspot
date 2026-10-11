# WPSD Hotspot Monitor

Reads a WPSD (Pi-Star successor) digital-voice hotspot's web dashboard every few seconds and
publishes it to Home Assistant as the **WPSD Hotspot** MQTT device:

- Radio status (Idle / Standby / RX / TX), On Air, frequency, modem
- Modes enabled, network links, DMR network + master (e.g. BrandMeister), timeslots, DMR ID, colour code
- Last heard (callsign + talkgroup) and last local RF transmission (with BER / RSSI)
- Live caller (name, location, talkgroup, duration) from WPSD's /live/ page
- BrandMeister talkgroups: static, dynamic (with expiry), auto-static, timed, blocked — from the public
  BrandMeister API using the hotspot's DMR ID (override with `bm_device_id`)
- Hotspot host: CPU load and temperature, RAM, disk, uptime

The `Status` sensor carries everything (including the last-heard and local-RF lists) as attributes.
MQTT credentials come from the Mosquitto add-on automatically.

## Install

1. Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories**, add
   `https://github.com/richcj10/ha-wpsd-hotspot`.
2. Install **WPSD Hotspot Monitor**, set its options on the **Configuration** tab, and start it.
3. Requires the Mosquitto broker add-on (MQTT credentials are taken from it automatically);
   to use another broker, set `mqtt_host`, `mqtt_user` and `mqtt_password`.

Part of [RackViewer](https://github.com/richcj10/RackViewer), a rack status display, which
reads this add-on's entities from Home Assistant.
