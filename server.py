#!/usr/bin/env python3
"""West Philippine Sea Watch — origin server.

Loopback only (127.0.0.1:8931); Cloudflare Tunnel terminates TLS at the edge.
No framework, no template engine, no user-supplied URLs.

  /            static dashboard
  /assets/*    fonts and static assets (allowlisted extensions)
  /api/summary feed health + counters
  /api/news    Jev-filtered news items
  /api/sar     recent Sentinel-1 scene footprints
  /api/vessels AIS layer (idle until AISSTREAM_KEY exists)
  /api/health  liveness for the tunnel / uptime checks
"""

import json
import os
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ais
import ingest
import jev_gate

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
CONFIG = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
CSP_PATH = os.path.join(ROOT, "csp.json")
CSP = {"script": [], "style": []}
_CSP_MTIME = [0.0]


def csp():
    """CSP hashes change whenever build.py runs; read them by mtime, not once."""
    try:
        m = os.path.getmtime(CSP_PATH)
        if m != _CSP_MTIME[0]:
            with open(CSP_PATH, encoding="utf-8") as fh:
                CSP.clear()
                CSP.update(json.load(fh))
            _CSP_MTIME[0] = m
    except Exception:
        pass
    return CSP

LOG_DIR = "/data/logs/wps-watch"
os.makedirs(LOG_DIR, exist_ok=True)

ASSET_EXT = {".woff2", ".css", ".svg", ".png", ".ico", ".webp"}
RATE_LIMIT = 120          # requests per minute per IP
MAX_URL = 512

AIS_WORKER = None

STATE = {
    "news": [], "news_held": 0, "news_stats": {}, "feeds": [],
    "sar": {"configured": False, "scenes": []},
    "vessels": {"configured": False, "vessels": []},
    "events": {"configured": False, "events": []},
    "news_at": None, "sar_at": None, "warming": True,
}
LOCK = threading.Lock()
HITS = defaultdict(deque)


# ---------------------------------------------------------------- helpers
def env_file(path="/data/.env"):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def classify(item):
    t = (item.get("title") or "").lower()
    if re.search(r"coast guard|vessel|ship|boat|hull|flotilla|navy|resupply|swarm|militia|fisher", t):
        return "vessels"
    if re.search(r"agreement|treaty|talks|deal|code of conduct|sanction|diplomac|summit|policy|law|protest|statement", t):
        return "policy"
    return "wps"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


# ---------------------------------------------------------------- refresh
def refresh_news():
    items, feeds = ingest.fetch_news(CONFIG)
    kept, held, stats = jev_gate.gate(items, CONFIG, budget_s=CONFIG["news"]["jev"].get("budget_s", 90))
    for it in kept:
        it["tag"] = classify(it)
        it["published"] = iso(it.get("published"))
        it.pop("verdict", None)
        it.pop("publisher_url", None)
    with LOCK:
        STATE["news"] = kept
        STATE["news_held"] = len(held)
        STATE["news_stats"] = stats
        STATE["feeds"] = feeds
        STATE["news_at"] = datetime.now(timezone.utc)
        STATE["warming"] = False
    return len(kept)


def refresh_sar():
    try:
        sar = ingest.fetch_sar(CONFIG)
    except Exception as exc:
        sar = {"configured": False, "note": "sar fetch failed: %s" % type(exc).__name__, "scenes": []}
    with LOCK:
        STATE["sar"] = sar
        STATE["sar_at"] = datetime.now(timezone.utc)


def refresh_idle():
    env = dict(os.environ)
    env.update(env_file())
    vessels = {"configured": False, "note": "AIS worker not started.", "vessels": []}
    if AIS_WORKER is not None:
        vessels = AIS_WORKER.snapshot()
    elif env.get("AISSTREAM_KEY"):
        vessels = {"configured": False, "note": "AIS worker starting.", "vessels": []}
    else:
        vessels = {"configured": False, "note": "AIS feed key not configured.", "vessels": []}
    events = {"configured": False, "note": "Event feed token not configured.", "gaps": [], "encounters": []}
    try:
        events = ingest.fetch_events(CONFIG, env)
    except Exception as exc:
        events["error"] = type(exc).__name__
    with LOCK:
        STATE["vessels"] = vessels
        STATE["events"] = events


def refresher():
    news_ttl = CONFIG["news"].get("ttl_s", 300)
    sar_ttl = 1800
    events_ttl = 600
    last_sar = 0.0
    last_idle = 0.0
    refresh_idle()
    while True:
        try:
            refresh_news()
        except Exception as exc:
            write_log("refresh_news error: %s: %s" % (type(exc).__name__, exc))
        try:
            if time.time() - last_sar > sar_ttl:
                refresh_sar()
                last_sar = time.time()
        except Exception as exc:
            write_log("refresh_sar error: %s" % type(exc).__name__)
        try:
            if time.time() - last_idle > events_ttl:
                refresh_idle()
                last_idle = time.time()
        except Exception as exc:
            write_log("refresh_idle error: %s" % type(exc).__name__)
        time.sleep(max(30, news_ttl))


