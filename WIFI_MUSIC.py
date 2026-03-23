#!/usr/bin/env python3
"""
Captive Portal Music Server — Educational Use Only

Updated fix for Android "Limited connection":
- No special 204 response → let detection requests fail naturally
- This usually triggers "Sign in to Wi-Fi network" notification
"""

import os
import sys
import time
import signal
import argparse
import threading
import subprocess
import http.server
import urllib.parse
from pathlib import Path

# Colors
R = "\033[91m"
G = "\033[92m"
Y = "\033[93m"
C = "\033[96m"
B = "\033[1m"
X = "\033[0m"

def info(m):  print(f"{C}[*]{X} {m}")
def ok(m):    print(f"{G}[✓]{X} {m}")
def warn(m):  print(f"{Y}[!]{X} {m}")
def err(m):   print(f"{R}[✗]{X} {m}")
def step(n, t):
    print(f"\n{B}{C}{'═'*52}\n  STEP {n}  —  {t}\n{'═'*52}{X}\n")

HOTSPOT_CON  = "captive-hotspot"
GATEWAY_IP   = "10.0.0.1"
HTTP_PORT    = 80
MUSIC_PATH   = None
dnsmasq_proc = None
hostapd_proc = None

MIME_TYPES = {
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".aac":  "audio/aac",
    ".ogg":  "audio/ogg",
    ".wav":  "audio/wav",
    ".flac": "audio/flac",
}

# ── Dependencies ────────────────────────────────────────────────
def install_deps():
    step(1, "Installing Dependencies")
    pkgs = ["dnsmasq", "iptables", "network-manager", "iw", "hostapd"]
    missing = []
    for p in pkgs:
        if subprocess.run(["dpkg", "-s", p], capture_output=True, text=True).returncode != 0:
            missing.append(p)
    if not missing:
        ok("Dependencies already installed.")
        return
    warn(f"Installing: {', '.join(missing)}")
    subprocess.run(["apt", "update", "-qq"], check=True)
    subprocess.run(["apt", "install", "-y", "-qq"] + missing, check=True)
    ok("Dependencies installed.")

# ── Interface selection ─────────────────────────────────────────
def get_iface_info():
    r = subprocess.run(["iw", "dev"], capture_output=True, text=True)
    ifaces = {}
    current = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Interface"):
            current = line.split()[1]
            ifaces[current] = {"mode": "unknown", "skip": False}
        elif line.startswith("type") and current:
            ifaces[current]["mode"] = line.split()[1]
        if current and any(s in current for s in ["wfphshr", "mon", "uap", "ap0"]):
            ifaces[current]["skip"] = True
    return ifaces

def pick_ap_iface(preferred=None):
    ifaces = get_iface_info()
    info("Detected wireless interfaces:")
    for name, d in ifaces.items():
        flag = " ← skip" if d["skip"] else ""
        print(f"    {B}{name}{X}  mode={d['mode']}{flag}")

    if preferred and preferred in ifaces:
        ok(f"Using specified: {preferred}")
        return preferred

    for name, d in ifaces.items():
        if not d["skip"] and d["mode"] == "managed":
            ok(f"Selected managed: {name}")
            return name

    for name, d in ifaces.items():
        if not d["skip"]:
            ok(f"Selected: {name}")
            return name

    warn("Fallback to wlan0")
    return "wlan0"

# ── Hotspot (hostapd) ───────────────────────────────────────────
HOSTAPD_CONF = "/tmp/captive_hostapd.conf"

