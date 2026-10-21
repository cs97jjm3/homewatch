"""Ask devices on the home network what they call themselves.

Lots of devices will happily tell you their own name if you ask the right way:
  - Bonjour / mDNS: Apple kit, Chromecasts, Google and Amazon speakers, printers, many smart devices
  - UPnP / SSDP: smart TVs, games consoles, Sonos, media boxes, routers
  - NetBIOS: Windows PCs

Python 3.5 or newer, nothing to install. Everything stays on the home network.
"""

import logging
import re
import socket
import struct
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("whoshome.discover")

MDNS = ("224.0.0.251", 5353)
SSDP = ("239.255.255.250", 1900)

# Service types worth asking about directly, as some devices don't answer the general question
SERVICES = [
    "_googlecast._tcp", "_airplay._tcp", "_raop._tcp", "_device-info._tcp", "_companion-link._tcp",
    "_spotify-connect._tcp", "_ipp._tcp", "_ipps._tcp", "_printer._tcp", "_pdl-datastream._tcp",
    "_hap._tcp", "_homekit._tcp", "_sonos._tcp", "_amzn-wplay._tcp", "_smb._tcp", "_workstation._tcp",
    "_androidtvremote2._tcp", "_googlezone._tcp", "_mediaremotetv._tcp", "_sleep-proxy._udp",
    "_http._tcp", "_ssh._tcp", "_rdlink._tcp", "_matter._tcp", "_meshcop._udp", "_hue._tcp",
    "_shelly._tcp", "_esphomelib._tcp", "_elg._tcp", "_nvstream._tcp", "_xbox._tcp",
]

# How much to trust each kind of name, highest first
RANK = {"cast": 1, "upnp": 2, "airplay": 3, "service": 4, "hostname": 5, "netbios": 6}


# ---------- DNS packets (used by mDNS) ----------

def encode_name(labels):
    out = b""
    for label in labels:
        raw = label.encode("utf-8")[:63]
        out += struct.pack("B", len(raw)) + raw
    return out + b"\x00"


def split_name(name):
    return [p for p in name.split(".") if p]


def build_query(names, qtype=12):
    """A DNS question for each name, asking for a direct reply."""
    packet = struct.pack(">HHHHHH", 0, 0, len(names), 0, 0, 0)
    for name in names:
        labels = name if isinstance(name, (list, tuple)) else split_name(name)
        packet += encode_name(labels) + struct.pack(">HH", qtype, 0x8001)
    return packet


def read_name(data, pos, depth=0):
    """Read a possibly-compressed DNS name. Returns (labels, next position)."""
    labels = []
    jumped = False
    end = pos
    for _ in range(128):
        if pos >= len(data):
            raise ValueError("name runs off the end")
        n = data[pos]
        if n == 0:
            pos += 1
            break
        if n & 0xC0 == 0xC0:
            if pos + 1 >= len(data) or depth > 10:
                raise ValueError("bad pointer")
            target = ((n & 0x3F) << 8) | data[pos + 1]
            if not jumped:
                end = pos + 2
            more, _ = read_name(data, target, depth + 1)
            labels.extend(more)
            jumped = True
            break
        pos += 1
        labels.append(data[pos:pos + n].decode("utf-8", "replace"))
        pos += n
    if not jumped:
        end = pos
    return labels, end


def parse_packet(data):
    """Pull out the useful records from an mDNS answer."""
    if len(data) < 12:
        return []
    _, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", data[:12])
    pos = 12
    for _ in range(qd):
        _, pos = read_name(data, pos)
        pos += 4
    records = []
    for _ in range(an + ns + ar):
        name, pos = read_name(data, pos)
        if pos + 10 > len(data):
            break
        rtype, _, _, rdlen = struct.unpack(">HHIH", data[pos:pos + 10])
        pos += 10
        rdata = data[pos:pos + rdlen]
        start = pos
        pos += rdlen
        try:
            if rtype == 12:  # PTR
                value, _ = read_name(data, start)
            elif rtype == 33:  # SRV
                value, _ = read_name(data, start + 6)
            elif rtype == 1 and rdlen == 4:  # A
                value = socket.inet_ntoa(rdata)
            elif rtype == 16:  # TXT
                value = {}
                i = 0
                while i < len(rdata):
                    ln = rdata[i]
                    item = rdata[i + 1:i + 1 + ln].decode("utf-8", "replace")
                    i += 1 + ln
                    k, _, v = item.partition("=")
                    if k:
                        value[k.lower()] = v
            else:
                continue
        except (ValueError, IndexError):
            continue
        records.append((tuple(name), rtype, value))
    return records


