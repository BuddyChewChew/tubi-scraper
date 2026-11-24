#!/usr/bin/env python3
"""
Tubi Live Channels Scraper (rewrite)

Outputs: tubi_playlist.m3u, tubi_epg.xml
Deps:    pip install requests          (pysocks only if you use a socks proxy)

Usage:
    python tubi_scraper.py
    python tubi_scraper.py --proxy socks5://host:port
    python tubi_scraper.py --categories     # group-title by Tubi category
    python tubi_scraper.py --debug          # dump raw responses to ./debug/

Flow:
    1. Discover channel IDs  (live page JSON -> container APIs -> cache -> seed)
    2. Fetch EPG in small batches, trying several endpoints per batch
    3. Write M3U + XMLTV, and cache the ID list when it looks healthy
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
LIVE_PAGE_URL = "https://tubitv.com/live"

# Tried in order; first one that returns rows wins (per batch).
EPG_ENDPOINTS = [
    "https://tubitv.com/oz/epg/programming",
    "https://epg-cdn.production-public.tubi.io/content/epg/programming",
]

CONTAINER_CANDIDATES = [
    "https://tubitv.com/oz/containers/linear",
    "https://tubitv.com/oz/containers/tubitv_us_linear",
    "https://tubitv.com/oz/containers?container_id=tubitv_us_linear",
]

EPG_BATCH_SIZE = 50          # smaller batches = fewer silent server-side truncations
GROUP_TITLE = "Fast Tubi"
FALLBACK_FILE = "tubi_fallback_ids.json"
CACHE_MIN_IDS = 100          # only overwrite the cache with a list at least this big
EPG_URL_TVG = ("https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/"
               "refs/heads/main/tubi_epg.xml")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
}

SEED_FALLBACK_IDS = [
    400000008, 400000011, 400000012, 400000024, 400000028, 400000030, 400000031,
    400000033, 400000056, 400000059, 400000062, 400000063, 400000067, 400000069,
    400000070, 400000073, 400000074, 400000083, 400000085, 400000086, 400000087,
    400000088, 400000089, 400000090, 400000092, 400000094, 400000095, 400000096,
    400000098, 400000099, 400000104, 400000105, 400000106, 400000108, 400000117,
    400000119, 400000121, 400000164, 400000169, 400000179, 400000195, 400000196,
    400000247, 400000251, 400000285, 400000287, 400000289, 400000290, 400000291,
    400000294, 400000296, 400000299, 400000303, 400000304,
    555113, 555119, 555121, 555124, 555126, 555127, 555130, 555382,
    556174, 557344, 557345, 559144, 560215, 571664,
    613683, 613695, 613758, 613759, 613761, 618762, 618763, 628893, 629323,
    653199, 653200, 653208, 656575, 670602, 670604, 671073, 671083,
    673411, 673498, 673499, 673500, 677011, 680705, 682057, 682059, 682634,
    684164, 684165, 684167, 684170, 691129, 692051, 692057, 692086, 692087,
    692090, 692114, 694174, 700406, 700407, 700414, 700418, 711410,
    715946, 715947, 715948, 715949, 715950, 715951, 715952, 724209,
]

DEBUG = False

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def make_session(proxy=None):
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def dump_debug(name, text):
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    path = os.path.join("debug", re.sub(r"[^\w.-]+", "_", name)[:80] + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def describe_failure(r):
    snippet = r.text[:150].replace("\n", " ") if r.text else ""
    return f"HTTP {r.status_code} ({len(r.content)} bytes) {snippet!r}"


def is_geo_block(text):
    t = text.lower()
    return "not available in europe" in t or "gdpr.tubi.tv" in t


# ---------------------------------------------------------------------------
# JSON extraction from the live page
# ---------------------------------------------------------------------------
def _balanced_object(text, start):
    """Return the substring of the {...} object starting at text[start]."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def _clean_js(js):
    js = re.sub(r'new Date\("([^"]*)"\)', r'"\1"', js)
    js = re.sub(r"\bundefined\b", "null", js)
    js = re.sub(r"\bNaN\b", "null", js)
    return js


def extract_page_json(html):
    """Find embedded state: window.__data = {...}, __NEXT_DATA__, or any big script JSON."""
    blobs = []

    m = re.search(r"window\.__data\s*=\s*", html)
    if m:
        i = html.find("{", m.end())
        if i != -1:
            obj = _balanced_object(html, i)
            if obj:
                blobs.append(obj)

    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        blobs.append(m.group(1))

    for m in re.finditer(r"<script[^>]*>\s*(\{.*?\})\s*</script>", html, re.S):
        if "epg" in m.group(1) or "contentIds" in m.group(1):
            blobs.append(m.group(1))

    for b in blobs:
        try:
            return json.loads(_clean_js(b))
        except json.JSONDecodeError:
            continue
    return None


