#!/usr/bin/env python3
"""QA for the live WPS Watch build (served over http, so /api works).

Checks: console/page errors, self-hosted fonts, horizontal overflow, map label
collisions, expected label count, live-data rendering (news rows, SAR
footprints), security headers on the document, and pixel samples at known
coordinates to prove the projection.

    LD_LIBRARY_PATH=/data/chromium-libs/all python3 qa.py [url]
"""
import json
import math
import os
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8931/"
OUT = os.environ.get("QA_OUT", "/tmp/wps-qa")   # keep screenshots out of the repo
os.makedirs(OUT, exist_ok=True)
SIZES = [(1680, 1000), (1440, 900), (1280, 800)]
VIEW = (117.4, 13.3, 23.2)
SAMPLES = [("Luzon land", 16.0, 121.0), ("open SCS", 15.0, 115.0), ("Palawan", 9.2, 118.2)]

PROBE = """(sel) => {
  const map = document.querySelector(sel);
  const box = map.getBoundingClientRect();
  const texts = [...map.querySelectorAll('text')].map(t => {
    const b = t.getBoundingClientRect();
    return {s: t.textContent.slice(0, 40), l: b.left, t: b.top, r: b.right, b: b.bottom};
  });
  const hits = [];
  for (let i = 0; i < texts.length; i++) for (let j = i + 1; j < texts.length; j++) {
    const a = texts[i], c = texts[j];
    const ox = Math.min(a.r, c.r) - Math.max(a.l, c.l);
    const oy = Math.min(a.b, c.b) - Math.max(a.t, c.t);
    if (ox > 3 && oy > 3) hits.push([a.s, c.s]);
  }
  const over = [...document.querySelectorAll('header *, .rail *, .status *')]
    .filter(e => { const b = e.getBoundingClientRect(); return b.right > innerWidth + 1 || b.left < -1; })
    .map(e => (e.tagName + '.' + (e.className || '')).slice(0, 40));
  const fonts = [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family);
  return {
    fonts: [...new Set(fonts)], texts, hits, over,
    labels: texts.filter(t => !/\\u00b0/.test(t.s)).map(t => t.s),
    scrollW: document.body.scrollWidth, vw: innerWidth,
    mapBox: {x: box.x, y: box.y, w: box.width, h: box.height},
    news: document.querySelectorAll('#feed .item').length,
    sar: document.querySelectorAll('#map path.sar').length,
    vessels: document.querySelectorAll('.vessel').length,
    feedCount: (document.getElementById('feedCount') || {}).textContent,
    filterText: (document.getElementById('filtertext') || {}).textContent,
    livePill: (document.getElementById('livetext') || {}).textContent,
    status: [...document.querySelectorAll('.status .s')].map(e => e.textContent.trim()),
  };
}"""


def merc(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def main():
    # security headers, as the browser would see them
    req = urllib.request.Request(URL, headers={"User-Agent": "qa"})
    with urllib.request.urlopen(req, timeout=15) as r:
        hdrs = dict(r.headers)
        body = r.read().decode()
    print("headers:", {k: v[:70] for k, v in hdrs.items()
                       if k.lower() in ("server", "content-security-policy", "strict-transport-security",
                                        "x-frame-options", "x-content-type-options", "referrer-policy",
                                        "permissions-policy", "cache-control")})
    missing = [h for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options",
                           "Referrer-Policy", "Strict-Transport-Security", "Permissions-Policy")
               if h.lower() not in {k.lower() for k in hdrs}]
    fails = ["missing header " + h for h in missing]
    if "fonts.googleapis" in body or "fonts.gstatic" in body:
        fails.append("external font origin still referenced")
    print("body KB %.1f | external font refs: %s" % (len(body) / 1024, "fonts.googleapis" in body))

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        for w, h in SIZES:
            pg = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
            errs = []
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(URL, wait_until="load")
            pg.wait_for_timeout(3000)
            res = pg.evaluate(PROBE, "#map")
            print("[%dx%d] fonts=%s labels=%d collisions=%d overflow=%d errors=%d news=%s sar=%s vessels=%s"
                  % (w, h, ",".join(sorted(res["fonts"])) or "NONE", len(res["labels"]), len(res["hits"]),
                     len(res["over"]), len(errs), res["news"], res["sar"], res["vessels"]))
            print("        pill=%r filter=%r count=%r" % (res["livePill"], res["filterText"], res["feedCount"]))
            print("        status=%s" % res["status"])
            if res["hits"]:
                print("        collisions:", res["hits"][:5])
            if res["over"]:
                print("        overflow:", res["over"][:5])
            if errs:
                print("        errors:", errs[:4])
            if len(res["labels"]) < 11:
                print("        placed labels:", res["labels"])
                fails.append("labels %d < 11 at %dx%d" % (len(res["labels"]), w, h))
            if res["hits"] or res["over"] or errs or res["scrollW"] > res["vw"] + 1:
                fails.append("layout/console at %dx%d" % (w, h))
            if res["news"] < 5:
                fails.append("news rows %d < 5 at %dx%d" % (res["news"], w, h))
            if res["sar"] < 1:
                fails.append("no SAR footprints at %dx%d" % (w, h))

            shot = os.path.join(OUT, "live-%d.png" % w)
            pg.screenshot(path=shot)

            from PIL import Image
            im = Image.open(shot).convert("RGB")
            lon0, lat0, span = VIEW
            box, S = res["mapBox"], 2
            ppr = box["w"] / (span * math.pi / 180)
            for tag, lat, lon in SAMPLES:
                x = (box["x"] + box["w"] / 2 + (lon - lon0) * ppr * math.pi / 180) * S
                y = (box["y"] + box["h"] / 2 - (merc(lat) - merc(lat0)) * ppr) * S
                if not (0 <= x < im.width and 0 <= y < im.height):
                    print("        sample %s OUT OF FRAME" % tag)
                    fails.append("sample %s out of frame" % tag)
                    continue
                print("        sample %-10s %s" % (tag, im.getpixel((int(x), int(y)))))
            pg.close()
        browser.close()

    if fails:
        print("\nFAILURES:", fails)
        return 1
    print("\nOK: clean at %s" % ", ".join("%dx%d" % s for s in SIZES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