def write_log(line):
    try:
        with open(os.path.join(LOG_DIR, "server.log"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (datetime.now(timezone.utc).isoformat(timespec="seconds"), line))
    except Exception:
        pass


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "wps"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return ""

    def send_response(self, code, message=None):
        # omit the Server banner entirely
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Date", self.date_time_string())

    def log_message(self, fmt, *args):
        ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
        write_log('%s "%s"' % (ip, (fmt % args)[:160]))

    # -- security
    def security_headers(self):
        script = " ".join("'%s'" % h for h in csp().get("script", [])) or "'none'"
        policy = ("; ".join([
            "default-src 'none'",
            "script-src 'self' " + script + " https://static.cloudflareinsights.com",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "manifest-src 'self'",
            "base-uri 'none'",
            "form-action 'none'",
            "frame-ancestors 'none'",
        ]))
        self.send_header("Content-Security-Policy", policy)
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=(), usb=(), interest-cohort=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")

    def client_ip(self):
        return (self.headers.get("CF-Connecting-IP") or self.client_address[0] or "?").split(",")[0].strip()

    def rate_ok(self):
        now = time.time()
        q = HITS[self.client_ip()]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        if len(HITS) > 5000:  # bound the table
            for k in [k for k, v in HITS.items() if not v][:1000]:
                HITS.pop(k, None)
        return True

    # -- responses
    def send_json(self, obj, code=200, cache="no-store"):
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(code)
        self.security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def error(self, code, msg):
        self.send_json({"error": msg, "status": code}, code=code)

    def send_file(self, path, ctype, cache):
        try:
            data = open(path, "rb").read()
        except OSError:
            return self.error(404, "not found")
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # -- routing
    def do_GET(self):
        try:
            self.route()
        except Exception as exc:
            write_log("handler error: %s: %s" % (type(exc).__name__, exc))
            self.error(500, "internal error")

    do_HEAD = do_GET

    def do_POST(self):
        self.error(405, "method not allowed")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def route(self):
        # Cloudflare terminates TLS; a plaintext visitor arrives as http and is
        # sent to https here (zone-level Always-Use-HTTPS is off and the API token
        # cannot change zone settings).
        if (self.headers.get("X-Forwarded-Proto") or "").lower() == "http":
            host = self.headers.get("Host") or CONFIG["site"]["host"]
            self.send_response(301)
            self.send_header("Location", "https://" + host + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if len(self.path) > MAX_URL:
            return self.error(414, "uri too long")
        if not self.rate_ok():
            self.send_response(429)
            self.security_headers()
            self.send_header("Content-Length", "0")
            self.send_header("Retry-After", "30")
            self.end_headers()
            return

        url = urlparse(self.path)
        path = url.path

        if path in ("/api/health", "/healthz"):
            return self.send_json({"ok": True, "ts": iso(datetime.now(timezone.utc))})

        if path == "/api/summary":
            with LOCK:
                st = dict(
                    name=CONFIG["site"]["name"],
                    generated=iso(datetime.now(timezone.utc)),
                    news_at=iso(STATE["news_at"]),
                    sar_at=iso(STATE["sar_at"]),
                    warming=STATE["warming"],
                    news_count=len(STATE["news"]),
                    news_held=STATE["news_held"],
                    filter=STATE["news_stats"],
                    feeds=STATE["feeds"],
                    sar={"configured": STATE["sar"].get("configured", False),
                         "last_pass": STATE["sar"].get("last_pass"),
                         "count": STATE["sar"].get("count", 0),
                         "note": STATE["sar"].get("note")},
                    vessels={"configured": STATE["vessels"].get("configured", False),
                             "count": len(STATE["vessels"].get("vessels") or []),
                             "note": STATE["vessels"].get("note")},
                    events={"configured": STATE["events"].get("configured", False),
                            "count": len(STATE["events"].get("events") or []),
                            "note": STATE["events"].get("note")},
                )
            return self.send_json(st)

        if path == "/api/news":
            q = parse_qs(url.query)
            f = (q.get("filter") or ["all"])[0]
            if f not in ("all", "wps", "vessels", "policy"):
                f = "all"
            with LOCK:
                items = [i for i in STATE["news"] if f == "all" or i.get("tag") == f]
            return self.send_json({"filter": f, "count": len(items), "items": items[:60]})

        if path == "/api/sar":
            with LOCK:
                sar = dict(STATE["sar"])
            return self.send_json(sar, cache="public, max-age=300")

        if path == "/api/vessels":
            if AIS_WORKER is not None:
                snap = AIS_WORKER.snapshot()
                with LOCK:
                    STATE["vessels"] = snap
            else:
                with LOCK:
                    snap = dict(STATE["vessels"])
            return self.send_json(snap)

        if path == "/api/events":
            with LOCK:
                e = dict(STATE["events"])
            return self.send_json(e)

        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(SITE, "index.html"),
                                  "text/html; charset=utf-8", "public, max-age=60")

        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.security_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        if path.startswith("/assets/"):
            rel = path[len("/assets/"):]
            if ".." in rel or rel.startswith(".") or "\\" in rel:
                return self.error(404, "not found")
            ext = os.path.splitext(rel)[1].lower()
            if ext not in ASSET_EXT:
                return self.error(404, "not found")
            full = os.path.realpath(os.path.join(SITE, "assets", rel))
            if not full.startswith(os.path.realpath(os.path.join(SITE, "assets"))):
                return self.error(404, "not found")
            ctype = {".woff2": "font/woff2", ".css": "text/css; charset=utf-8",
                     ".svg": "image/svg+xml", ".png": "image/png",
                     ".webp": "image/webp", ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
            return self.send_file(full, ctype, "public, max-age=31536000, immutable")

        return self.error(404, "not found")


def main():
    global AIS_WORKER
    env = env_file()
    key = env.get("AISSTREAM_KEY")
    if key:
        AIS_WORKER = ais.AisWorker(key, CONFIG["bbox"])
        AIS_WORKER.start()
        write_log("AIS worker started for bbox %s" % (CONFIG["bbox"],))
    else:
        write_log("AIS worker not started: no AISSTREAM_KEY")
    port = int(CONFIG["site"].get("port", 8931))
    threading.Thread(target=refresher, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    write_log("listening on 127.0.0.1:%d" % port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