def find_key(obj, wanted):
    """Depth-first search for the first dict value stored under key `wanted`."""
    if isinstance(obj, dict):
        if wanted in obj:
            return obj[wanted]
        for v in obj.values():
            r = find_key(v, wanted)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, wanted)
            if r is not None:
                return r
    return None


def _to_id(v):
    if isinstance(v, dict):
        v = v.get("content_id") or v.get("id")
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def ids_from_page_data(data):
    """Return (ids, {id: category}) from the page state's contentIdsByContainer."""
    by_container = find_key(data, "contentIdsByContainer")
    ids, cats = [], {}
    if isinstance(by_container, dict):
        for cat_list in by_container.values():
            if not isinstance(cat_list, list):
                continue
            for cat in cat_list:
                if not isinstance(cat, dict):
                    continue
                name = cat.get("name") or "Other"
                for entry in cat.get("contents", []):
                    cid = _to_id(entry)
                    if cid:
                        ids.append(cid)
                        cats.setdefault(str(cid), name)
    return list(dict.fromkeys(ids)), cats


def ids_from_container_json(data):
    ids = []
    rows = data.get("rows") if isinstance(data, dict) else None
    for row in rows or []:
        for item in row.get("contents", []):
            cid = _to_id(item)
            if cid:
                ids.append(cid)
    if not ids and isinstance(data, dict):
        for item in data.get("contents", []):
            cid = _to_id(item)
            if cid:
                ids.append(cid)
    return list(dict.fromkeys(ids))


# ---------------------------------------------------------------------------
# Channel ID discovery
# ---------------------------------------------------------------------------
def discover_ids(session):
    print("Fetching live page...")
    try:
        r = session.get(LIVE_PAGE_URL, timeout=25)
        dump_debug("live_page", r.text)
        if r.status_code != 200:
            print(f"  live page: {describe_failure(r)}")
        elif is_geo_block(r.text):
            print("  live page: geo-blocked (not a US IP)")
        else:
            data = extract_page_json(r.text)
            if data is None:
                print("  live page: loaded, but no embedded JSON found (page format changed?)")
            else:
                ids, cats = ids_from_page_data(data)
                print(f"  live page: {len(ids)} IDs")
                if ids:
                    return ids, cats
    except requests.RequestException as e:
        print(f"  live page error: {e}")

    for url in CONTAINER_CANDIDATES:
        try:
            r = session.get(url, timeout=25)
            dump_debug("container_" + url, r.text)
            if r.status_code != 200:
                print(f"  {url}: {describe_failure(r)}")
                continue
            ids = ids_from_container_json(r.json())
            print(f"  {url}: {len(ids)} IDs")
            if ids:
                return ids, {}
        except (requests.RequestException, ValueError) as e:
            print(f"  {url}: {e}")

    return [], {}


def load_cached_ids():
    if os.path.exists(FALLBACK_FILE):
        try:
            with open(FALLBACK_FILE, encoding="utf-8") as f:
                ids = [int(x) for x in json.load(f).get("ids", [])]
            if ids:
                print(f"Using {len(ids)} cached IDs from {FALLBACK_FILE}")
                return ids
        except Exception as e:
            print(f"Could not read {FALLBACK_FILE}: {e}")
    print(f"Using {len(SEED_FALLBACK_IDS)} built-in seed IDs")
    return list(SEED_FALLBACK_IDS)


def save_cached_ids(ids):
    if len(ids) < CACHE_MIN_IDS:
        return
    with open(FALLBACK_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(),
                   "count": len(ids), "ids": sorted(set(ids))}, f, indent=2)
    print(f"Cached {len(ids)} IDs to {FALLBACK_FILE}")


# ---------------------------------------------------------------------------
# EPG
# ---------------------------------------------------------------------------
def rows_from_epg_response(data):
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("contents") or data.get("data") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def fetch_epg(session, ids):
    all_rows, seen = [], set()
    batches = [ids[i:i + EPG_BATCH_SIZE] for i in range(0, len(ids), EPG_BATCH_SIZE)]
    working = None  # remember which endpoint works so we stop probing the others

    for n, batch in enumerate(batches, 1):
        params = {"content_id": ",".join(map(str, batch))}
        endpoints = [working] if working else EPG_ENDPOINTS
        got = []
        for url in endpoints:
            try:
                r = session.get(url, params=params, timeout=40)
                if r.status_code != 200:
                    print(f"  batch {n}/{len(batches)} {urlparse(url).netloc}: {describe_failure(r)}")
                    continue
                got = rows_from_epg_response(r.json())
                if got:
                    working = url
                    break
                dump_debug(f"epg_empty_{n}", r.text)
            except (requests.RequestException, ValueError) as e:
                print(f"  batch {n}/{len(batches)} {urlparse(url).netloc}: {e}")
        new = 0
        for row in got:
            cid = str(row.get("content_id") or row.get("id") or "")
            if cid and cid not in seen:
                seen.add(cid)
                all_rows.append(row)
                new += 1
        print(f"EPG batch {n}/{len(batches)}: +{new} channels")
    print(f"EPG total: {len(all_rows)} channels")
    return all_rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def clean_stream_url(url):
    p = urlparse(unquote(url))
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


