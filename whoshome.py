"""Who's Home: watches the BT Smart Hub 2 and keeps track of what's on the network.

Written to run on old Pis: Python 3.5 or newer, and nothing to install.
Everything it uses is built into Python.
"""

import html
import json
import os
import logging
import re
import subprocess
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

try:
    import config
except ImportError:
    raise SystemExit("No config.py found. Copy config.example.py to config.py and fill it in.")
import discover

# Settings added later get sensible defaults, so an older config.py still works
for _name, _value in (("QUIET_AFTER_DAYS", 60), ("FORGET_AFTER_DAYS", 180), ("DISCOVER_MINUTES", 60)):
    if not hasattr(config, _name):
        setattr(config, _name, _value)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whoshome")

DEVICE_TYPES = ["Phone", "Laptop", "Tablet", "TV", "Games console", "Speaker",
                "Doorbell / camera", "Smart plug / light", "Printer", "Computer", "Other"]

DB_LOCK = threading.Lock()
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------- database ----------

def db():
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def setup_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS devices (
            mac TEXT PRIMARY KEY,
            hub_name TEXT,
            ip TEXT,
            friendly_name TEXT,
            owner TEXT,
            device_type TEXT,
            counts_for_presence INTEGER DEFAULT 0,
            known INTEGER DEFAULT 0,
            connection TEXT,
            hub_hint TEXT,
            first_seen INTEGER,
            last_seen INTEGER,
            online INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER,
            kind TEXT,
            mac TEXT,
            detail TEXT
        );
        CREATE TABLE IF NOT EXISTS people (
            name TEXT PRIMARY KEY,
            is_home INTEGER DEFAULT 0,
            since INTEGER
        );
        CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
        """)
        cols = [r[1] for r in c.execute("PRAGMA table_info(devices)")]
        if "hub_last_active" not in cols:
            c.execute("ALTER TABLE devices ADD COLUMN hub_last_active INTEGER")
        if "ignored" not in cols:
            c.execute("ALTER TABLE devices ADD COLUMN ignored INTEGER DEFAULT 0")
        for col in ("found_name", "found_model", "found_via", "found_services"):
            if col not in cols:
                c.execute("ALTER TABLE devices ADD COLUMN {} TEXT".format(col))


# When a device was last about, using our own record or the hub's memory, whichever is later
SEEN_AT = "MAX(COALESCE(last_seen,0), COALESCE(hub_last_active,0))"


def quiet_cutoff():
    return now() - config.QUIET_AFTER_DAYS * 86400


def not_quiet():
    return "(COALESCE(ignored,0)=0 AND (online=1 OR {} >= {}))".format(SEEN_AT, quiet_cutoff())


def is_quiet():
    return "(COALESCE(ignored,0)=0 AND online=0 AND {} < {})".format(SEEN_AT, quiet_cutoff())


IGNORED = "COALESCE(ignored,0)=1"


def now():
    return int(time.time())


def log_event(c, kind, mac, detail):
    c.execute("INSERT INTO events(ts, kind, mac, detail) VALUES (?,?,?,?)",
              (now(), kind, mac, detail))


# ---------- notifications ----------

def notify(title, message, tags="house"):
    if not config.NTFY_TOPIC:
        return
    url = "{}/{}".format(config.NTFY_SERVER.rstrip("/"), config.NTFY_TOPIC)
    req = urllib.request.Request(url, data=message.encode("utf-8"),
                                 headers={"Title": title, "Tags": tags})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        log.warning("Notification failed: %s", e)


# ---------- reading the hub ----------

def fetch(path):
    url = "http://{}/{}".format(config.HUB_IP, path)
    req = urllib.request.Request(url, headers={
        "Referer": "http://{}/basic_-_my_devices.htm".format(config.HUB_IP)})
    return urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")


def js_array(body, marker):
    """The hub gives us a JavaScript list, not proper JSON. Tidy it up and read it."""
    start = body.find(marker)
    if start < 0:
        return []
    start += len(marker)
    end = body.find("];", start)
    text = body[start:end + 1]

    # Pull out the quoted values first so nothing inside them gets mangled
    strings = []

    def keep(m):
        strings.append(urllib.parse.unquote(m.group(1)))
        return "\x00{}\x00".format(len(strings) - 1)

    text = re.sub(r"'((?:[^'\\]|\\.)*)'", keep, text)
    text = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: json.dumps(strings[int(m.group(1))]), text)
    items = json.loads(text)
    return [i for i in items if isinstance(i, dict)]


def read_hub():
    body = fetch("cgi/cgi_basicMyDevice.js")
    devices = js_array(body, "known_device_list=")

    # How each device connects (2.4GHz, 5GHz, cable). Nice to have, so don't fail on it.
    links = {}
    try:
        for s in js_array(fetch("cgi/cgi_owl.js"), "owl_station="):
            links[(s.get("station_mac") or "").lower()] = str(s.get("connect_type") or "")
    except Exception as e:
        log.debug("No connection details: %s", e)

    out = []
    for d in devices:
        mac = (d.get("mac") or "").lower()
        if not mac:
            continue
        d["mac"] = mac
        d["connection"] = nice_connection(links.get(mac)) or nice_connection(d.get("port"))
        out.append(d)
    return out


def nice_connection(v):
    """Turn the hub's connection codes into plain words."""
    raw = str(v or "").strip()
    t = raw.lower().replace(" ", "")
    if not t or t in ("unknown", "none", "0", "-"):
        return ""
    if any(k in t for k in ("eth", "lan", "wired", "cable")):
        return "Wired"
    if any(k in t for k in ("5g", "5.0", "wl1", "wifi5", "radio1")):
        return "Wi-Fi 5GHz"
    if any(k in t for k in ("2.4", "2g", "24g", "wl0", "radio0")):
        return "Wi-Fi 2.4GHz"
    if any(k in t for k in ("wifi", "wireless", "wlan", "wl")):
        return "Wi-Fi"
    if "usb" in t:
        return "USB"
    return raw


def is_random_mac(mac):
    """Phones invent a 'private' address per network. The second character tells us."""
    return len(mac) > 1 and mac[1].lower() in "26ae"


MAKERS = None


def maker(mac):
    """Look up who made a device from the first half of its address, using oui.tsv."""
    global MAKERS
    if MAKERS is None:
        MAKERS = {}
        try:
            with open("oui.tsv", encoding="utf-8") as f:
                for line in f:
                    code, _, name = line.rstrip("\n").partition("\t")
                    MAKERS[code] = " ".join(name.split())
            log.info("Loaded %s device makers", len(MAKERS))
        except IOError:
            log.warning("oui.tsv not found, so no maker lookups")
    if is_random_mac(mac):
        return ""
    return MAKERS.get(mac.replace(":", "").replace("-", "").upper()[:6], "")


