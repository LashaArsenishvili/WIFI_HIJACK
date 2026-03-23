#!/usr/bin/env python3
"""
Captive Portal Video Server — Educational Use Only
Usage:
    sudo python3 captive_video.py --video testvideo.mp4
    sudo python3 captive_video.py --video testvideo.mp4 --ssid "FreeWifi" --pass "12345678"
    sudo python3 captive_video.py --video testvideo.mp4 --iface wlan0
"""

import os, sys, time, signal, argparse
import threading, subprocess, http.server, urllib.parse
from pathlib import Path

R="\033[91m"; G="\033[92m"; Y="\033[93m"
C="\033[96m"; B="\033[1m";  X="\033[0m"

def info(m): print(f"{C}[*]{X} {m}")
def ok(m):   print(f"{G}[✓]{X} {m}")
def warn(m): print(f"{Y}[!]{X} {m}")
def err(m):  print(f"{R}[✗]{X} {m}")
def step(n,t): print(f"\n{B}{C}{'═'*52}\n  STEP {n}  —  {t}\n{'═'*52}{X}\n")

HOTSPOT_CON  = "captive-hotspot"
GATEWAY_IP   = "10.0.0.1"
HTTP_PORT    = 80
VIDEO_PATH   = None
dnsmasq_proc = None

MIME_TYPES = {
    ".mp4":"video/mp4", ".webm":"video/webm",
    ".ogg":"video/ogg", ".mkv":"video/x-matroska",
    ".avi":"video/x-msvideo", ".mov":"video/quicktime",
}

# ── Step 1: Dependencies ──────────────────────────────────
def install_deps():
    step(1, "Installing Dependencies")
    pkgs = ["dnsmasq","iptables","network-manager","iw","hostapd"]
    missing = [p for p in pkgs if
               "install ok installed" not in
               subprocess.run(["dpkg","-s",p],capture_output=True,text=True).stdout]
    if not missing:
        ok("All dependencies already installed."); return
    warn(f"Installing: {', '.join(missing)}")
    subprocess.run(["apt","update","-qq"], check=True)
    subprocess.run(["apt","install","-y","-qq"]+missing, check=True)
    ok("Done.")

# ── Interface selection ───────────────────────────────────
def get_iface_info():
    r = subprocess.run(["iw","dev"], capture_output=True, text=True)
    ifaces = {}
    current = None
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("Interface"):
            current = line.split()[1]
            ifaces[current] = {"mode": "unknown", "skip": False}
        elif line.startswith("type") and current:
            ifaces[current]["mode"] = line.split()[1]
        if current and any(s in current for s in ["wfphshr","mon","uap","ap0"]):
            ifaces[current]["skip"] = True
    return ifaces

def pick_ap_iface(preferred=None):
    ifaces = get_iface_info()
    info("Detected interfaces:")
    for name, d in ifaces.items():
        flag = " ← skip" if d["skip"] else ""
        print(f"    {B}{name}{X}  mode={d['mode']}{flag}")

    if preferred and preferred in ifaces:
        ok(f"Using specified interface: {B}{preferred}{X}")
        return preferred

    for name, d in ifaces.items():
        if not d["skip"] and d["mode"] == "managed":
            ok(f"Selected: {B}{name}{X} (managed mode)")
            return name

    for name, d in ifaces.items():
        if not d["skip"]:
            ok(f"Selected: {B}{name}{X}")
            return name

    warn("No good interface → defaulting to wlan0")
    return "wlan0"

# ── Hotspot setup ─────────────────────────────────────────
HOSTAPD_CONF = "/tmp/captive_hostapd.conf"
hostapd_proc = None