def pick_manifest(channel):
    """Prefer a non-DRM manifest; fall back to whatever exists."""
    resources = channel.get("video_resources") or []
    candidates = []
    for res in resources:
        url = (res.get("manifest") or {}).get("url")
        if url:
            drm = bool(res.get("license_server") or res.get("drm"))
            candidates.append((drm, url))
    candidates.sort(key=lambda x: x[0])
    return clean_stream_url(candidates[0][1]) if candidates else ""


def thumbnail(channel):
    imgs = channel.get("images") or {}
    thumb = imgs.get("thumbnail") or imgs.get("landscape") or []
    if isinstance(thumb, str):
        return thumb
    return thumb[0] if thumb else ""


def build_m3u(rows, categories, use_categories):
    lines = [f'#EXTM3U url-tvg="{EPG_URL_TVG}"',
             f"# Generated {datetime.now(timezone.utc).isoformat()}"]
    seen_urls = set()
    for ch in sorted(rows, key=lambda c: (c.get("title") or "").lower()):
        url = pick_manifest(ch)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        cid = str(ch.get("content_id") or ch.get("id") or "")
        name = (ch.get("title") or "Unknown").replace(",", " ")
        group = categories.get(cid, GROUP_TITLE) if use_categories else GROUP_TITLE
        lines.append(f'#EXTINF:-1 tvg-id="{cid}" tvg-logo="{thumbnail(ch)}" '
                     f'group-title="{group}",{name}')
        lines.append(url)
    print(f"Playlist: {len(seen_urls)} playable channels")
    return "\n".join(lines) + "\n"


def xmltv_time(iso):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(iso, fmt).strftime("%Y%m%d%H%M%S +0000")
        except (ValueError, TypeError):
            pass
    try:  # e.g. 2026-11-21T10:00:00+00:00
        return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    except (ValueError, TypeError):
        return iso or ""


def build_xmltv(rows):
    root = ET.Element("tv", {"generator-info-name": "tubi-scraper"})
    for ch in rows:
        cid = str(ch.get("content_id") or ch.get("id") or "")
        c = ET.SubElement(root, "channel", id=cid)
        ET.SubElement(c, "display-name").text = ch.get("title") or "Unknown"
        if thumbnail(ch):
            ET.SubElement(c, "icon", src=thumbnail(ch))
    for ch in rows:
        cid = str(ch.get("content_id") or ch.get("id") or "")
        for prog in ch.get("programs") or []:
            p = ET.SubElement(root, "programme", channel=cid,
                              start=xmltv_time(prog.get("start_time")),
                              stop=xmltv_time(prog.get("end_time")))
            ET.SubElement(p, "title").text = prog.get("title") or ""
            if prog.get("description"):
                ET.SubElement(p, "desc").text = prog["description"]
    return ET.ElementTree(root)


# ---------------------------------------------------------------------------
def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=os.environ.get("TUBI_PROXY"),
                    help="e.g. http://host:port or socks5://host:port (US exit)")
    ap.add_argument("--categories", action="store_true",
                    help="use Tubi categories for group-title instead of a single group")
    ap.add_argument("--debug", action="store_true", help="save raw responses to ./debug/")
    args = ap.parse_args()
    DEBUG = args.debug

    session = make_session(args.proxy)
    ids, categories = discover_ids(session)

    if ids:
        save_cached_ids(ids)
    else:
        print("Live discovery failed - falling back to cached/seed IDs")
        ids = load_cached_ids()

    ids = list(dict.fromkeys(ids))
    print(f"Channel IDs to query: {len(ids)}")

    rows = fetch_epg(session, ids)
    if not rows:
        print("ERROR: EPG returned nothing. Run with --debug, and check that "
              "you're on a US IP (Tubi blocks many datacenter/non-US IPs).")
        sys.exit(1)

    with open("tubi_playlist.m3u", "w", encoding="utf-8") as f:
        f.write(build_m3u(rows, categories, args.categories))
    build_xmltv(rows).write("tubi_epg.xml", encoding="utf-8", xml_declaration=True)
    print(f"Done: {len(rows)} channels -> tubi_playlist.m3u, tubi_epg.xml")


if __name__ == "__main__":
    main()
