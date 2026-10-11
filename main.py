"""WPSD Hotspot Monitor.

Reads a WPSD (Pi-Star successor) digital-voice hotspot's dashboard and publishes its radio,
network and activity status to Home Assistant via MQTT discovery.
"""
import html
import json
import logging
import os
import re
import time
import urllib.request
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("wpsd_hotspot")

OPTIONS_PATH = "/data/options.json"
BASE = "wpsd_hotspot"
MODEL = "WPSD Hotspot"

# (component, object_id, name, state key, extra discovery fields); None in json_attributes_topic = full state
ENTITIES = [
    ("sensor", "status", "Status", "status", {"icon": "mdi:radio-tower", "json_attributes_topic": None}),
    ("binary_sensor", "online", "Online", "online",
     {"payload_on": "on", "payload_off": "off", "device_class": "connectivity"}),
    ("binary_sensor", "dmr_network", "DMR Network", "dmr_net",
     {"payload_on": "on", "payload_off": "off", "device_class": "connectivity"}),
    ("binary_sensor", "transmitting", "On Air", "on_air",
     {"payload_on": "on", "payload_off": "off", "icon": "mdi:access-point"}),
    ("sensor", "dmr_master", "DMR Master", "dmr_master", {"icon": "mdi:server-network"}),
    ("sensor", "frequency", "Frequency", "freq_mhz",
     {"unit_of_measurement": "MHz", "icon": "mdi:sine-wave"}),
    ("sensor", "modes", "Modes Enabled", "modes_text", {"icon": "mdi:format-list-checks"}),
    ("sensor", "last_heard", "Last Heard", "last_call", {"icon": "mdi:account-voice"}),
    ("sensor", "last_heard_target", "Last Heard Target", "last_target", {"icon": "mdi:target"}),
    ("sensor", "local_rf_last", "Last Local RF", "last_local_call", {"icon": "mdi:radio-handheld"}),
    ("sensor", "live_caller", "Live Caller", "live_call", {"icon": "mdi:account-voice"}),
    ("sensor", "dynamic_talkgroups", "Dynamic Talkgroups", "tg_dynamic_text", {"icon": "mdi:account-group"}),
    ("sensor", "static_talkgroups", "Static Talkgroups", "tg_static_text", {"icon": "mdi:pin"}),
    ("sensor", "bm_status", "BrandMeister Status", "bm_status", {"icon": "mdi:cloud-check-outline"}),
    ("sensor", "cpu_temp", "CPU Temperature", "cpu_temp_c",
     {"unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"}),
    ("sensor", "cpu_load", "CPU Load", "cpu_load",
     {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:cpu-64-bit"}),
    ("sensor", "ram_used", "RAM Used", "ram_pct",
     {"unit_of_measurement": "%", "state_class": "measurement", "entity_category": "diagnostic"}),
    ("sensor", "disk_used", "Disk Used", "disk_pct",
     {"unit_of_measurement": "%", "state_class": "measurement", "entity_category": "diagnostic"}),
    ("sensor", "uptime", "Uptime", "uptime", {"icon": "mdi:timer-outline", "entity_category": "diagnostic"}),
    ("sensor", "modem", "Modem", "modem", {"icon": "mdi:chip", "entity_category": "diagnostic"}),
]


def load_options() -> dict:
    with open(OPTIONS_PATH) as f:
        return json.load(f)


def supervisor_token() -> str:
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
        try:
            with open(f"/run/s6/container_environment/{name}") as f:
                tok = f.read().strip()
                if tok:
                    return tok
        except OSError:
            pass
    return ""


def mqtt_settings(opts: dict) -> Optional[dict]:
    if opts.get("mqtt_host"):
        return dict(host=opts["mqtt_host"], port=int(opts.get("mqtt_port") or 1883),
                    username=opts.get("mqtt_user", ""), password=opts.get("mqtt_password", ""))
    tok = supervisor_token()
    if not tok:
        log.error("No Supervisor token; set mqtt_host/mqtt_user/mqtt_password in the add-on options")
        return None
    req = urllib.request.Request("http://supervisor/services/mqtt", headers={"Authorization": "Bearer " + tok})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)["data"]
    except Exception as e:
        log.error("Supervisor MQTT lookup failed (%s); set mqtt_host/mqtt_user/mqtt_password instead", e)
        return None
    log.info("MQTT settings from Supervisor: %s:%s user=%s", d["host"], d["port"], d.get("username"))
    return dict(host=d["host"], port=int(d["port"]), username=d.get("username", ""), password=d.get("password", ""))


# ---------------------------------------------------------------- dashboard parsing
def fetch(host: str, path: str) -> str:
    with urllib.request.urlopen(f"http://{host}{path}", timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def pills(section_html: str) -> List[Dict[str, Any]]:
    """WPSD sidebar 'status-pill' entries: class (active/inactive/...), label, optional value."""
    out = []
    for m in re.finditer(r"""<div class=['"]status-pill ([\w-]+)['"][^>]*>(.*?)</div>\s*</div>|"""
                         r"""<div class=['"]status-pill ([\w-]+)['"][^>]*>(.*?)</div>""", section_html, re.S):
        cls = m.group(1) or m.group(3)
        body = m.group(2) or m.group(4)
        spans = [text(x) for x in re.findall(r"<span[^>]*>(.*?)</span>", body, re.S)]
        if spans:
            out.append(dict(state=cls, label=spans[0], value=spans[1] if len(spans) > 1 else None))
    return out


def section(page: str, title: str) -> str:
    m = re.search(r'sidebar-section-title[^>]*>\s*' + re.escape(title) + r'\s*</div>(.*?)(?=sidebar-section-title|\Z)',
                  page, re.S)
    return m.group(1) if m else ""


def div_rows(page: str) -> List[List[str]]:
    """divTable rows -> list of cell texts."""
    rows = []
    for row in re.split(r'<div class="divTableRow', page)[1:]:
        row = row.split(">", 1)[1] if ">" in row else row          # drop the rest of the row's own tag
        cells = [text(c) for c in re.split(r"<div[^>]*>", row)]
        rows.append([c for c in cells if c])
    return rows


def html_table(page: str, limit: int = 10) -> List[Dict[str, str]]:
    heads = [text(re.sub(r"<span>.*?</span>", "", h, flags=re.S))
             for h in re.findall(r"<th[^>]*>(.*?)</th>", page, re.S)]
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        tds = [text(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if tds and heads:
            rows.append({re.sub(r"\s*\(.*?\)", "", h).strip().lower() or f"c{i}": v
                         for i, (h, v) in enumerate(zip(heads, tds))})
        if len(rows) >= limit:
            break
    return rows


def num(pattern: str, s: str) -> Optional[float]:
    m = re.search(pattern, s)
    try:
        return float(m.group(1).replace(",", "")) if m else None
    except ValueError:
        return None


BM_API = "https://api.brandmeister.network/v2"
_tg_names: Dict[int, str] = {}


def bm_get(path: str) -> Any:
    # note: BrandMeister's front end rejects custom User-Agents (403); the default urllib one is accepted
    req = urllib.request.Request(BM_API + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def tg_name(tg: int) -> str:
    if tg not in _tg_names:
        try:
            _tg_names[tg] = (bm_get(f"/talkgroup/{tg}") or {}).get("Name") or ""
        except Exception:
            return ""
    return _tg_names[tg]


def brandmeister(device_id: str) -> Dict[str, Any]:
    """Talkgroup subscriptions + device status from the public BrandMeister API (no key needed)."""
    out: Dict[str, Any] = {}
    dev = bm_get(f"/device/{device_id}")
    prof = bm_get(f"/device/{device_id}/profile")
    out["bm_status"] = dev.get("statusText") or str(dev.get("status"))
    out["bm_last_seen"] = dev.get("last_seen")
    out["bm_master"] = dev.get("lastKnownMaster")
    now = time.time()

    def tgs(items, kind):
        res = []
        for it in items or []:
            tg = int(it.get("talkgroup"))
            d = dict(tg=tg, name=tg_name(tg), slot=it.get("slot"), kind=kind)
            if it.get("timeout"):
                d["expires_in"] = max(0, int(it["timeout"] - now))
            res.append(d)
        return res
    out["tg_static"] = tgs(prof.get("staticSubscriptions"), "static")
    out["tg_dynamic"] = tgs(prof.get("dynamicSubscriptions"), "dynamic")
    out["tg_timed"] = tgs(prof.get("timedSubscriptions"), "timed")
    out["tg_blocked"] = [int(x.get("talkgroup")) for x in prof.get("blockedGroups") or []]
    auto = prof.get("autoStatic")
    out["tg_autostatic"] = int(auto["talkgroup"]) if isinstance(auto, dict) and auto.get("talkgroup") else None
    out["tg_static_text"] = ", ".join(str(t["tg"]) for t in out["tg_static"]) or "none"
    out["tg_dynamic_text"] = ", ".join(str(t["tg"]) for t in out["tg_dynamic"]) or "none"
    return out


def live_caller(host: str) -> Dict[str, Any]:
    """WPSD /live/ page backend: current / last caller with name, location, target, duration."""
    page = fetch(host, "/mmdvmhost/live_caller_backend.php")
    d: Dict[str, Any] = {}
    m = re.search(r"class='oc_call'>(.*?)</span>", page, re.S)
    d["call"] = text(m.group(1)) if m else None
    m = re.search(r"class='oc_name'>(.*?)</span>(.*?)</span>", page, re.S)
    if m:
        d["name"] = text(m.group(1))
        d["location"] = ", ".join(x for x in (text(t) for t in re.split(r"<br\s*/?>", m.group(2))) if x)
    m = re.search(r"alt='([^']*)'", page)
    d["country"] = m.group(1) if m else None
    body = text(page)
    for label, key in (("Source", "source"), ("Mode", "mode"), ("Target", "target"),
                       ("TX Duration", "duration"), ("Packet Loss", "loss"), ("BER", "ber")):
        m = re.search(label + r":\s*(.*?)\s*(?=Source:|Mode:|Target:|TX Duration:|Packet Loss:|BER:|Last Caller ID:|Main Dashboard|$)", body)
        if m and m.group(1):
            d[key] = m.group(1).strip()
    return d


def poll(host: str) -> Dict[str, Any]:
    st: Dict[str, Any] = {"host": host}
    rep = fetch(host, "/mmdvmhost/repeaterinfo.php")
    modes = pills(section(rep, "Modes Enabled"))
    nets = pills(section(rep, "Network Status"))
    dmr = pills(section(rep, "DMR Status"))
    master = pills(section(rep, "DMR Master"))
    st["modes"] = [p["label"] for p in modes if p["state"] == "active"]
    st["modes_text"] = ", ".join(st["modes"]) or "none"
    st["networks"] = {p["label"]: p["state"] for p in nets}
    st["dmr_net"] = "on" if st["networks"].get("DMR Net") == "active" else "off"
    st["dmr_master"] = master[0]["label"] if master else None
    st["dmr_master_state"] = master[0]["state"] if master else None
    for p in dmr:
        if p["label"].startswith("TS"):
            st.setdefault("timeslots", {})[p["label"]] = p["value"] or p["state"]
        elif p["label"] in ("ID", "CC"):
            st["dmr_" + p["label"].lower()] = p["value"]

    radio = div_rows(fetch(host, "/mmdvmhost/radioinfo.php"))
    if len(radio) >= 2:
        info = dict(zip(radio[0], radio[1]))
        st["radio_status"] = info.get("Radio Status")
        st["freq"] = info.get("TX/RX Freq.")
        st["freq_mhz"] = num(r"([\d.]+)\s*MHz", info.get("TX/RX Freq.", ""))
        st["radio_mode"] = info.get("Radio Mode")
        st["modem"] = info.get("Modem Type")
        st["tcxo"] = info.get("TCXO Freq.")
    raw = st.get("radio_status") or ""
    # WPSD shows IDLE, "Standby: DMR" (hang time), or RX / TX with the mode while traffic is passing
    st["on_air"] = "on" if re.search(r"\b(RX|TX)\b", raw.upper()) else "off"
    st["status"] = {"IDLE": "Idle", "": "Unknown"}.get(raw.strip().upper(), raw.strip())

    hw = text(fetch(host, "/includes/hw_info.php"))
    st["cpu_load"] = num(r"CPU Load\s*([\d.]+)\s*%", hw)
    st["cpu_temp_c"] = num(r"/\s*([\d.]+)\s*°C", hw)
    st["ram_pct"] = num(r"RAM Usage.*?Used:\s*([\d.]+)%", hw)
    st["disk_pct"] = num(r"Disk Usage.*?Used:\s*([\d.]+)%", hw)
    m = re.search(r"Uptime:?\s*([\w ,]+?)(?:\s+CPU|\s+RAM|$)", hw)
    st["uptime"] = m.group(1).strip(" ,") if m else None
    m = re.search(r"Platform:\s*(.+?)\s+(?:aarch64|armv7l|x86_64)", hw)
    st["platform"] = m.group(1) if m else None

    lh = html_table(fetch(host, "/mmdvmhost/last_heard_table.php"))
    st["last_heard"] = lh
    st["last_call"] = lh[0].get("callsign") if lh else "none"
    st["last_target"] = lh[0].get("target") if lh else None
    rf = html_table(fetch(host, "/mmdvmhost/local_tx_table.php"))
    st["local_rf"] = rf
    st["last_local_call"] = rf[0].get("callsign") if rf else "none"
    try:
        lc = live_caller(host)
        st["live"] = lc
        st["live_call"] = lc.get("call") or "none"
    except Exception as e:
        log.debug("live caller: %s", e)
    st["online"] = "on"
    return st


def publish_discovery(client: mqtt.Client, host: str, st: Optional[dict], callsign: str) -> None:
    uid = "wpsd_" + host.replace(".", "_")
    dev = {"identifiers": [uid], "name": f"WPSD Hotspot {callsign}".strip(), "manufacturer": "WPSD",
           "model": MODEL, "configuration_url": f"http://{host}/"}
    if st and st.get("modem"):
        dev["hw_version"] = st["modem"]
    for comp, oid, name, key, extra in ENTITIES:
        cfg = {"name": name, "unique_id": f"{uid}_{oid}", "device": dev,
               "state_topic": f"{BASE}/state", "value_template": "{{ value_json.%s }}" % key,
               "availability_topic": f"{BASE}/status"}
        for k, v in extra.items():
            cfg[k] = v if v is not None else f"{BASE}/state"
        client.publish(f"homeassistant/{comp}/{uid}/{oid}/config", json.dumps(cfg), retain=True)
    log.info("Published discovery for %d entities (%s)", len(ENTITIES), uid)


def main() -> None:
    opts = load_options()
    logging.getLogger().setLevel(getattr(logging, str(opts.get("log_level", "INFO")).upper(), logging.INFO))
    host = opts["host"]
    interval = int(opts.get("poll_interval", 5))

    ms = mqtt_settings(opts)
    while ms is None:
        time.sleep(60)
        ms = mqtt_settings(opts)
    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2, client_id="wpsd_hotspot_monitor")
    if ms["username"]:
        client.username_pw_set(ms["username"], ms["password"])
    client.will_set(f"{BASE}/status", "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected: %s", reason_code)
        c.publish(f"{BASE}/status", "online", retain=True)

    def on_disconnect(c, userdata, flags, reason_code, properties=None):
        log.warning("MQTT disconnected: %s", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect(ms["host"], ms["port"], 60)
    client.loop_start()

    published = False
    fails = 0
    bm: Dict[str, Any] = {}
    bm_at = 0.0
    while True:
        try:
            st = poll(host)
            fails = 0
            dev_id = opts.get("bm_device_id") or st.get("dmr_id")
            if dev_id and time.time() - bm_at > 30:
                bm_at = time.time()
                try:
                    bm = brandmeister(str(dev_id))
                except Exception as e:
                    log.warning("BrandMeister API: %s", e)
            if bm:
                # countdowns stay accurate between API polls
                age = int(time.time() - bm_at)
                bm_now = dict(bm, tg_dynamic=[dict(t, expires_in=max(0, t.get("expires_in", 0) - age))
                                              if "expires_in" in t else t for t in bm.get("tg_dynamic", [])])
                st.update(bm_now)
        except Exception as e:
            fails += 1
            log.warning("Dashboard poll failed (%d): %s", fails, e)
            st = {"host": host, "online": "off" if fails >= 3 else "on", "status": "Offline" if fails >= 3 else None}
            if fails < 3:
                time.sleep(interval)
                continue
        if not published:
            publish_discovery(client, host, st, opts.get("callsign", ""))
            published = True
        client.publish(f"{BASE}/state", json.dumps(st), retain=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