def listen(sock, seconds):
    """Collect (sender ip, packet) pairs for a few seconds."""
    got = []
    stop = time.time() + seconds
    while True:
        left = stop - time.time()
        if left <= 0:
            break
        sock.settimeout(left)
        try:
            data, addr = sock.recvfrom(9000)
            got.append((addr[0], data))
        except socket.timeout:
            break
        except OSError:
            break
    return got


# ---------- mDNS / Bonjour ----------

def mdns(ips, wait=3.0):
    """Returns {ip: [(rank, name, model, how)]}."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.bind(("", 0))
    replies = []
    try:
        # Round 1: what services are out there, plus the common ones by name
        names = ["_services._dns-sd._udp.local"] + [s + ".local" for s in SERVICES]
        for i in range(0, len(names), 10):
            sock.sendto(build_query(names[i:i + 10]), MDNS)
        # Ask each device directly for its own hostname
        for ip in ips:
            rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
            try:
                sock.sendto(build_query([rev]), (ip, 5353))
            except OSError:
                pass
        replies += listen(sock, wait)

        # Round 2: any service types we didn't already ask about
        extra = set()
        for _, data in replies:
            for name, rtype, value in parse_packet(data):
                if rtype == 12 and list(name[-3:]) == ["_dns-sd", "_udp", "local"]:
                    t = ".".join(value)
                    if t[:-len(".local")] not in SERVICES:
                        extra.add(t)
        if extra:
            extra = sorted(extra)
            for i in range(0, len(extra), 10):
                sock.sendto(build_query(extra[i:i + 10]), MDNS)
            replies += listen(sock, wait / 2)
    finally:
        sock.close()

    instances = {}   # instance name -> sender ip
    srv = {}         # instance -> target host
    txt = {}         # instance -> {key: value}
    a = {}           # host -> ip
    hostnames = {}   # ip -> hostname from reverse lookup
    for src, data in replies:
        for name, rtype, value in parse_packet(data):
            if rtype == 12:
                if name and name[-1] == "arpa":
                    if value:
                        hostnames[src] = value[0]
                elif len(value) >= 4 and value[-1] == "local" and value[1].startswith("_"):
                    instances.setdefault(tuple(value), src)
            elif rtype == 33:
                srv[name] = tuple(value)
                instances.setdefault(name, src)
            elif rtype == 16:
                txt.setdefault(name, {}).update(value)
            elif rtype == 1:
                a[tuple(name)] = value

    for inst, src in instances.items():
        ip = a.get(srv.get(inst), src)
        label = inst[0]
        kind = inst[1]
        t = txt.get(inst, {})
        rank, name, model = RANK["service"], label, ""
        if kind == "_googlecast":
            rank, name, model = RANK["cast"], t.get("fn") or label, t.get("md", "")
        elif kind in ("_airplay", "_raop"):
            rank = RANK["airplay"]
            name = label.split("@", 1)[1] if "@" in label else label
            model = t.get("model") or t.get("am", "")
        elif kind == "_device-info":
            model = t.get("model", "")
        elif kind in ("_ipp", "_ipps", "_printer", "_pdl-datastream"):
            model = t.get("ty", "")
        elif kind in ("_sleep-proxy", "_http", "_ssh"):
            rank = RANK["hostname"]
            name = re.sub(r"^[\d-]+ ", "", label)
        if kind == "_device-info":
            rank = RANK["hostname"]
        found.setdefault(ip, []).append((rank, name, model, "Bonjour", kind.lstrip("_")))

    for host, ip in a.items():
        if host and host[-1] == "local":
            found.setdefault(ip, []).append((RANK["hostname"], host[0], "", "Bonjour"))
    for ip, host in hostnames.items():
        found.setdefault(ip, []).append((RANK["hostname"], host, "", "Bonjour"))
    return found


# ---------- UPnP / SSDP ----------

def ssdp(wait=3.0):
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    sock.bind(("", 0))
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: ssdp:all\r\n\r\n").encode("ascii")
    try:
        sock.sendto(msg, SSDP)
        sock.sendto(msg, SSDP)
        replies = listen(sock, wait)
    finally:
        sock.close()

    locations = {}
    for src, data in replies:
        text = data.decode("utf-8", "replace")
        m = re.search(r"^location:\s*(\S+)", text, re.I | re.M)
        if m:
            locations.setdefault(m.group(1), src)

    done = set()
    for url, src in locations.items():
        if src in done and len(done) > 50:
            continue
        info = upnp_description(url)
        if info:
            found.setdefault(src, []).append(info)
            done.add(src)
    return found


def upnp_description(url):
    try:
        body = urllib.request.urlopen(url, timeout=3).read(200000)
        root = ET.fromstring(body)
    except Exception:
        return None

    def first(tag):
        for el in root.iter():
            if el.tag.split("}")[-1] == tag and el.text and el.text.strip():
                return el.text.strip()
        return ""

    name = first("friendlyName")
    maker = first("manufacturer")
    model = first("modelName") or first("modelDescription")
    if not name:
        return None
    model = " ".join(x for x in (maker, model) if x and x.lower() not in name.lower())
    return (RANK["upnp"], name, model, "UPnP")


# ---------- NetBIOS (Windows PCs) ----------

def netbios(ips, wait=2.0):
    found = {}
    query = (struct.pack(">HHHHHH", 0x5157, 0, 1, 0, 0, 0)
             + b"\x20" + b"CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" + b"\x00" + struct.pack(">HH", 0x21, 1))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    try:
        for ip in ips:
            try:
                sock.sendto(query, (ip, 137))
            except OSError:
                pass
        replies = listen(sock, wait)
    finally:
        sock.close()
    for src, data in replies:
        name = parse_netbios(data)
        if name:
            found.setdefault(src, []).append((RANK["netbios"], name, "", "Windows"))
    return found


def parse_netbios(data):
    try:
        pos = 12
        _, pos = read_name(data, pos)
        pos += 10  # type, class, ttl, length
        count = data[pos]
        pos += 1
        for _ in range(count):
            raw = data[pos:pos + 15].decode("ascii", "replace").strip()
            suffix = data[pos + 15]
            flags = struct.unpack(">H", data[pos + 16:pos + 18])[0]
            pos += 18
            if suffix == 0 and not flags & 0x8000 and raw:
                return raw
    except (IndexError, ValueError, struct.error):
        pass
    return ""


# ---------- knocking on doors ----------

# Ports that give a device away. Checking them is the same as a browser trying to load a page.
PORTS = [62078, 7000, 8009, 8008, 4070, 55443, 9295, 8060, 1400, 445, 3389, 548, 631, 9100,
         6668, 22, 80, 8080, 443, 5000, 49152, 1900]


def port_open(ip, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


WEB_JUNK = re.compile(r"(?i)^(index|home|login|log in|sign in|welcome|web ?server|default|untitled|"
                      r"404|403|401|error|not found|forbidden|unauthorized|document|redirect|opening|loading)\b|redirect")


def web_title(ip, port):
    """Many printers, plugs and TVs say what they are in their web page title."""
    scheme = "https" if port == 443 else "http"
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        r = urllib.request.urlopen("{}://{}:{}/".format(scheme, ip, port), timeout=3, context=ctx)
        body = r.read(30000).decode("utf-8", "replace")
    except Exception:
        return ""
    m = re.search(r"<title[^>]*>([^<]{2,80})</title>", body, re.I)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    return "" if (not title or WEB_JUNK.search(title)) else title


def knock(ips):
    """Returns {ip: [(rank, name, model, via, service)]} from open ports and web page titles."""
    jobs = [(ip, port) for ip in ips for port in PORTS]
    found = {}
    if not jobs:
        return found
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(lambda j: (j, port_open(*j)), jobs))
    open_ports = {}
    for (ip, port), ok in results:
        if ok:
            open_ports.setdefault(ip, []).append(port)
            found.setdefault(ip, []).append((99, "", "", "ports", "port:{}".format(port)))
    web = [(ip, p) for ip, ports in open_ports.items() for p in ports if p in (80, 8080, 443, 5000)]
    if web:
        with ThreadPoolExecutor(max_workers=8) as pool:
            titles = list(pool.map(lambda j: (j[0], web_title(*j)), web))
        for ip, title in titles:
            if title:
                found.setdefault(ip, []).append((RANK["service"], title, "", "its web page", ""))
    return found


# ---------- putting it together ----------

APPLE = [("AudioAccessory1", "HomePod"), ("AudioAccessory5", "HomePod mini"), ("AudioAccessory6", "HomePod"),
         ("AudioAccessory", "HomePod"), ("AppleTV", "Apple TV"), ("MacBookAir", "MacBook Air"),
         ("MacBookPro", "MacBook Pro"), ("Macmini", "Mac mini"), ("iMac", "iMac"), ("iPhone", "iPhone"),
         ("iPad", "iPad"), ("Watch", "Apple Watch")]


def nice_model(model):
    if "eureka" in model.lower():
        return "Chromecast"
    for prefix, nice in APPLE:
        if model.startswith(prefix) and re.match(r"^[A-Za-z]+\d+,\d+$", model):
            return "Apple " + nice if not nice.startswith(("Apple", "i", "Mac", "HomePod")) else nice
    return model


def tidy(name):
    name = re.sub(r"\.local$", "", name or "").strip()
    return name


GENERIC = re.compile(r"(?i)^(spotify ?connect|google-?home|chromecast|android|amazon|amzn|esp|"
                     r"localhost|unknown|device|wlan\d*|eth\d*|linux|none|spzc|smart ?hub|espressif)([\s_#-].*)?$|"
                     r"#\d+$|^[0-9a-f]{8,}$|^[0-9a-f]{8}-[0-9a-f]{4}-|^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$|"
                     r"[0-9a-f]{12}|redirect|opening\.*$|^loading")


def is_generic(name):
    """Names like 'SpotifyConnect #4' or 'a1b2c3d4e5f6' tell you nothing."""
    return bool(GENERIC.search(name or ""))


def discover(ips):
    """Ask everything we can. Returns {ip: {"name", "model", "via"}} for the IPs we got answers from."""
    ips = [ip for ip in ips if ip]
    results = {}
    for job in (lambda: mdns(ips), ssdp, lambda: netbios(ips), lambda: knock(ips)):
        try:
            for ip, items in job().items():
                results.setdefault(ip, []).extend(items)
        except Exception:
            log.exception("A discovery step failed, carrying on")

    out = {}
    for ip, items in results.items():
        services = sorted(set(it[4] for it in items if len(it) > 4 and it[4]))
        model = next((it[2] for it in items if it[2]), "")
        good = sorted([it for it in items if tidy(it[1]) and not is_generic(tidy(it[1]))], key=lambda x: x[0])
        best = good[0] if good else None
        if not best and not model and not services:
            continue
        out[ip] = {"name": tidy(best[1]) if best else "", "model": nice_model(model),
                   "via": best[3] if best else "Bonjour", "services": services}
    return out


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    for ip, info in sorted(discover(sys.argv[1:]).items()):
        print("{:16} {:30} {:30} {:8} {}".format(ip, info["name"] or "-", info["model"] or "-", info["via"],
                                                  ", ".join(info["services"])))
