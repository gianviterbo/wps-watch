"""Ingest layer for West Philippine Sea Watch.

Two live sources, both key-free:

  * news   - allowlisted RSS/Atom feeds (Philippine outlets, monitoring orgs)
  * sar    - Sentinel-1 scene metadata from the Copernicus Data Space STAC API

AIS (AISStream) and vessel events (Global Fishing Watch) need API keys; their
fetchers are present but return {"configured": false} until keys exist, so the
UI can say so honestly instead of inventing hulls.

Stdlib only. No user-supplied URLs are ever fetched.
"""

import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

UA = "WPSWatch/1.0 (+https://wps.gdgt.link)"
MAX_BYTES = 3 * 1024 * 1024
TIMEOUT = 15

STAC_SEARCH = "https://stac.dataspace.copernicus.eu/v1/search"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'",
        "&apos;": "'", "&nbsp;": " ", "&hellip;": "...", "&#8217;": "'",
        "&#8216;": "'", "&#8220;": '"', "&#8221;": '"', "&#8211;": "-"}


def _get(url, data=None, headers=None, timeout=TIMEOUT):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if not (200 <= r.status < 300):
            raise urllib.error.HTTPError(url, r.status, "unexpected status", r.headers, None)
        return r.read(MAX_BYTES).decode("utf-8", "replace")


def clean(text, limit=400):
    if not text:
        return ""
    for k, v in _ENT.items():
        text = text.replace(k, v)
    text = _TAG.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    return text[:limit]


def _local(tag):
    return tag.split("}")[-1].lower()


def _parse_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%a, %d %b %Y %H:%M %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:  # last resort: ISO8601 with Z / offsets
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def parse_feed(xml_text, feed):
    """Return a list of normalized items from RSS 2.0 or Atom."""
    items = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        return items

    for node in root.iter():
        name = _local(node.tag)
        if name not in ("item", "entry"):
            continue
        rec = {"source": feed["name"], "kind": feed["kind"], "tier": feed.get("tier", 1)}
        for child in node:
            cn = _local(child.tag)
            if cn == "title":
                rec["title"] = clean(child.text or "", 220)
            elif cn == "link":
                href = child.get("href") if child.get("href") else (child.text or "")
                rec["link"] = (href or "").strip()
            elif cn in ("description", "summary", "content", "encoded"):
                if not rec.get("summary"):
                    rec["summary"] = clean(child.text or "", 420)
            elif cn in ("pubdate", "published", "updated", "date"):
                if not rec.get("published"):
                    rec["published"] = _parse_date(child.text or "")
            elif cn == "source":
                rec["publisher"] = clean(child.text or "", 80)
                u = child.get("url") or ""
                if u.startswith("http"):
                    rec["publisher_url"] = u
            elif cn == "guid" and not rec.get("link"):
                rec["link"] = (child.text or "").strip()
        if rec.get("title") and rec.get("link", "").startswith("http"):
            rec.setdefault("published", None)
            rec.setdefault("summary", "")
            items.append(rec)
    return items


SOCIAL = {"facebook.com", "x.com", "twitter.com", "youtube.com", "tiktok.com",
          "reddit.com", "instagram.com", "linkedin.com", "threads.net", "t.me"}


def _norm_title(t):
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())[:80]


def _strip_suffix(title, publisher):
    """Google News appends ' - Publisher'; drop it so the rail shows the headline."""
    if not publisher or not title:
        return title
    for suf in (" - " + publisher, " – " + publisher, " | " + publisher):
        if title.lower().endswith(suf.lower()):
            return title[: -len(suf)].rstrip()
    stem = re.sub(r"\.(com|net|org|ph)$", "", publisher.split(".")[0], flags=re.I)
    for suf in (" - " + stem, " – " + stem):
        if stem and title.lower().endswith(suf.lower()):
            return title[: -len(suf)].rstrip()
    return title


def _domain(url):
    m = re.match(r"https?://([^/]+)", url or "")
    return (m.group(1) if m else "").lower().lstrip("www.")


def relevant(item, keywords):
    hay = (item.get("title", "") + " " + item.get("summary", "")).lower()
    return sum(1 for k in keywords if k in hay)


def fetch_news(cfg):
    """Fetch every allowlisted feed. Returns (items, feed_status)."""
    keywords = [k.lower() for k in cfg["keywords"]]
    deny = [d.lower() for d in cfg.get("deny_domains", [])]
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["news"]["max_age_days"])
    out, status, seen = [], [], set()

    for feed in cfg["feeds"]:
        st = {"name": feed["name"], "ok": False, "items": 0}
        try:
            xml_text = _get(feed["url"], headers={"Accept": "application/rss+xml, application/xml, text/xml"})
            got = parse_feed(xml_text, feed)
            kept = 0
            for it in got:
                doms = {_domain(it["link"]), _domain(it.get("publisher_url", ""))}
                if any(dm == d or dm.endswith("." + d) for dm in doms if dm for d in deny):
                    continue
                if it.get("published") and it["published"] < cutoff:
                    continue
                score = relevant(it, keywords)
                if score < cfg["news"].get("min_relevance", 1):
                    continue
                if any(dm in SOCIAL for dm in doms if dm):
                    continue
                it["title"] = _strip_suffix(it["title"], it.get("publisher", ""))
                key = _norm_title(it["title"]) or it["link"].split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                it["relevance"] = score
                it["id"] = key
                out.append(it)
                kept += 1
            st.update(ok=True, items=kept)
        except Exception as exc:
            st["error"] = type(exc).__name__
        status.append(st)

    out.sort(key=lambda i: i.get("published") or datetime(1970, 1, 1, tzinfo=timezone.utc), reverse=True)
    cap = cfg["news"].get("max_items", 80)
    return out[:cap], status