def setup_hotspot_hostapd(iface, ssid, password):
    global hostapd_proc
    info(f"Setting up AP with hostapd on {B}{iface}{X}")

    subprocess.run(["nmcli","device","disconnect", iface], capture_output=True)
    subprocess.run(["nmcli","device","set", iface, "managed","no"], capture_output=True)
    time.sleep(1)

    subprocess.run(["ip","link","set", iface,"up"], capture_output=True)
    subprocess.run(["ip","addr","flush","dev", iface], capture_output=True)
    subprocess.run(["ip","addr","add",f"{GATEWAY_IP}/24","dev",iface], check=True, capture_output=True)

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

    with open(HOSTAPD_CONF,"w") as f:
        f.write(conf)

    hostapd_proc = subprocess.Popen(
        ["hostapd", HOSTAPD_CONF],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(3)

    if hostapd_proc.poll() is not None:
        _, err = hostapd_proc.communicate()
        raise RuntimeError(f"hostapd failed:\n{err.decode()}")

    ok(f"hostapd running  SSID={B}{ssid}{X}  IP={B}{GATEWAY_IP}{X}")
    return GATEWAY_IP

def setup_hotspot(iface, ssid, password):
    step(2, "Setting Up Hotspot")
    try:
        return setup_hotspot_hostapd(iface, ssid, password)
    except Exception as e:
        warn(f"hostapd failed: {e}")
        warn("Trying fallback method not implemented in this version")
        raise

# ── DNS + DHCP hijack ─────────────────────────────────────
DNSMASQ_CONF = "/tmp/captive_dnsmasq.conf"

def start_dns_hijack(iface, gw):
    step(3, "DHCP + DNS Hijack")
    global dnsmasq_proc
    subprocess.run(["systemctl","stop","dnsmasq"], capture_output=True)
    subprocess.run(["pkill","-9","-f","dnsmasq"], capture_output=True)
    time.sleep(1)

    subnet = gw.rsplit(".",1)[0]
    with open(DNSMASQ_CONF,"w") as f:
        f.write(f"""
interface={iface}
bind-interfaces
listen-address={gw}
dhcp-range={subnet}.10,{subnet}.100,255.255.255.0,12h
dhcp-option=3,{gw}
dhcp-option=6,{gw}
no-resolv
address=/#/{gw}
""")

    dnsmasq_proc = subprocess.Popen(
        ["dnsmasq","--conf-file="+DNSMASQ_CONF,"--no-daemon"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    time.sleep(1)

    subprocess.run(["iptables","-t","nat","-F"], capture_output=True)
    subprocess.run([
        "iptables","-t","nat","-A","PREROUTING",
        "-i",iface,"-p","udp","--dport","53",
        "-j","DNAT","--to-destination",f"{gw}:53"
    ], capture_output=True)
    subprocess.run([
        "iptables","-t","nat","-A","PREROUTING",
        "-i",iface,"-p","tcp","--dport","80",
        "-j","DNAT","--to-destination",f"{gw}:{HTTP_PORT}"
    ], capture_output=True)
    subprocess.run(["sysctl","-w","net.ipv4.ip_forward=1"], capture_output=True)
    ok(f"DHCP range: {subnet}.10 – {subnet}.100")
    ok("All DNS → us")

# ── HTTP Server ───────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Video</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    html,body{width:100%;height:100%;background:#000;overflow:hidden}
    video{width:100vw;height:100vh;object-fit:contain;display:block}
    #msg {
      position: fixed;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #fff;
      font-family: sans-serif;
      font-size: 1.2rem;
      background: rgba(0,0,0,0.65);
      pointer-events: none;
      z-index: 10;
      opacity: 0;
      transition: opacity 1s;
      text-align: center;
      padding: 20px;
    }
    #soundhint {
      position: fixed;
      bottom: 30px;
      left: 50%;
      transform: translateX(-50%);
      color: #fff;
      font-size: 1rem;
      background: rgba(0,0,0,0.5);
      padding: 8px 16px;
      border-radius: 20px;
      z-index: 9;
      opacity: 0;
      transition: opacity 1.5s;
      pointer-events: none;
    }
  </style>
</head>
<body>

<video id="v"
       src="/video"
       autoplay
       muted
       loop
       playsinline
       webkit-playsinline
       x5-playsinline
       preload="auto"
       style="background:#111">
</video>

<div id="msg">Starting video…</div>
<div id="soundhint">Tap screen for sound 🔊</div>

<script>
const video = document.getElementById('v');
const msg   = document.getElementById('msg');
const hint  = document.getElementById('soundhint');

function tryAutoPlay() {
  video.play()
    .then(() => {
      msg.style.opacity = '0';
      setTimeout(() => { msg.style.display = 'none'; }, 1000);
      setTimeout(() => {
        if (document.documentElement.requestFullscreen) {
          document.documentElement.requestFullscreen().catch(()=>{});
        } else if (video.webkitEnterFullscreen) {
          video.webkitEnterFullscreen();
        }
      }, 800);
    })
    .catch(() => {
      msg.textContent = "Loading… tap if needed";
      msg.style.opacity = '1';
      setTimeout(tryAutoPlay, 1800);
    });
}

video.muted = true;
video.volume = 1.0;
tryAutoPlay();

function enableSound(e) {
  if (video.muted) {
    video.muted = false;
    if (video.paused || video.ended) {
      video.play().catch(()=>{});
    }
    msg.style.opacity = '0';
    setTimeout(() => { msg.style.display = 'none'; }, 800);
    hint.style.opacity = '0';
    setTimeout(() => { hint.style.display = 'none'; }, 1500);
  }
}

document.addEventListener('touchstart', enableSound, {once: true, passive: true});
document.addEventListener('click', enableSound, {once: true});

video.addEventListener('loadedmetadata', () => {
  setTimeout(tryAutoPlay, 400);
});

setTimeout(() => {
  if (video.muted && video.currentTime > 2) {
    hint.style.opacity = '0.9';
  }
}, 6500);
</script>

</body>
</html>"""

CAPTIVE_PATHS = {
    "/generate_204", "/gen_204", "/mobile/status.php",
    "/hotspot-detect.html", "/library/test/success.html",
    "/connecttest.txt", "/ncsi.txt", "/success.txt", "/redirect",
}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        info(f"HTTP {self.address_string()}  {fmt%args}")

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/video":
            self._serve_video()
            return
        if path in CAPTIVE_PATHS:
            body = f'<html><head><meta http-equiv="refresh" content="0;url=http://{GATEWAY_IP}/"></head></html>'.encode()
            self.send_response(302)
            self.send_header("Location", f"http://{GATEWAY_IP}/")
            self.send_header("Content-Type","text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type","text/html;charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control","no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_video(self):
        if not VIDEO_PATH or not os.path.isfile(VIDEO_PATH):
            self.send_error(404)
            return
        mime = MIME_TYPES.get(Path(VIDEO_PATH).suffix.lower(), "video/mp4")
        fsize = os.path.getsize(VIDEO_PATH)
        rng = self.headers.get("Range")

        if rng:
            p = rng.replace("bytes=","").split("-")
            s = int(p[0]) if p[0] else 0
            e = int(p[1]) if p[1] else fsize-1
            e = min(e, fsize-1)
            ln = e - s + 1
            self.send_response(206)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Range", f"bytes {s}-{e}/{fsize}")
            self.send_header("Content-Length", str(ln))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(VIDEO_PATH, "rb") as f:
                f.seek(s)
                rem = ln
                while rem > 0:
                    chunk = f.read(min(65536, rem))
                    if not chunk: break
                    self.wfile.write(chunk)
                    rem -= len(chunk)
        else:
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(fsize))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with open(VIDEO_PATH, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    self.wfile.write(chunk)

    def do_HEAD(self):
        self.do_GET()

def start_http(gw):
    step(4, "Starting HTTP Captive Portal")
    srv = http.server.HTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ok(f"HTTP server → http://{gw}/")
    return srv

# ── Cleanup ───────────────────────────────────────────────
def cleanup(iface="wlan0"):
    print(f"\n{Y}[*] Shutting down...{X}")
    global hostapd_proc, dnsmasq_proc
    if hostapd_proc:    hostapd_proc.terminate()
    if dnsmasq_proc:    dnsmasq_proc.terminate()
    subprocess.run(["iptables","-t","nat","-F"], capture_output=True)
    subprocess.run(["nmcli","con","down", HOTSPOT_CON], capture_output=True)
    subprocess.run(["nmcli","con","delete", HOTSPOT_CON], capture_output=True)
    subprocess.run(["nmcli","device","set", iface,"managed","yes"], capture_output=True)
    subprocess.run(["systemctl","start","dnsmasq"], capture_output=True)
    for f in [DNSMASQ_CONF, HOSTAPD_CONF]:
        if os.path.exists(f): os.remove(f)
    ok("Cleanup done.")

# ── Main ──────────────────────────────────────────────────
def main():
    global VIDEO_PATH
    parser = argparse.ArgumentParser()
    parser.add_argument("--video",      required=True)
    parser.add_argument("--ssid",       default="FreeWifi")
    parser.add_argument("--pass",       dest="password", default="")
    parser.add_argument("--iface",      default=None)
    parser.add_argument("--no-hotspot", action="store_true")
    args = parser.parse_args()

    if os.geteuid() != 0:
        err("Please run with sudo")
        sys.exit(1)

    if not os.path.isfile(args.video):
        err(f"Video file not found: {args.video}")
        sys.exit(1)

    VIDEO_PATH = os.path.abspath(args.video)
    size_mb = os.path.getsize(VIDEO_PATH) / 1024 / 1024

    print(f"\n{B}{C}  ╔══════════════════════════════════════════╗")
    print(  "  ║   CAPTIVE PORTAL VIDEO SERVER            ║")
    print(  "  ║   Educational / Testing Use Only         ║")
    print(f"  ╚══════════════════════════════════════════╝{X}\n")

    info(f"Video:  {B}{args.video}{X}  ({size_mb:.1f} MB)")
    info(f"SSID:   {B}{args.ssid}{X}")
    info(f"Pass:   {B}{args.password or '(open)'}{X}")

    iface = pick_ap_iface(args.iface)

    try:
        install_deps()

        if not args.no_hotspot:
            gw = setup_hotspot(iface, args.ssid, args.password)
        else:
            gw = GATEWAY_IP
            warn("--no-hotspot used → no AP created")

        start_dns_hijack(iface, gw)
        start_http(gw)

        print(f"""
{B}{G}╔══════════════════════════════════════════════════════╗
║           ✅  PORTAL IS RUNNING                      ║
╠══════════════════════════════════════════════════════╣
║  Interface : {B}{iface:<38}{G}║
║  SSID      : {B}{args.ssid:<38}{G}║
║  Password  : {B}{(args.password or '(open)'):<38}{G}║
║  Address   : {B}{gw:<38}{G}║
╠══════════════════════════════════════════════════════╣
║  • Video starts muted automatically                 ║
║  • Tap screen once → sound turns on                 ║
║  • Fullscreen usually activates automatically       ║
╚══════════════════════════════════════════════════════╝{X}
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