def setup_hotspot_hostapd(iface, ssid, password):
    global hostapd_proc
    info(f"Setting up AP on {B}{iface}{X}")

    subprocess.run(["nmcli", "device", "disconnect", iface], capture_output=True)
    subprocess.run(["nmcli", "device", "set", iface, "managed", "no"], capture_output=True)
    time.sleep(1.2)

    subprocess.run(["ip", "link", "set", iface, "up"], capture_output=True)
    subprocess.run(["ip", "addr", "flush", "dev", iface], capture_output=True)
    subprocess.run(["ip", "addr", "add", f"{GATEWAY_IP}/24", "dev", iface], check=True, capture_output=True)

    open_net = not password
    conf = f"""interface={iface}
driver=nl80211
ssid={ssid}
hw_mode=g
channel=6
macaddr_acl=0
ignore_broadcast_ssid=0
"""

    if not open_net:
        conf += f"""auth_algs=1
wpa=2
wpa_passphrase={password}
wpa_key_mgmt=WPA-PSK
wpa_pairwise=CCMP
rsn_pairwise=CCMP
"""

    with open(HOSTAPD_CONF, "w") as f:
        f.write(conf)

    hostapd_proc = subprocess.Popen(
        ["hostapd", HOSTAPD_CONF],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    time.sleep(3.5)

    if hostapd_proc.poll() is not None:
        _, err = hostapd_proc.communicate()
        raise RuntimeError(f"hostapd failed:\n{err.decode().strip()}")

    ok(f"hostapd running → SSID: {B}{ssid}{X}")
    return GATEWAY_IP

def setup_hotspot(iface, ssid, password):
    step(2, "Setting Up Hotspot")
    return setup_hotspot_hostapd(iface, ssid, password)

# ── DNS + DHCP hijack ───────────────────────────────────────────
DNSMASQ_CONF = "/tmp/captive_dnsmasq.conf"

def start_dns_hijack(iface, gw):
    step(3, "Starting DHCP & DNS Hijack")
    global dnsmasq_proc

    subprocess.run(["systemctl", "stop", "dnsmasq"], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", "dnsmasq"], capture_output=True)
    time.sleep(1)

    subnet = gw.rsplit(".", 1)[0]
    with open(DNSMASQ_CONF, "w") as f:
        f.write(f"""interface={iface}
bind-interfaces
listen-address={gw}
dhcp-range={subnet}.50,{subnet}.150,255.255.255.0,12h
dhcp-option=3,{gw}
dhcp-option=6,{gw}
no-resolv
address=/#/{gw}
""")

    dnsmasq_proc = subprocess.Popen(
        ["dnsmasq", "--conf-file=" + DNSMASQ_CONF, "--no-daemon"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(1.5)

    subprocess.run(["iptables", "-t", "nat", "-F"], capture_output=True)
    subprocess.run([
        "iptables", "-t", "nat", "-A", "PREROUTING",
        "-i", iface, "-p", "udp", "--dport", "53",
        "-j", "DNAT", "--to-destination", f"{gw}:53"
    ], capture_output=True)
    subprocess.run([
        "iptables", "-t", "nat", "-A", "PREROUTING",
        "-i", iface, "-p", "tcp", "--dport", "80",
        "-j", "DNAT", "--to-destination", f"{gw}:{HTTP_PORT}"
    ], capture_output=True)

    subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], capture_output=True)
    ok(f"DHCP range: {subnet}.50 – {subnet}.150")
    ok("DNS redirected to gateway")

# ── HTTP Server ─────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Music Portal</title>
  <style>
    * { margin:0; padding:0; box-sizing:border-box; }
    html, body {
      width:100%; height:100%;
      background: url('/image.jpg') center center / cover no-repeat fixed;
      color:#fff;
      font-family:sans-serif;
      overflow:hidden;
    }
    #container {
      height:100vh;
      display:flex;
      flex-direction:column;
      align-items:center;
      justify-content:center;
      text-align:center;
      padding:40px;
      background:rgba(0,0,0,0.45);
    }
    h1 {
      font-size:3.6rem;
      text-shadow:0 8px 24px #000;
    }
  </style>
</head>
<body>
<div id="container">
</div>

<audio id="player" src="/music" autoplay loop preload="auto" playsinline></audio>

<script>
const audio = document.getElementById('player');
audio.play().catch(e => console.log("Autoplay:", e));

['touchstart', 'click'].forEach(ev => {
  document.addEventListener(ev, () => audio.play().catch(() => {}), {once: true, passive: true});
});

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') audio.play().catch(() => {});
});
</script>
</body>
</html>"""

CAPTIVE_PATHS = {
    "/hotspot-detect.html", "/library/test/success.html",
    "/connecttest.txt", "/ncsi.txt", "/success.txt", "/redirect",
}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        info(f"HTTP {self.address_string()}  {fmt % args}")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.lower()

        # IMPORTANT: Do NOT handle /generate_204 or /gen_204 here
        # Let them timeout/fail → triggers Android sign-in prompt

        if path == "/music":
            self._serve_music()
            return

        if path == "/image.jpg":
            image_path = os.path.join(os.path.dirname(MUSIC_PATH), "image.jpg")
            if not os.path.isfile(image_path):
                self.send_error(404, "Background not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=86400")
            with open(image_path, "rb") as f:
                content = f.read()
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path in CAPTIVE_PATHS:
            body = f'<html><head><meta http-equiv="refresh" content="0;url=http://{GATEWAY_IP}/"></head></html>'.encode()
            self.send_response(302)
            self.send_header("Location", f"http://{GATEWAY_IP}/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # All other requests → show music page
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html;charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_music(self):
        if not MUSIC_PATH or not os.path.isfile(MUSIC_PATH):
            self.send_error(404, "Music not found")
            return

        mime = MIME_TYPES.get(Path(MUSIC_PATH).suffix.lower(), "audio/mpeg")
        fsize = os.path.getsize(MUSIC_PATH)
        rng = self.headers.get("Range")

        if rng:
            p = rng.replace("bytes=", "").split("-")
            start = int(p[0]) if p[0] else 0
            end = int(p[1]) if p[1] else fsize - 1
            end = min(end, fsize - 1)
            length = end - start + 1

            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(MUSIC_PATH, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk: break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(fsize))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()

            with open(MUSIC_PATH, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)

    def do_HEAD(self):
        self.do_GET()

def start_http(gw):
    step(4, "Starting Captive Portal HTTP Server")
    srv = http.server.HTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok(f"HTTP server → http://{gw}/")
    return srv

# ── Cleanup ─────────────────────────────────────────────────────
def cleanup(iface="wlan0"):
    print(f"\n{Y}[*] Shutting down...{X}")
    global hostapd_proc, dnsmasq_proc
    if hostapd_proc:    hostapd_proc.terminate()
    if dnsmasq_proc:    dnsmasq_proc.terminate()
    subprocess.run(["iptables", "-t", "nat", "-F"], capture_output=True)
    subprocess.run(["nmcli", "con", "down", HOTSPOT_CON], capture_output=True)
    subprocess.run(["nmcli", "con", "delete", HOTSPOT_CON], capture_output=True)
    subprocess.run(["nmcli", "device", "set", iface, "managed", "yes"], capture_output=True)
    subprocess.run(["systemctl", "start", "dnsmasq"], capture_output=True)
    for f in [DNSMASQ_CONF, HOSTAPD_CONF]:
        if os.path.exists(f): os.remove(f)
    ok("Cleanup finished.")

# ── Main ────────────────────────────────────────────────────────
def main():
    global MUSIC_PATH
    parser = argparse.ArgumentParser(description="Captive portal music server with background image")
    parser.add_argument("--music",      required=True, help="Path to the .mp3 / .m4a / etc file")
    parser.add_argument("--ssid",       default="FreeMusic", help="Wi-Fi SSID")
    parser.add_argument("--pass",       dest="password", default="", help="Wi-Fi password (leave empty for open)")
    parser.add_argument("--iface",      default=None, help="Wireless interface (e.g. wlan0, wlan1)")
    parser.add_argument("--no-hotspot", action="store_true", help="Skip creating AP")
    args = parser.parse_args()

    if os.geteuid() != 0:
        err("This script must be run with sudo")
        sys.exit(1)

    if not os.path.isfile(args.music):
        err(f"Audio file not found: {args.music}")
        sys.exit(1)

    MUSIC_PATH = os.path.abspath(args.music)
    size_mb = os.path.getsize(MUSIC_PATH) / 1024 / 1024

    print(f"\n{B}{C}╔════════════════════════════════════════════╗")
    print(  "║      CAPTIVE PORTAL MUSIC SERVER           ║")
    print(  "║         Educational / Testing Only         ║")
    print(f"╚════════════════════════════════════════════╝{X}\n")

    info(f"Music file : {B}{args.music}{X}  ({size_mb:.1f} MB)")
    info(f"SSID       : {B}{args.ssid}{X}")
    info(f"Password   : {B}{args.password or '(open network)'}{X}")
    info(f"Background : image.jpg (must be in same folder as music file)")

    iface = pick_ap_iface(args.iface)

    try:
        install_deps()

        if not args.no_hotspot:
            gw = setup_hotspot(iface, args.ssid, args.password)
        else:
            gw = GATEWAY_IP
            warn("--no-hotspot used → assuming interface already has IP " + gw)

        start_dns_hijack(iface, gw)
        start_http(gw)

        print(f"""
{B}{G}╔══════════════════════════════════════════════════════════╗
║               MUSIC PORTAL IS RUNNING                    ║
╠══════════════════════════════════════════════════════════╣
║  Interface : {B}{iface:<42}{G}║
║  SSID      : {B}{args.ssid:<42}{G}║
║  Password  : {B}{(args.password or '(open)'):<42}{G}║
║  Gateway   : {B}{gw:<42}{G}║
╠══════════════════════════════════════════════════════════╣
║  • Full-screen background (image.jpg)                    ║
║  • Music auto-start attempt                              ║
║  • Android sign-in prompt fix                            ║
╚══════════════════════════════════════════════════════════╝{X}

{Y}Press Ctrl+C to stop{X}
""")

        seen = set()
        def shutdown(sig, frame):
            cleanup(iface)
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        while True:
            time.sleep(4)
            leases = "/var/lib/misc/dnsmasq.leases"
            if os.path.exists(leases):
                with open(leases) as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) >= 4 and parts[1] not in seen:
                            seen.add(parts[1])
                            ok(f"Device connected → IP={B}{parts[2]}{X}  MAC={parts[1]}")

    except Exception as e:
        err(str(e))
        import traceback
        traceback.print_exc()
        cleanup(iface)
        sys.exit(1)

if __name__ == "__main__":
    main()
