#!/usr/bin/env python3
"""Build site/index.html: inline geometry + self-hosted fonts + CSP hashes.

Run after editing site/template.html or land.json:
    python3 build.py
"""

import base64
import hashlib
import json
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(ROOT, "site")
FONTS = os.path.join(SITE, "assets", "fonts")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

CSS_URLS = [
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap",
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&display=swap",
    "https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,400&display=swap",
]


def fetch(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data if binary else data.decode("utf-8")


def localise_fonts():
    """Download the latin woff2 subsets and rewrite @font-face to local paths."""
    os.makedirs(FONTS, exist_ok=True)
    out_css, seen = [], {}
    for url in CSS_URLS:
        css = fetch(url)
        for block in re.findall(r"@font-face\s*\{[^}]*\}", css):
            if "U+0000-00FF" not in block and "U+0000-00A0" not in block:
                continue  # latin subset only
            family = re.search(r"font-family:\s*'([^']+)'", block).group(1)
            weight = re.search(r"font-weight:\s*(\d+)", block)
            weight = weight.group(1) if weight else "400"
            style = re.search(r"font-style:\s*(\w+)", block)
            style = style.group(1) if style else "normal"
            src = re.search(r"url\((https://[^)]+\.woff2)\)", block)
            if not src:
                continue
            src_url = src.group(1)
            if src_url in seen:
                local = seen[src_url]
            else:
                slug = re.sub(r"[^a-z0-9]+", "-", (family + "-" + weight + "-" + style).lower()).strip("-")
                local = "assets/fonts/%s.woff2" % slug
                path = os.path.join(SITE, local)
                if not os.path.exists(path):
                    with open(path, "wb") as fh:
                        fh.write(fetch(src_url, binary=True))
                seen[src_url] = local
            rng = re.search(r"unicode-range:\s*([^;]+);", block)
            out_css.append(
                "@font-face{font-family:'%s';font-style:%s;font-weight:%s;font-display:swap;"
                "src:url(/%s) format('woff2');%s}"
                % (family, style, weight, local,
                   ("unicode-range:" + rng.group(1).strip() + ";") if rng else ""))
    return "\n".join(out_css)


def main():
    template = open(os.path.join(SITE, "template.html"), encoding="utf-8").read()
    land_path = os.path.join(ROOT, "land.json")
    if not os.path.exists(land_path):
        land_path = "/data/cache/scratch/wps/land.json"
    land = open(land_path, encoding="utf-8").read().strip()

    font_css_path = os.path.join(SITE, "fonts.css")
    if os.path.exists(font_css_path):
        font_css = open(font_css_path, encoding="utf-8").read()
    else:
        font_css = localise_fonts()
        open(font_css_path, "w", encoding="utf-8").write(font_css)

    html = template.replace("__LAND__", land).replace("/*FONT_CSS*/", font_css)

    # CSP hashes for inline scripts (styles use 'unsafe-inline' for style attributes)
    hashes = []
    for body in re.findall(r"<script>(.*?)</script>", html, flags=re.S):
        digest = hashlib.sha256(body.encode("utf-8")).digest()
        hashes.append("sha256-" + base64.b64encode(digest).decode())

    out = os.path.join(SITE, "index.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    with open(os.path.join(ROOT, "csp.json"), "w", encoding="utf-8") as fh:
        json.dump({"script": hashes, "style": []}, fh, indent=1)

    print("built %s (%.1f KB) · %d script hash(es) · %d font files"
          % (out, len(html) / 1024, len(hashes),
             len(os.listdir(FONTS)) if os.path.isdir(FONTS) else 0))


if __name__ == "__main__":
    main()