def fetch_sar(cfg, days=7, limit=24):
    """Recent Sentinel-1 GRD scenes over the WPS box, newest first."""
    bbox = cfg["bbox"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    body = json.dumps({
        "collections": ["sentinel-1-grd"],
        "bbox": bbox,
        "limit": limit,
        "datetime": start.strftime("%Y-%m-%dT%H:%M:%SZ") + "/" + end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }).encode()
    raw = _get(STAC_SEARCH, data=body, headers={"Content-Type": "application/json"}, timeout=25)
    data = json.loads(raw)
    scenes = []
    for f in data.get("features", []):
        props = f.get("properties", {}) or {}
        geom = f.get("geometry") or {}
        poly = geom.get("coordinates") or []
        ring = poly[0] if poly and isinstance(poly[0], list) else []
        scenes.append({
            "id": f.get("id"),
            "datetime": props.get("datetime"),
            "platform": props.get("platform"),
            "orbit": props.get("sat:orbit_state"),
            "rel_orbit": props.get("sat:relative_orbit"),
            "mode": props.get("sar:instrument_mode"),
            "ring": [[round(p[0], 4), round(p[1], 4)] for p in ring if len(p) >= 2][:40],
        })
    scenes = [s for s in scenes if s["ring"]]
    scenes.sort(key=lambda s: s.get("datetime") or "", reverse=True)
    last = scenes[0]["datetime"] if scenes else None
    return {"configured": True, "last_pass": last, "count": len(scenes), "scenes": scenes[:16]}


def fetch_vessels(cfg, env):
    """AISStream live positions. Needs AISSTREAM_KEY; returns not-configured otherwise."""
    key = env.get("AISSTREAM_KEY")
    if not key:
        return {"configured": False,
                "note": "AIS feed key not configured. Vessel layer idle.",
                "vessels": []}
    return {"configured": False,
            "note": "AIS WebSocket worker not started.",
            "vessels": []}


def fetch_events(cfg, env, days=7):
    """Global Fishing Watch: AIS-gap and encounter events inside the WPS box.

    Gaps are the useful signal here, but the raw feed includes obvious AIS junk
    (impossible speeds, multi-thousand-km teleports), so entries are filtered to
    plausible dark periods before they reach the map.
    """
    token = env.get("GFW_TOKEN") or env.get("GFW_API_TOKEN")
    if not token:
        return {"configured": False, "note": "Event feed token not configured.", "gaps": [], "encounters": []}

    lon0, lat0, lon1, lat1 = cfg["bbox"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    geom = {"type": "Polygon",
            "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}
    url = "https://gateway.api.globalfishingwatch.org/v3/events?limit=100&offset=0"
    out = {"configured": True, "gaps": [], "encounters": [], "note": None, "error": None,
           "window": "%s..%s" % (start.date(), end.date())}

    def query(dataset):
        body = json.dumps({"datasets": [dataset],
                           "startDate": start.strftime("%Y-%m-%d"),
                           "endDate": end.strftime("%Y-%m-%d"),
                           "geometry": geom}).encode()
        raw = _get(url, data=body, timeout=45,
                   headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        return json.loads(raw)

    inside = lambda p: p and lon0 <= p.get("lon", -999) <= lon1 and lat0 <= p.get("lat", 999) <= lat1

    try:
        gaps = query("public-global-gaps-events:latest")
        for e in gaps.get("entries", []):
            g, p = e.get("gap") or {}, e.get("position") or {}
            if not inside(p):
                continue
            try:
                km = float(g.get("impliedSpeedKnots") or 0)
                hours = float(g.get("durationHours") or 0)
            except (TypeError, ValueError):
                continue
            if km > 30 or hours < 4:        # AIS junk or a blip, not a dark period
                continue
            v = e.get("vessel") or {}
            out["gaps"].append({
                "id": e.get("id"), "start": e.get("start"), "end": e.get("end"),
                "hours": round(hours, 1), "pos": [p["lat"], p["lon"]],
                "vessel": (v.get("name") or "").strip() or "unnamed hull",
                "flag": v.get("flag"), "ssvid": v.get("ssvid"),
                "off": g.get("offPosition"), "on": g.get("onPosition"),
                "intentional": bool(g.get("intentionalDisabling")),
            })
    except Exception as exc:
        out["error"] = "gaps: %s" % type(exc).__name__

    try:
        enc = query("public-global-encounters-events:latest")
        for e in enc.get("entries", []):
            p = e.get("position") or {}
            if not inside(p):
                continue
            v = e.get("vessel") or {}
            other = (e.get("encounter") or {}).get("vessel") or {}
            out["encounters"].append({
                "id": e.get("id"), "start": e.get("start"), "pos": [p["lat"], p["lon"]],
                "vessel": (v.get("name") or "").strip() or "unnamed hull",
                "flag": v.get("flag"), "other": (other.get("name") or "").strip(),
                "other_flag": other.get("flag"), "hours": (e.get("encounter") or {}).get("medianSpeedKnots"),
            })
    except Exception as exc:
        out["error"] = (out["error"] + " | " if out["error"] else "") + "encounters: %s" % type(exc).__name__

    out["gaps"].sort(key=lambda g: g.get("start") or "", reverse=True)
    out["encounters"].sort(key=lambda g: g.get("start") or "", reverse=True)
    out["gaps"] = out["gaps"][:40]
    out["encounters"] = out["encounters"][:15]
    out["count"] = len(out["gaps"]) + len(out["encounters"])
    return out
