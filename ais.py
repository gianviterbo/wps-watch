"""Live vessel layer: AISStream WebSocket -> rolling in-memory picture.

One long-lived connection (the free tier allows three per account), a bounded
vessel table, and short per-vessel position histories for the map trails.
Reconnects with backoff; every failure leaves the previous picture in place
rather than blanking the layer.
"""

import asyncio
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone

WS_URL = "wss://stream.aisstream.io/v0/stream"
MAX_VESSELS = 400
HISTORY = 10
TTL_H = 3.0

SHIP_TYPE = {30: "fishing", 31: "tug", 32: "tug", 35: "military", 36: "sailing",
             37: "pleasure craft", 50: "pilot", 51: "SAR", 52: "tug", 55: "law enforcement",
             60: "passenger", 70: "cargo", 80: "tanker", 90: "other"}


def classify(mmsi, name):
    """Map a hull to one of the four map classes we are willing to assert."""
    n = (name or "").upper().strip()
    s = str(mmsi)
    mid = int(s[:3]) if s.isdigit() and len(s) >= 3 else 0
    if "HAI JING" in n or n.startswith("CCG") or "CHINA COAST GUARD" in n:
        return "ccg"
    if mid == 548 or n.startswith("BRP ") or n.startswith("BRP"):
        return "ph"
    if mid in (412, 413):
        return "cn"          # Chinese-flagged, includes fishing fleet hulls
    return "com"


def type_label(code, fallback=""):
    if fallback:
        return fallback
    try:
        return SHIP_TYPE.get(int(code), "vessel")
    except (TypeError, ValueError):
        return "vessel"


class AisWorker(threading.Thread):
    def __init__(self, key, bbox, ttl_h=TTL_H):
        super().__init__(daemon=True)
        self.key = key
        self.bbox = bbox                       # [lon_min, lat_min, lon_max, lat_max]
        self.ttl = ttl_h * 3600
        self.vessels = {}
        self.names = {}
        self.lock = threading.Lock()
        self.status = {"connected": False, "messages": 0, "last": None,
                       "error": None, "since": datetime.now(timezone.utc).isoformat()}

    # ---- websocket loop
    def run(self):
        while True:
            try:
                asyncio.run(self._session())
            except Exception as exc:
                with self.lock:
                    self.status.update(connected=False, error="%s: %s" % (type(exc).__name__, str(exc)[:100]))
                time.sleep(15)

    async def _session(self):
        import websockets
        lon0, lat0, lon1, lat1 = self.bbox
        sub = {"APIKey": self.key,
               "BoundingBoxes": [[[lat0, lon0], [lat1, lon1]]],
               "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
        async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20,
                                      max_size=2 ** 23, close_timeout=5) as ws:
            await ws.send(json.dumps(sub))
            with self.lock:
                self.status.update(connected=True, error=None, since=datetime.now(timezone.utc).isoformat())
            async for raw in ws:
                try:
                    self.handle(json.loads(raw))
                except Exception:
                    continue

    # ---- message handling
    def handle(self, msg):
        kind = msg.get("MessageType")
        meta = msg.get("MetaData") or {}
        body = msg.get("Message") or {}
        now = time.time()

        if kind == "ShipStaticData":
            static = body.get("ShipStaticData") or {}
            mmsi = str(static.get("UserID") or meta.get("MMSI") or "")
            if mmsi:
                with self.lock:
                    self.names[mmsi] = {"name": (static.get("Name") or "").strip(),
                                        "type": static.get("Type")}
            return

        if kind != "PositionReport":
            return
        rep = body.get("PositionReport") or {}
        mmsi = str(rep.get("UserID") or meta.get("MMSI") or "")
        lat = rep.get("Latitude", meta.get("latitude"))
        lon = rep.get("Longitude", meta.get("longitude"))
        if not mmsi or lat is None or lon is None:
            return
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return
        lon0, lat0, lon1, lat1 = self.bbox
        if not (lon0 <= lon <= lon1 and lat0 <= lat <= lat1):
            return

        stat = self.names.get(mmsi) or {}
        name = (rep.get("Name") or meta.get("ShipName") or stat.get("name") or "").strip()

        with self.lock:
            v = self.vessels.get(mmsi)
            if v is None:
                if len(self.vessels) >= MAX_VESSELS:
                    # evict the oldest contact so a busy window cannot grow without bound
                    oldest = min(self.vessels.items(), key=lambda kv: kv[1]["seen"])[0]
                    self.vessels.pop(oldest, None)
                v = {"mmsi": mmsi, "trk": deque(maxlen=HISTORY)}
                self.vessels[mmsi] = v
            v.update({
                "id": name or ("MMSI " + mmsi),
                "cls": classify(mmsi, name),
                "type": type_label(stat.get("type")),
                "pos": [round(lat, 4), round(lon, 4)],
                "hdg": int(rep.get("TrueHeading") or rep.get("Cog") or 0) % 360,
                "spd": round(float(rep.get("Sog") or 0), 1),
                "seen": now,
                "nav": rep.get("NavigationalStatus"),
            })
            last = v["trk"][-1] if v["trk"] else None
            if last is None or abs(last[0] - lat) + abs(last[1] - lon) > 0.002 or now - v.get("seen_prev", 0) > 300:
                v["trk"].append([round(lat, 4), round(lon, 4)])
                v["seen_prev"] = now
            self.status["messages"] += 1
            self.status["last"] = datetime.now(timezone.utc).isoformat()

    # ---- snapshot for the API
    def snapshot(self):
        now = time.time()
        with self.lock:
            live = [v for v in self.vessels.values() if now - v["seen"] <= self.ttl]
            for mmsi in [k for k, v in self.vessels.items() if now - v["seen"] > self.ttl]:
                self.vessels.pop(mmsi, None)
            out = []
            for v in live:
                out.append({
                    "id": v["id"], "mmsi": v["mmsi"], "cls": v["cls"], "type": v["type"],
                    "pos": v["pos"], "hdg": v["hdg"], "spd": v["spd"],
                    "seen": datetime.fromtimestamp(v["seen"], timezone.utc).isoformat(),
                    "trk": list(v["trk"]),
                })
            st = dict(self.status)
        order = {"ccg": 0, "ph": 1, "cn": 2, "com": 3}
        out.sort(key=lambda v: (order.get(v["cls"], 9), -(v["spd"] or 0)))
        return {"configured": True, "count": len(out), "vessels": out,
                "feed": {"connected": st["connected"], "messages": st["messages"], "last": st["last"],
                         "error": st["error"], "since": st["since"]}}