def hub_hint(d):
    bits = []
    made_by = maker(d["mac"])
    if made_by:
        bits.append("Made by " + made_by)
    for k in ("device_oui", "os", "device", "device_class"):
        v = str(d.get(k) or "").strip()
        if v and v.lower() != "unknown" and v not in bits and v.lower() not in made_by.lower():
            bits.append(v)
    if is_random_mac(d["mac"]):
        bits.append("private address, so the maker is hidden (almost always a phone, tablet or laptop)")
    return ", ".join(bits)


def hub_time(v):
    """The hub remembers when it last saw each device. Turn that into a timestamp if we can."""
    v = str(v or "").strip()
    if not v or v in ("0", "-"):
        return None
    if v.isdigit():
        n = int(v)
        if n > 1e12:
            n //= 1000
        return n if 1e9 < n <= now() + 86400 else None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%a %b %d %H:%M:%S %Y", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(v[:len(time.strftime(fmt))].strip(), fmt)))
        except ValueError:
            pass
    try:
        return int(time.mktime(time.strptime(v.split(".")[0].replace("Z", ""), "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def is_active(d):
    return str(d.get("activity")).strip().lower() in ("1", "true", "yes")


def display_name(row):
    return (row["friendly_name"] or row["found_name"] or hub_label(row["hub_name"])
            or guess(row) or row["hub_name"] or row["mac"])


# Hub names that tell you what something is, even if they don't look like it
HUB_NAME_RULES = [
    (r"(?i)^pantum", "Pantum printer"),
    (r"(?i)^int6400", "Powerline adapter"),
    (r"(?i)^RE\d{3}\b", "TP-Link Wi-Fi extender"),
    (r"(?i)^bt-?wifi-?disc", "BT Wi-Fi Disc"),
    (r"(?i)^ringdoorbell", "Ring Doorbell"),
    (r"(?i)^ringcam|^ringstickup", "Ring camera"),
    (r"(?i)^ee-?tv", "EE TV box"),
    (r"(?i)^xbox", "Xbox"),
    (r"(?i)^chromecast", "Chromecast"),
    (r"(?i)^(brn|brw)[0-9a-f]{12}$", "Brother printer"),
    (r"(?i)^(hp|npi)[0-9a-f]{6}", "HP printer"),
    (r"(?i)^epson", "Epson printer"),
]


def hub_label(name):
    """Turn the hub's name into something readable, or nothing if it's meaningless."""
    name = (name or "").strip()
    if not name or discover.is_generic(name):
        return ""
    for pattern, label in HUB_NAME_RULES:
        if re.search(pattern, name):
            return label
    # "Sams-Galaxy-S23" -> "Sams Galaxy S23", "Jo-s-iPad" -> "Jo's iPad", "Pixel-9-Pro-XL" -> "Pixel 9 Pro XL"
    name = re.sub(r"-s-", "'s ", name)
    name = re.sub(r"[-_]+", " ", name).strip()
    return name


# Educated guesses from who made it, what it offers on the network and what the hub says
GUESSES = [
    # (maker contains, needs one of these services/ports or None, guess)
    ("Apple", ("port:62078",), "iPhone or iPad"),
    ("Apple", ("port:22", "port:445", "port:548"), "Mac"),
    ("Apple", ("airplay", "raop", "companion-link"), "Apple TV, HomePod or Mac"),
    ("", ("port:62078",), "iPhone or iPad"),
    ("Samsung", ("smartthings", "smartthings-hedge", "airplay", "port:8001", "port:8002"), "Samsung TV"),
    ("Amazon", ("port:8009", "port:8008"), "Amazon Fire TV"),
    ("Amazon", ("port:4070", "port:55443", "spotify-connect", "amzn-wplay"), "Amazon Echo (Alexa)"),
    ("Amazon", None, "Amazon device (Echo, Fire TV or Kindle)"),
    ("Ring", None, "Ring doorbell or camera"),
    ("Google", ("googlecast", "port:8008", "port:8009"), "Chromecast or Google speaker"),
    ("Google", None, "Google device (Nest, Chromecast or Pixel)"),
    ("Sony Interactive", None, "PlayStation"),
    ("Nintendo", None, "Nintendo Switch"),
    ("Microsoft", None, "Xbox or Microsoft device"),
    ("Guangzhou Shiyuan", None, "Smart TV (CVTE makes the insides for many TV brands)"),
    ("GONGJIN", None, "Powerline adapter or BT kit"),
    ("Sagemcom", ("yv-bridge",), "YouView / EE TV box"),
    ("Sagemcom", None, "TV box or router"),
    ("Sonos", None, "Sonos speaker"),
    ("Roku", None, "Roku streaming stick"),
    ("Philips Lighting", None, "Philips Hue bridge"),
    ("Signify", None, "Philips Hue bridge"),
    ("Raspberry Pi", None, "Raspberry Pi"),
    ("Espressif", ("port:6668",), "Tuya smart device (plug, switch or thermostat)"),
    ("Espressif", None, "Smart plug, switch or gadget"),
    ("Tuya", None, "Smart plug, bulb or gadget"),
    ("Shelly", None, "Shelly smart switch"),
    ("Brother", None, "Brother printer"),
    ("Seiko Epson", None, "Epson printer"),
    ("CANON", None, "Canon printer"),
    ("SKY UK", None, "Sky box"),
    ("TP-LINK", None, "TP-Link network kit"),
    ("Intel", ("port:3389", "port:445"), "Windows laptop or PC"),
    ("Intel", None, "Laptop or PC"),
    ("", ("port:9295",), "PlayStation"),
    ("", ("port:8060",), "Roku"),
    ("", ("port:1400",), "Sonos speaker"),
    ("", ("port:3389", "port:445"), "Windows PC"),
    ("", ("port:548",), "Mac"),
    ("", ("port:631", "port:9100", "ipp", "ipps", "printer", "pdl-datastream"), "Printer"),
    ("", ("port:6668",), "Tuya smart device"),
    ("", ("googlecast",), "Chromecast or Google speaker"),
    ("", ("spotify-connect",), "Speaker (Spotify Connect)"),
]

HINT_RULES = [
    ("game console", "Games console"),
    ("powerline", "Powerline adapter"),
    ("wifiex", "Wi-Fi extender"),
    ("stb", "TV box"),
]


def guess(row):
    made_by = maker(row["mac"]).lower()
    services = set((row["found_services"] or "").split(","))
    hint = (row["hub_hint"] or "").lower()
    for who, needs, label in GUESSES:
        if who and who.lower() not in made_by:
            continue
        if needs and not services.intersection(needs):
            continue
        if not who and not needs:
            continue
        return label
    for word, label in HINT_RULES:
        if word in hint:
            return label
    if is_random_mac(row["mac"]):
        if "android" in hint or "smartphone" in hint:
            return "Android phone or tablet"
        if "ios" in hint or "iphone" in hint or "ipad" in hint:
            return "iPhone or iPad"
        return "Phone, tablet or laptop"
    if (row["hub_name"] or "").lower().startswith("wlan"):
        return "Smart home gadget (sets no name of its own)"
    return ""


# ---------- asking devices their own names ----------

LOOK_NOW = threading.Event()


def find_names():
    with db() as c:
        rows = c.execute("SELECT mac, ip FROM devices WHERE online=1 AND ip IS NOT NULL AND ip<>''").fetchall()
    by_ip = {r["ip"]: r["mac"] for r in rows}
    if not by_ip:
        return
    found = discover.discover(sorted(by_ip))
    with DB_LOCK, db() as c:
        for ip, info in found.items():
            mac = by_ip.get(ip)
            if mac:
                c.execute("""UPDATE devices SET found_name=?, found_model=?, found_via=?, found_services=?
                             WHERE mac=?""",
                          (info["name"] or None, info["model"] or None, info["via"],
                           ",".join(info.get("services", [])) or None, mac))
    log.info("Asked %s devices their names, %s answered", len(by_ip), len(found))


def name_finder():
    time.sleep(5)
    while True:
        try:
            find_names()
        except Exception:
            log.exception("Looking for names failed, will try again")
        LOOK_NOW.wait(config.DISCOVER_MINUTES * 60)
        LOOK_NOW.clear()


def poll_once():
    devices = read_hub()
    t = now()
    active_macs = set()

    with DB_LOCK, db() as c:
        # On the very first run everything is "new", so just learn quietly
        first_run = c.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0

        for d in devices:
            mac = d["mac"]
            active = is_active(d)
            name = d.get("hostname") or d.get("name") or ""
            hint = hub_hint(d)
            hub_seen = hub_time(d.get("time_last_active"))
            row = c.execute("SELECT * FROM devices WHERE mac=?", (mac,)).fetchone()

            if row is None:
                # The hub remembers devices for years. Only take on a new one if it's actually
                # connected, or on the very first look if the hub saw it recently.
                recent = hub_seen is not None and hub_seen >= quiet_cutoff()
                if not active and not (first_run and recent):
                    continue
                c.execute("""INSERT INTO devices(mac, hub_name, ip, connection, hub_hint,
                             first_seen, last_seen, online, hub_last_active) VALUES (?,?,?,?,?,?,?,?,?)""",
                          (mac, name, d.get("ip"), d["connection"], hint,
                           t, t if active else None, 1 if active else 0, hub_seen))
                if active:
                    active_macs.add(mac)
                    if not first_run:
                        label = name or "no name given"
                        log_event(c, "new", mac, "New device: " + label)
                        LOOK_NOW.set()
                        notify("New device on the Wi-Fi",
                               "{}\n{}\nIP {}".format(label, hint or "No clues from the hub", d.get("ip")),
                               tags="warning")
                continue

            c.execute("""UPDATE devices SET hub_name=?, ip=?, connection=?, hub_hint=?,
                         hub_last_active=COALESCE(?, hub_last_active) WHERE mac=?""",
                      (name or row["hub_name"], d.get("ip") or row["ip"],
                       d["connection"] or row["connection"], hint or row["hub_hint"], hub_seen, mac))

            if active:
                active_macs.add(mac)
                c.execute("UPDATE devices SET last_seen=?, online=1 WHERE mac=?", (t, mac))
                if not row["online"] and not row["ignored"]:
                    log_event(c, "online", mac, display_name(row) + " connected")
                    if not row["friendly_name"]:
                        LOOK_NOW.set()

        if first_run is not None and not getattr(poll_once, "logged", False):
            sample = [d.get("time_last_active") for d in devices if d.get("time_last_active")][:3]
            log.info("Hub's 'last active' times look like: %s", sample or "nothing given")
            poll_once.logged = True

        if first_run and devices:
            log_event(c, "new", None,
                      "First look: found {} devices the hub knows about".format(len(devices)))

        for row in c.execute("SELECT * FROM devices WHERE online=1").fetchall():
            if row["mac"] not in active_macs:
                c.execute("UPDATE devices SET online=0 WHERE mac=?", (row["mac"],))
                if not row["ignored"]:
                    log_event(c, "offline", row["mac"], display_name(row) + " disconnected")

        update_people(c)


def update_people(c):
    cutoff = now() - config.AWAY_AFTER_MINUTES * 60
    owners = c.execute("""SELECT owner, MAX(last_seen) AS last FROM devices
                          WHERE counts_for_presence=1 AND owner IS NOT NULL AND owner<>''
                            AND COALESCE(ignored,0)=0
                          GROUP BY owner""").fetchall()
    for o in owners:
        home = 1 if o["last"] and o["last"] >= cutoff else 0
        person = c.execute("SELECT * FROM people WHERE name=?", (o["owner"],)).fetchone()
        if person is None:
            c.execute("INSERT INTO people(name, is_home, since) VALUES (?,?,?)",
                      (o["owner"], home, now()))
            continue
        if person["is_home"] != home:
            c.execute("UPDATE people SET is_home=?, since=? WHERE name=?", (home, now(), o["owner"]))
            msg = o["owner"] + (" is home" if home else " has gone out")
            log_event(c, "arrived" if home else "left", None, msg)
            if config.NOTIFY_ARRIVALS:
                notify(msg, msg, tags="house" if home else "wave")
    c.execute("""DELETE FROM people WHERE name NOT IN (
                 SELECT DISTINCT owner FROM devices
                 WHERE counts_for_presence=1 AND owner IS NOT NULL AND owner<>'')""")


def tidy_history():
    cutoff = now() - config.HISTORY_DAYS * 86400
    with DB_LOCK, db() as c:
        n = c.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
    if n:
        log.info("Tidied %s old history entries", n)
    if config.FORGET_AFTER_DAYS:
        gone = now() - config.FORGET_AFTER_DAYS * 86400
        with DB_LOCK, db() as c:
            n = c.execute("DELETE FROM devices WHERE known=0 AND online=0 AND {} < ?".format(SEEN_AT),
                          (gone,)).rowcount
        if n:
            log.info("Forgot %s unnamed devices not seen for %s days", n, config.FORGET_AFTER_DAYS)


def watcher():
    last_tidy = None
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("Couldn't read the hub this time, will try again")
        today = time.strftime("%Y-%m-%d")
        if last_tidy != today:
            tidy_history()
            last_tidy = today
        time.sleep(config.POLL_SECONDS)


# ---------- web pages ----------

def esc(v):
    return html.escape("" if v is None else str(v))


def ago(ts):
    if not ts:
        return "never"
    secs = now() - ts
    if secs < 90:
        return "just now"
    if secs < 3600:
        return "{} min ago".format(secs // 60)
    if secs < 86400:
        return "{} hr ago".format(secs // 3600)
    return "{} days ago".format(secs // 86400)


def local(ts):
    return time.strftime("%a %d %b %H:%M", time.localtime(ts)) if ts else ""


STYLE = """
:root { --bg:#171a20; --panel:#21252d; --panel2:#2a2f38; --line:#3a404b; --ink:#f5f6f8; --ti:#d6dae0;
        --muted:#a7adb7; --red:#ff4545; --red-dim:#5a1414; --home:#3ef08c; --away:#6b717b;
        --wired:#b45cff; --fast:#2ee87f; --slow:#ffd23f; --none:#4a4f57; --warn:var(--red); --accent:var(--red); }
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:16px/1.5 "Barlow Semi Condensed", "Segoe UI", Roboto, system-ui, sans-serif;
       background-image:linear-gradient(180deg, #1f232b 0, var(--bg) 260px); min-height:100vh; }
a { color:var(--ink); }
.num { font-variant-numeric:tabular-nums; }

/* top bar */
header { position:sticky; top:0; z-index:5; display:flex; align-items:stretch; gap:0;
         background:#000; border-bottom:2px solid var(--red); }
.brand { white-space:nowrap; display:flex; align-items:center; gap:10px; padding:12px 22px 12px 16px; margin-right:auto;
         background:linear-gradient(100deg, #000 70%, transparent 70.5%); }
.brand b { font-size:19px; font-weight:700; font-style:italic; letter-spacing:.04em; text-transform:uppercase; }
.brand b span { color:var(--red); }
.live { display:inline-flex; align-items:center; gap:6px; font-size:11px; font-weight:700; letter-spacing:.14em;
        color:var(--ti); border:1px solid var(--line); padding:2px 8px; }
.live i { width:7px; height:7px; border-radius:50%; background:var(--red); animation:pulse 1.6s infinite; }
@keyframes pulse { 0%,100% { opacity:1 } 50% { opacity:.25 } }
nav { display:flex; }
nav a { white-space:nowrap; display:flex; align-items:center; padding:0 14px; color:var(--ti); text-decoration:none;
        font-size:14px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; border-left:1px solid #15171b; }
nav a.on { color:#fff; background:var(--red); }
main { max-width:980px; margin:0 auto; padding:18px 16px 40px; }

/* section titles */
h2 { display:flex; align-items:center; gap:10px; font-size:14.5px; font-weight:700; letter-spacing:.16em;
     text-transform:uppercase; color:var(--ti); margin:28px 0 10px; }
h2::before { content:""; width:14px; height:14px; background:var(--red); transform:skewX(-20deg); }
h2 .count { margin-left:auto; color:var(--muted); font-weight:600; letter-spacing:.06em; }

/* stat strip */
.stats { display:grid; grid-template-columns:repeat(3,1fr); gap:2px; background:var(--line); border:1px solid var(--line); }
.stat { background:var(--panel); padding:12px 14px; }
.stat .v { font-size:34px; font-weight:700; font-style:italic; line-height:1; }
.stat .v small { font-size:16px; color:var(--muted); font-style:normal; font-weight:600; }
.stat .k { font-size:12.5px; font-weight:700; letter-spacing:.14em; text-transform:uppercase; color:var(--ti); margin-top:6px; }

/* race control banner */
.rc { display:flex; align-items:center; gap:12px; margin-top:14px; padding:10px 14px; background:var(--panel);
      border-left:4px solid var(--red); text-decoration:none; }
.rc .tag { white-space:nowrap; font-size:11px; font-weight:700; letter-spacing:.14em; background:var(--red); color:#fff; padding:2px 7px; }

/* timing tower */
.tower { border:1px solid var(--line); background:var(--panel); }
.tower .r { display:grid; grid-template-columns:34px 5px 1fr auto; align-items:center; gap:0 12px;
            padding:9px 12px 9px 0; border-top:1px solid var(--line); text-decoration:none; color:inherit; }
.tower .r:first-child { border-top:0; }
.tower .r:hover { background:var(--panel2); }
.tower .pos { text-align:right; font-weight:700; font-style:italic; color:var(--ti); font-size:16px; }
.tower .stripe { align-self:stretch; width:5px; transform:skewX(-12deg); background:var(--none); }
.tower .nm { font-weight:700; text-transform:uppercase; letter-spacing:.03em; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tower .sub { font-size:14px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tower .rt { text-align:right; white-space:nowrap; }
.tower .ip { font-size:13.5px; color:var(--muted); }
.big .nm { font-size:21px; font-style:italic; }
.pill { display:inline-block; font-size:14px; font-weight:700; letter-spacing:.12em; padding:3px 12px; text-transform:uppercase; }
.pill.home { background:rgba(62,240,140,.16); color:#7dffb3; border:1px solid var(--home); }
.pill.out { background:transparent; color:var(--ti); border:1px solid var(--away); }
.gap { display:block; font-size:14px; color:var(--ti); margin-top:5px; }

/* connection colours, like sector times */
.s-wired { background:var(--wired) !important; } .s-fast { background:var(--fast) !important; }
.s-slow { background:var(--slow) !important; }
.t-wired { color:var(--wired); } .t-fast { color:var(--fast); } .t-slow { color:var(--slow); }
.conn { font-size:12.5px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:12px; color:var(--muted); letter-spacing:.08em;
          text-transform:uppercase; margin-top:8px; }
.legend i { display:inline-block; width:10px; height:10px; margin-right:5px; transform:skewX(-12deg); vertical-align:-1px; }

/* race control feed */
.feed { border:1px solid var(--line); background:var(--panel); }
.feed .m { display:grid; grid-template-columns:62px 1fr; gap:12px; padding:8px 12px; border-top:1px solid var(--line); }
.feed .m:first-child { border-top:0; }
.feed .t { color:var(--ti); font-size:14px; }
.feed .lbl { text-transform:uppercase; font-size:12px; font-weight:700; letter-spacing:.14em; margin-right:8px; padding:1px 6px; }
.lbl.new { background:var(--red); color:#fff; } .lbl.in { background:rgba(62,240,140,.16); color:#7dffb3; border:1px solid var(--home); }
.lbl.out { border:1px solid var(--away); color:var(--ti); }

/* shared bits used on the other pages */
.card { background:var(--panel); border:1px solid var(--line); border-left:3px solid var(--line); padding:14px; }
.muted { color:var(--muted); font-size:14.5px; }
.muted a { color:var(--ti); }
.warn { color:var(--red); }
.badge { display:inline-block; padding:1px 7px; font-size:11px; font-weight:700; letter-spacing:.12em;
         text-transform:uppercase; background:var(--red); color:#fff; }
button, .btn { font:inherit; font-size:12px; font-weight:700; letter-spacing:.1em; text-transform:uppercase;
               padding:7px 13px; border:1px solid var(--line); background:var(--panel2); color:var(--ink);
               cursor:pointer; text-decoration:none; border-radius:0; display:inline-block; }
button:hover, .btn:hover { border-color:var(--ti); }
button.primary { background:var(--red); border-color:var(--red); color:#fff; }
input[type=text], select { font:inherit; padding:7px 9px; border:1px solid var(--line); border-radius:0;
       background:#0d0f12; color:var(--ink); width:100%; margin-top:3px; }
input[type=text]:focus, select:focus { outline:none; border-color:var(--red); }
label { font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; color:var(--ti); }
input[type=checkbox] { accent-color:var(--red); }
table { width:100%; border-collapse:collapse; }
td, th { text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
th { font-size:11px; color:var(--muted); font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
.fields { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
form.card { margin-bottom:10px; }
form.card strong { text-transform:uppercase; letter-spacing:.03em; font-style:italic; }
@media (max-width:560px) {
  .brand { padding:12px 10px 12px 12px; background:#000; } .brand b { font-size:15px; } .live { display:none; }
  nav a { padding:0 9px; font-size:11px; letter-spacing:.06em; }
  .stat .v { font-size:26px; }
  .tower .r { grid-template-columns:26px 4px 1fr auto; gap:0 10px; }
}
"""

FONTS = "<style>@font-face{font-family:'Barlow Semi Condensed';font-weight:500;font-style:normal;font-display:swap;src:url(/fonts/barlow-semi-condensed-latin-500-normal.woff2) format('woff2')}@font-face{font-family:'Barlow Semi Condensed';font-weight:600;font-style:normal;font-display:swap;src:url(/fonts/barlow-semi-condensed-latin-600-normal.woff2) format('woff2')}@font-face{font-family:'Barlow Semi Condensed';font-weight:700;font-style:normal;font-display:swap;src:url(/fonts/barlow-semi-condensed-latin-700-normal.woff2) format('woff2')}@font-face{font-family:'Barlow Semi Condensed';font-weight:600;font-style:italic;font-display:swap;src:url(/fonts/barlow-semi-condensed-latin-600-italic.woff2) format('woff2')}@font-face{font-family:'Barlow Semi Condensed';font-weight:700;font-style:italic;font-display:swap;src:url(/fonts/barlow-semi-condensed-latin-700-italic.woff2) format('woff2')}</style>"


def page(path, body, refresh=False):
    def nav(href, label, on):
        return '<a href="{}" class="{}">{}</a>'.format(href, "on" if on else "", label)
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{}
<meta name="theme-color" content="#000000"><title>Who's Home</title>{}<style>{}</style></head><body>
<header><div class="brand"><b>Who's <span>Home</span></b><span class="live"><i></i>Live</span></div>
<nav>{}{}{}</nav></header><main>{}</main></body></html>""".format(
        '<meta http-equiv="refresh" content="60">' if refresh else "", FONTS, STYLE,
        nav("/", "Pit wall", path == "/"), nav("/devices", "Devices", path.startswith("/devices")),
        nav("/history", "History", path == "/history"), body)


def sector(conn):
    """Colour each connection like an F1 sector: purple wired, green 5GHz, yellow 2.4GHz."""
    c = (conn or "").lower()
    if "wired" in c or "eth" in c:
        return "wired"
    if "5" in c:
        return "fast"
    if "2.4" in c or "2g" in c:
        return "slow"
    return ""


def duration(ts):
    if not ts:
        return ""
    secs = max(0, now() - ts)
    if secs >= 86400:
        return "{}d {}h".format(secs // 86400, secs % 86400 // 3600)
    if secs >= 3600:
        return "{}h {}m".format(secs // 3600, secs % 3600 // 60)
    return "{}m".format(secs // 60)


def home_page():
    with db() as c:
        people = c.execute("SELECT * FROM people ORDER BY is_home DESC, since").fetchall()
        online = c.execute("""SELECT * FROM devices WHERE online=1 AND COALESCE(ignored,0)=0 ORDER BY known,
                              COALESCE(owner,'zzz'), COALESCE(friendly_name, hub_name)""").fetchall()
        unknown = c.execute("SELECT COUNT(*) FROM devices WHERE known=0 AND " + not_quiet()).fetchone()[0]
        events = c.execute("""SELECT * FROM events WHERE kind IN ('new','arrived','left')
                              ORDER BY ts DESC LIMIT 25""").fetchall()
        new_today = c.execute("SELECT COUNT(*) FROM events WHERE kind='new' AND mac IS NOT NULL AND ts>=?",
                              (now() - 86400,)).fetchone()[0]
    home_n = sum(1 for p in people if p["is_home"])
    out = ['<div class="stats">',
           '<div class="stat"><div class="v num">{}<small>/{}</small></div><div class="k">Home</div></div>'
           .format(home_n, len(people)),
           '<div class="stat"><div class="v num">{}</div><div class="k">On the network</div></div>'.format(len(online)),
           '<div class="stat"><div class="v num" style="color:{}">{}</div><div class="k">New today</div></div>'
           .format("var(--red)" if new_today else "inherit", new_today),
           '</div>']
    if unknown:
        out.append('<a class="rc" href="/devices?show=unknown"><span class="tag">Race control</span>'
                   '<span>{} device{} naming</span><span class="muted" style="margin-left:auto;white-space:nowrap">Review &rsaquo;</span></a>'
                   .format(unknown, "s need" if unknown != 1 else " needs"))

    out.append("<h2>Who's in</h2>")
    if people:
        out.append('<div class="tower big">')
        for i, p in enumerate(people, 1):
            out.append('<a class="r" href="/devices?show={}"><span class="pos num">{}</span>'
                       '<span class="stripe" style="background:{}"></span>'
                       '<span class="nm">{}</span>'
                       '<span class="rt"><span class="pill {}">{}</span><span class="gap num">{} &middot; since {}</span></span></a>'
                       .format(esc(urllib.parse.quote("owner:" + p["name"])), i,
                               "var(--home)" if p["is_home"] else "var(--away)", esc(p["name"]),
                               "home" if p["is_home"] else "out", "Home" if p["is_home"] else "Out",
                               duration(p["since"]), time.strftime("%H:%M", time.localtime(p["since"]))))
        out.append("</div>")
    else:
        out.append('<p class="muted">Nobody set up yet. On the <a href="/devices">Devices</a> page, '
                   'give each family phone an owner and tick "Counts for who\'s in".</p>')

    out.append('<h2>On the network <span class="count num">{}</span></h2><div class="tower">'.format(len(online)))
    for i, d in enumerate(online, 1):
        sec = sector(d["connection"])
        kind = d["device_type"] or d["found_model"] or ("" if d["friendly_name"] else guess(d))
        sub = " · ".join(x for x in (d["owner"], kind) if x)
        out.append('<a class="r" href="/devices?show={}"><span class="pos num">{}</span>'
                   '<span class="stripe {}"></span>'
                   '<span style="min-width:0"><div class="nm">{}{}</div><div class="sub">{}</div></span>'
                   '<span class="rt"><div class="conn {}">{}</div><div class="ip num">{}</div></span></a>'
                   .format(esc(urllib.parse.quote("mac:" + d["mac"])), i,
                           "s-" + sec if sec else "",
                           '<span class="badge">New</span> ' if not d["known"] else "", esc(display_name(d)),
                           esc(sub), "t-" + sec if sec else "", esc(d["connection"] or "-"), esc(d["ip"])))
    if not online:
        out.append('<div class="r"><span></span><span></span><span class="muted">Nothing yet. '
                   'Give it a minute to check the hub.</span><span></span></div>')
    out.append('</div><div class="legend"><span><i class="s-wired"></i>Wired</span>'
               '<span><i class="s-fast"></i>Wi-Fi 5GHz</span><span><i class="s-slow"></i>Wi-Fi 2.4GHz</span></div>')

    out.append('<h2>Race control</h2><div class="feed">')
    for e in events:
        kind = {"new": ("new", "New"), "arrived": ("in", "In"), "left": ("out", "Out")}[e["kind"]]
        out.append('<div class="m"><span class="t num">{}</span><span><span class="lbl {}">{}</span>{}</span></div>'
                   .format(time.strftime("%H:%M", time.localtime(e["ts"])) if e["ts"] >= now() - 86400
                           else time.strftime("%d %b", time.localtime(e["ts"])),
                           kind[0], kind[1], esc(e["detail"])))
    if not events:
        out.append('<div class="m"><span></span><span class="muted">All quiet.</span></div>')
    out.append("</div>")
    out.append('<form method="post" action="/test-notification" style="margin-top:22px">'
               '<button>Send a test notification</button></form>')
    return page("/", "".join(out), refresh=True)


def devices_page(show):
    q = "SELECT *, {} AS seen_at FROM devices WHERE ".format(SEEN_AT)
    args = ()
    title = ""
    if show.startswith("owner:"):
        q += "owner=?"
        args = (show[6:],)
        title = show[6:] + "'s devices"
    elif show.startswith("mac:"):
        q += "mac=?"
        args = (show[4:],)
        title = "Edit device"
    elif show == "ignored":
        q += IGNORED
    elif show == "quiet":
        q += is_quiet()
    elif show == "unknown":
        q += "known=0 AND " + not_quiet()
    else:
        q += not_quiet()
    q += " ORDER BY known, online DESC, COALESCE(owner,'zzz'), COALESCE(friendly_name, hub_name)"
    with db() as c:
        rows = c.execute(q, args).fetchall()
        quiet_count = c.execute("SELECT COUNT(*) FROM devices WHERE " + is_quiet()).fetchone()[0]
        ignored_count = c.execute("SELECT COUNT(*) FROM devices WHERE " + IGNORED).fetchone()[0]
        owners = [r[0] for r in c.execute(
            "SELECT DISTINCT owner FROM devices WHERE owner IS NOT NULL AND owner<>'' ORDER BY owner")]

    bold = lambda k: ' style="font-weight:700"' if show == k else ""
    out = ['<div class="row">',
           '<a class="btn" href="/devices"{}>Current</a>'.format(bold("all")),
           '<a class="btn" href="/devices?show=unknown"{}>Still to name</a>'.format(bold("unknown")),
           '<a class="btn" href="/devices?show=quiet"{}>Gone quiet ({})</a>'.format(bold("quiet"), quiet_count),
           '<a class="btn" href="/devices?show=ignored"{}>Ignored ({})</a>'.format(bold("ignored"), ignored_count),
           '<span class="muted" style="margin-left:auto">{} devices</span></div>'.format(len(rows))]
    if title:
        out.append('<h2>{}</h2>'.format(esc(title)))
        if show.startswith("owner:"):
            out.append('<p class="muted">Untick "Counts for who\'s in" on a device to stop it counting, '
                       'or change the owner to move it to someone else.</p>')
    if show == "ignored":
        out.append('<p class="muted">Ignored devices are hidden from everything, never count for who\'s in, '
                   'and don\'t fill up the history. They stay on your network though. To actually block a device, '
                   'use the hub\'s own <a href="http://{}/" target="_blank" rel="noopener">Hub Manager</a>.</p>'
                   .format(esc(config.HUB_IP)))
    if show == "quiet":
        out.append('<p class="muted">Devices not seen for over {} days. They\'re hidden everywhere else. '
                   'Forgetting them clears them out completely, and if one ever comes back you\'ll get a '
                   'new device alert.</p>'.format(config.QUIET_AFTER_DAYS))
        if rows:
            out.append('<form method="post" action="/devices/forget-quiet" style="margin-bottom:14px">'
                       '<button onclick="return confirm(\'Forget all {} of these?\')">Forget all of these</button>'
                       '</form>'.format(len(rows)))
    out += [
           '<p class="muted">Give each device a name you\'ll recognise. For the family\'s own phones, add who '
           'owns it and tick "Counts for who\'s in". Saving marks a device as known, so it won\'t show as new '
           'again. If you\'re not sure what something is, switch it off and see which one drops off.</p>',
           '<datalist id="owners">{}</datalist>'.format(
               "".join('<option value="{}">'.format(esc(o)) for o in owners))]

    for d in rows:
        anchor = "d-" + d["mac"].replace(":", "")
        types = "".join('<option{}>{}</option>'.format(" selected" if d["device_type"] == t else "", esc(t))
                        for t in DEVICE_TYPES)
        out.append("""
<form class="card" id="{anchor}" method="post" action="/devices/save?show={showq}" style="margin-bottom:10px">
  <input type="hidden" name="mac" value="{mac}">
  <div class="row">{badge}<strong>{name}</strong><span class="muted">{status}</span>{connbadge}</div>
  {found}<div class="muted" style="margin:4px 0 10px">Hub calls it "{hub}" · {ip}{hint}
    <br>{mac} · first seen {first}</div>
  <div class="fields">
    <label>Name<input type="text" name="friendly_name" value="{fname}" placeholder="{suggest}"></label>
    <label>Owner<input type="text" name="owner" list="owners" value="{owner}" placeholder="e.g. Sam, or Family"></label>
    <label>Type<select name="device_type"><option value="">Choose...</option>{types}</select></label>
  </div>
  <div class="row" style="margin-top:10px">
    <label><input type="checkbox" name="counts_for_presence"{checked}> Counts for who's in</label>
    <button class="primary" style="margin-left:auto">Save</button>
    <button type="button" onclick="doPing(this, '{mac}')">Ping</button>
    {weblink}
    <span id="p-{anchor_id}" class="muted"></span>
    <button formaction="/devices/{ignore_action}">{ignore_label}</button>
    <button formaction="/devices/forget" onclick="return confirm('Forget this device? It will show as new if it comes back.')">Forget</button>
  </div>
</form>""".format(
            found=('<div style="margin:6px 0 2px">It calls itself <strong>{0}</strong>{1}'
                   ' <span class="muted">(via {2})</span> '
                   '<button type="button" onclick="this.form.friendly_name.value={3}">Use this name</button></div>'
                   .format(esc(d["found_name"]), " · " + esc(d["found_model"]) if d["found_model"] else "",
                           esc(d["found_via"]), esc(json.dumps(d["found_name"]))) if d["found_name"]
                   else '<div style="margin:6px 0 2px">{0}{1} '
                        '<button type="button" onclick="this.form.friendly_name.value={2}">Use {3}</button></div>'
                        .format("Looks like <strong>{}</strong>".format(esc(guess(d))) if guess(d) else "",
                                (" · " if guess(d) else "") + "hub says <strong>{}</strong>".format(
                                    esc(hub_label(d["hub_name"]))) if hub_label(d["hub_name"]) else "",
                                esc(json.dumps(hub_label(d["hub_name"]) or guess(d))),
                                esc("\u201c{}\u201d".format(hub_label(d["hub_name"]) or guess(d))))
                   if (guess(d) or hub_label(d["hub_name"])) else ""),
            connbadge=' <span class="conn">{}</span>'.format(esc(d["connection"])) if d["connection"] else "",
            ignore_action="unignore" if d["ignored"] else "ignore",
            ignore_label="Stop ignoring" if d["ignored"] else "Ignore",
            showq=esc(urllib.parse.quote(show)), anchor=anchor, anchor_id=d["mac"].replace(":", ""), show=esc(show), mac=esc(d["mac"]),
            weblink='<a class="btn" href="http://{0}/" target="_blank" rel="noopener">Open its web page</a>'
                    .format(esc(d["ip"])) if d["ip"] else "",
            badge='<span class="badge">New</span>' if not d["known"] else "",
            name=esc(display_name(d)),
            status="● online" if d["online"] else "last seen " + ago(d["seen_at"] or None),
            hub=esc(d["hub_name"] or "nothing"), ip=esc(d["ip"] or "no IP"),
            hint="<br>Clues: " + esc(d["hub_hint"]) if d["hub_hint"] else "",
            first=local(d["first_seen"]), fname=esc(d["friendly_name"]),
            suggest=esc(d["found_name"] or hub_label(d["hub_name"]) or guess(d))
                    if (d["found_name"] or hub_label(d["hub_name"]) or guess(d)) else esc("e.g. {} {}".format(maker(d["mac"]), d["device_type"] or "device")
                        if maker(d["mac"]) else "e.g. Sam's phone"), owner=esc(d["owner"]),
            types=types, checked=" checked" if d["counts_for_presence"] else ""))
    if not rows:
        out.append('<p class="muted">Nothing here.</p>')
    out.append('<form method="post" action="/look-for-names" style="margin-top:20px">'
               '<button>Ask devices their names now</button> <span class="muted">It also does this every {} '
               'minutes. Give it about 15 seconds, then refresh.</span></form>'.format(config.DISCOVER_MINUTES))
    out.append('<p class="muted" style="margin-top:20px"><a href="/report">Copy a report of every device</a> · '
               '<a href="/hub">See exactly what the hub reports</a></p>')
    return page("/devices", "".join(out) + PING_SCRIPT)


def ping(ip):
    """Send the device three pings and say how it went, in plain words."""
    if not ip or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
        return {"ok": False, "text": "No address to ping"}
    try:
        r = subprocess.run(["ping", "-c", "3", "-W", "1", ip], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=10)
    except Exception as e:
        return {"ok": False, "text": "Couldn't run ping: {}".format(e)}
    got = re.search(r"(\d+) (?:packets )?received", r.stdout)
    avg = re.search(r"= [\d.]+/([\d.]+)/", r.stdout)
    n = int(got.group(1)) if got else 0
    if n == 0:
        return {"ok": False, "text": "No answer (it may be off, asleep, or set to ignore pings)"}
    return {"ok": True, "text": "Answered {} of 3{}".format(
        n, ", about {:.0f} ms".format(float(avg.group(1))) if avg else "")}


PING_SCRIPT = """<script>
function doPing(btn, mac) {
  var out = document.getElementById('p-' + mac.replace(/:/g, ''));
  btn.disabled = true; out.textContent = 'Pinging...'; out.className = 'muted';
  fetch('/ping?mac=' + encodeURIComponent(mac)).then(function (r) { return r.json(); })
    .then(function (j) { out.textContent = j.text; out.style.color = j.ok ? 'var(--home)' : 'var(--warn)'; })
    .catch(function () { out.textContent = 'Ping failed'; })
    .then(function () { btn.disabled = false; });
}
</script>"""


def hub_page():
    """Everything the hub says, untouched. Handy for working out odd ones."""
    try:
        devices = read_hub()
    except Exception as e:
        return page("/hub", "<p>Couldn't read the hub: {}</p>".format(esc(e)))
    keys = ["mac", "hostname", "ip", "activity", "connection", "port", "time_last_active", "time_first_seen",
            "os", "device", "device_class", "device_oui"]
    devices.sort(key=lambda d: str(d.get("activity")) != "1")
    out = ['<p class="muted">Exactly what the hub reports, {} devices. Nothing here is saved.</p>'
           '<div class="card" style="overflow-x:auto"><table><tr>{}</tr>'.format(
               len(devices), "".join("<th>{}</th>".format(k) for k in keys))]
    for d in devices:
        out.append("<tr>{}</tr>".format("".join(
            '<td style="white-space:nowrap">{}</td>'.format(esc(d.get(k))) for k in keys)))
    out.append("</table></div>")
    return page("/hub", "".join(out))


def report_page():
    """Everything we know about each current device, as plain text you can copy and paste."""
    with db() as c:
        rows = c.execute("SELECT * FROM devices WHERE " + not_quiet() +
                         " ORDER BY friendly_name IS NOT NULL, ip").fetchall()
    lines = []
    for d in rows:
        lines.append(" | ".join([
            d["ip"] or "-",
            ("NAMED: " + d["friendly_name"]) if d["friendly_name"] else "unnamed",
            "maker: " + (maker(d["mac"]) or ("private address" if is_random_mac(d["mac"]) else "?")),
            "hub: " + (d["hub_name"] or "-"),
            "clues: " + (d["hub_hint"] or "-"),
            "conn: " + (d["connection"] or "-"),
            "calls itself: " + (d["found_name"] or "-"),
            "model: " + (d["found_model"] or "-"),
            "offers: " + (d["found_services"] or "-"),
            "guess: " + (guess(d) or "-"),
        ]))
    text = "\n".join(lines)
    return page("/report", '<p class="muted">Everything known about each current device. Unnamed ones are at '
                'the top. Copy it and paste it to Claude to get better guesses.</p>'
                '<button onclick="var r=document.createRange();r.selectNodeContents(document.getElementById(\'r\'));'
                'var s=window.getSelection();s.removeAllRanges();s.addRange(r);'
                'this.textContent=document.execCommand(\'copy\')?\'Copied\':\'Selected, now copy it\'">Copy all</button>'
                '<pre id="r" class="card" style="white-space:pre-wrap;font-size:13px;margin-top:10px">{}</pre>'
                .format(esc(text)))


def history_page():
    with db() as c:
        events = c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 500").fetchall()
    out = ['<p class="muted">The last 500 things that happened. Anything older than {} days is tidied away '
           'automatically.</p><div class="card"><table><tr><th>When</th><th>What</th></tr>'.format(
               config.HISTORY_DAYS)]
    for e in events:
        out.append('<tr><td class="muted" style="white-space:nowrap">{}</td><td class="{}">{}</td></tr>'.format(
            local(e["ts"]), "warn" if e["kind"] == "new" else "", esc(e["detail"])))
    if not events:
        out.append('<tr><td colspan="2" class="muted">Nothing yet.</td></tr>')
    out.append("</table></div>")
    return page("/history", "".join(out))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the log quiet

    def send(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_font(self, name):
        if not re.match(r"^[a-z0-9-]+\.woff2$", name) or not os.path.exists(os.path.join(HERE, "fonts", name)):
            self.send(page("/", "<p>Not found.</p>"), 404)
            return
        with open(os.path.join(HERE, "fonts", name), "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "font/woff2")
        self.send_header("Cache-Control", "public, max-age=31536000")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                self.send(home_page())
            elif url.path == "/devices":
                self.send(devices_page(qs.get("show", ["all"])[0]))
            elif url.path == "/ping":
                mac = qs.get("mac", [""])[0]
                with db() as c:
                    row = c.execute("SELECT ip FROM devices WHERE mac=?", (mac,)).fetchone()
                data = json.dumps(ping(row["ip"] if row else "")).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif url.path == "/history":
                self.send(history_page())
            elif url.path.startswith("/fonts/"):
                self.send_font(url.path[len("/fonts/"):])
            elif url.path == "/report":
                self.send(report_page())
            elif url.path == "/hub":
                self.send(hub_page())
            else:
                self.send(page("/", "<p>Not found.</p>"), 404)
        except Exception:
            log.exception("Page error")
            self.send(page("/", "<p>Something went wrong, check the log.</p>"), 500)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
        f = lambda k: (form.get(k, [""])[0] or "").strip()
        show = urllib.parse.parse_qs(url.query).get("show", ["all"])[0]
        mac = f("mac")

        if url.path == "/devices/save" and mac:
            with DB_LOCK, db() as c:
                c.execute("""UPDATE devices SET friendly_name=?, owner=?, device_type=?,
                             counts_for_presence=?, known=1 WHERE mac=?""",
                          (f("friendly_name") or None, f("owner") or None, f("device_type") or None,
                           1 if f("counts_for_presence") else 0, mac))
                update_people(c)
            self.redirect("/devices?show={}#d-{}".format(urllib.parse.quote(show), mac.replace(":", "")))
        elif url.path == "/devices/forget" and mac:
            with DB_LOCK, db() as c:
                c.execute("DELETE FROM devices WHERE mac=?", (mac,))
            self.redirect("/devices")
        elif url.path in ("/devices/ignore", "/devices/unignore") and mac:
            with DB_LOCK, db() as c:
                c.execute("UPDATE devices SET ignored=?, known=1 WHERE mac=?",
                          (1 if url.path.endswith("/ignore") else 0, mac))
                update_people(c)
            self.redirect("/devices?show={}".format(urllib.parse.quote(show)))
        elif url.path == "/devices/forget-quiet":
            with DB_LOCK, db() as c:
                n = c.execute("DELETE FROM devices WHERE " + is_quiet()).rowcount
            log.info("Forgot %s quiet devices", n)
            self.redirect("/devices")
        elif url.path == "/look-for-names":
            LOOK_NOW.set()
            self.redirect("/devices?show={}".format(urllib.parse.quote(show)))
        elif url.path == "/test-notification":
            notify("Who's Home test", "If you can read this, notifications are working.")
            self.redirect("/")
        else:
            self.redirect("/")


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    setup_db()
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=name_finder, daemon=True).start()
    log.info("Who's Home running on port %s", config.WEB_PORT)
    Server(("0.0.0.0", config.WEB_PORT), Handler).serve_forever()
