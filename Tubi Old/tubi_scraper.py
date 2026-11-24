#!/usr/bin/env python3
"""
Tubi Live Channels Scraper – maximized + robust version (2026-10-04)
- Multiple API candidates
- Strong recursive ID extraction
- Direct + limited US proxies
- GDPR detection
- Clean unique 174-ID fallback
- Auto-saves full ID list (≥250) when available
"""

import requests
import json
import re
import sys
import os
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlparse, urlunparse
from datetime import datetime, timezone
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
LIVE_PAGE_URL = "https://tubitv.com/live"
EPG_URL       = "https://tubitv.com/oz/epg/programming"

CONTAINER_CANDIDATES = [
    "https://tubitv.com/oz/containers/linear",
    "https://tubitv.com/oz/containers/tubitv_us_linear",
    "https://tubitv.com/oz/containers?container_id=tubitv_us_linear",
    "https://tubitv.com/oz/containers?slug=tubitv_us_linear",
    "https://tubitv.com/oz/containers/linear?platform=web",
]

PROXY_API_URL = (
    "https://api.proxyscrape.com/v2/"
    "?request=displayproxies&protocol=socks4&timeout=10000"
    "&country=US&ssl=all&anonymity=elite"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/html, */*",
}

FALLBACK_FILE = "tubi_fallback_ids.json"

# Clean unique 174-ID seed list (from 2026-10-02 successful playlist)
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

# ---------------------------------------------------------------------------
def _req_kwargs(proxy=None, timeout=20):
    kw = {"headers": HEADERS, "verify": False, "timeout": timeout}
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    return kw

def get_proxies(limit=8):
    try:
        r = requests.get(PROXY_API_URL, timeout=15)
        if r.status_code == 200:
            return [f"socks4://{p.strip()}" for p in r.text.splitlines() if p.strip()][:limit]
    except Exception as e:
        print(f"Proxy fetch error: {e}")
    return []

def is_gdpr_block(html: str) -> bool:
    return "not available in Europe" in html or "gdpr.tubi.tv" in html.lower()

def load_fallback_ids():
    if os.path.exists(FALLBACK_FILE):
        try:
            with open(FALLBACK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                ids = [int(x) for x in data.get("ids", [])]
                if ids:
                    print(f"Loaded {len(ids)} IDs from {FALLBACK_FILE}")
                    return ids
        except Exception as e:
            print(f"Could not load {FALLBACK_FILE}: {e}")
    return list(SEED_FALLBACK_IDS)

def save_fallback_ids(ids):
    if len(ids) < 250:
        return
    try:
        payload = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "count": len(ids),
            "ids": sorted(set(ids))
        }
        with open(FALLBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"Saved {len(ids)} IDs to {FALLBACK_FILE}")
    except Exception as e:
        print(f"Failed to save fallback IDs: {e}")

def _recursive_extract_ids(obj, found=None):
    """Strong recursive walker – looks for any plausible content_id."""
    if found is None:
        found = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = str(k).lower()
            if key in ("content_id", "contentid", "id", "channel_id", "channelid") and isinstance(v, (int, str)):
                try:
                    cid = int(v)
                    if 100000 <= cid <= 999999999:
                        found.add(cid)
                except (ValueError, TypeError):
                    pass
            else:
                _recursive_extract_ids(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _recursive_extract_ids(item, found)
    return found

def fetch_channel_ids_via_api(proxy=None):
    for url in CONTAINER_CANDIDATES:
        try:
            r = requests.get(url, **_req_kwargs(proxy))
            if r.status_code != 200:
                print(f"  {url} → {r.status_code}")
                continue
            data = r.json()
            ids = []
            for row in data.get("rows", []):
                for item in row.get("contents", []):
                    cid = item.get("content_id") or item.get("id")
                    if cid:
                        ids.append(int(cid))
            if not ids:
                for item in data.get("contents", []):
                    cid = item.get("content_id") or item.get("id")
                    if cid:
                        ids.append(int(cid))
            if not ids:
                ids = list(_recursive_extract_ids(data))
            if ids:
                print(f"API strategy ({url}): found {len(ids)} channel IDs")
                return list(dict.fromkeys(ids))
        except Exception as e:
            print(f"  API error on {url}: {e}")
    return []

def fetch_channel_list_via_html(proxy=None, retries=2):
    for attempt in range(retries):
        try:
            r = requests.get(LIVE_PAGE_URL, **_req_kwargs(proxy))
            if r.status_code != 200:
                print(f"HTML fetch → {r.status_code} (attempt {attempt+1})")
                continue
            html = r.content.decode("utf-8", errors="replace")
            if is_gdpr_block(html):
                print("GDPR / geo-block detected – skipping")
                return None
            soup = BeautifulSoup(html, "html.parser")
            target = None
            for script in soup.find_all("script"):
                text = script.string or ""
                if "window.__data" in text:
                    target = text
                    break
            if not target:
                for script in soup.find_all("script"):
                    text = script.string or ""
                    if text.strip().startswith("{") and '"epg"' in text:
                        target = text
                        break
            if not target:
                continue
            start = target.find("{")
            end   = target.rfind("}") + 1
            js = target[start:end].replace("undefined", "null")
            js = re.sub(r'new Date\("([^"]*)"\)', r'"\1"', js)
            data = json.loads(js)
            print("HTML strategy: successfully decoded window.__data")
            return data
        except Exception as e:
            print(f"HTML strategy error (attempt {attempt+1}): {e}")
    return None

def extract_ids_from_html_data(json_data):
    ids = []
    # Classic path
    container = json_data if isinstance(json_data, dict) else {}
    epg = container.get("epg", {}).get("contentIdsByContainer", {})
    for cat_list in epg.values():
        for cat in cat_list:
            for entry in cat.get("contents", []):
                if isinstance(entry, (int, str)):
                    try:
                        ids.append(int(entry))
                    except Exception:
                        pass
                elif isinstance(entry, dict):
                    cid = entry.get("content_id") or entry.get("id")
                    if cid:
                        ids.append(int(cid))
    # Strong recursive fallback
    ids.extend(_recursive_extract_ids(json_data))
    return list(dict.fromkeys(ids))

def create_group_mapping_from_html(json_data):
    mapping = {}
    container = json_data if isinstance(json_data, dict) else {}
    epg = container.get("epg", {}).get("contentIdsByContainer", {})
    for cat_list in epg.values():
        for cat in cat_list:
            name = cat.get("name", "Other")
            for entry in cat.get("contents", []):
                cid = entry if isinstance(entry, (int, str)) else (entry.get("content_id") or entry.get("id"))
                if cid:
                    mapping[str(cid)] = name
    return mapping

def fetch_epg_data(channel_ids, proxy=None):
    if not channel_ids:
        return []
    epg_data = []
    group_size = 120
    batches = [channel_ids[i:i + group_size] for i in range(0, len(channel_ids), group_size)]
    for i, batch in enumerate(batches, 1):
        params = {"content_id": ",".join(map(str, batch))}
        try:
            r = requests.get(EPG_URL, params=params, **_req_kwargs(proxy, timeout=30))
            if r.status_code == 200:
                rows = r.json().get("rows", [])
                epg_data.extend(rows)
                print(f"EPG batch {i}/{len(batches)}: +{len(rows)} rows")
            else:
                print(f"EPG batch {i} failed: {r.status_code}")
        except Exception as e:
            print(f"EPG batch error: {e}")
    print(f"EPG total: {len(epg_data)} rows")
    return epg_data

def clean_stream_url(url):
    p = urlparse(unquote(url))
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

def convert_to_xmltv_time(iso_time):
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%Y%m%d%H%M%S +0000")
    except ValueError:
        return iso_time

def create_m3u_playlist(epg_data, group_mapping):
    lines = [
        '#EXTM3U url-tvg="https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/refs/heads/main/tubi_epg.xml"',
        f"# Generated on {datetime.now(timezone.utc).isoformat()}",
    ]
    seen = set()
    for ch in sorted(epg_data, key=lambda x: (x.get("title") or "").lower()):
        name   = (ch.get("title") or "Unknown").encode("utf-8", "ignore").decode("utf-8")
        tvg_id = str(ch.get("content_id", ""))
        logo   = (ch.get("images", {}).get("thumbnail") or [None])[0] or ""
        group  = group_mapping.get(tvg_id, "Other").encode("utf-8", "ignore").decode("utf-8")
        resources = ch.get("video_resources") or []
        if not resources:
            continue
        raw = (resources[0].get("manifest") or {}).get("url", "")
        url = clean_stream_url(raw)
        if not url or url in seen:
            continue
        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}" group-title="{group}",{name}')
        lines.append(url)
        seen.add(url)
    return "\n".join(lines) + "\n"

def create_epg_xml(epg_data):
    root = ET.Element("tv")
    for ch in epg_data:
        cid = str(ch.get("content_id", ""))
        channel_el = ET.SubElement(root, "channel", id=cid)
        ET.SubElement(channel_el, "display-name").text = ch.get("title", "Unknown")
        thumb = (ch.get("images", {}).get("thumbnail") or [None])[0]
        if thumb:
            ET.SubElement(channel_el, "icon", src=thumb)
        for prog in ch.get("programs", []):
            p = ET.SubElement(root, "programme",
                              channel=cid,
                              start=convert_to_xmltv_time(prog.get("start_time", "")),
                              stop=convert_to_xmltv_time(prog.get("end_time", "")))
            ET.SubElement(p, "title").text = prog.get("title", "")
            if prog.get("description"):
                ET.SubElement(p, "desc").text = prog["description"]
    return ET.ElementTree(root)

def save_text(content, filename):
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Saved: {filename}")

def save_xml(tree, filename):
    tree.write(filename, encoding="utf-8", xml_declaration=True)
    print(f"Saved: {filename}")

def try_with_proxy(proxy):
    label = proxy or "direct"
    print(f"\n=== Trying {label} ===")
    ids = fetch_channel_ids_via_api(proxy)
    if ids:
        return ids, {}
    print("API empty → trying HTML…")
    html_data = fetch_channel_list_via_html(proxy)
    if html_data:
        ids = extract_ids_from_html_data(html_data)
        mapping = create_group_mapping_from_html(html_data)
        print(f"HTML extraction: {len(ids)} IDs")
        if ids:
            return ids, mapping
    return [], {}

def main():
    channel_ids = []
    group_mapping = {}

    # 1. Direct first
    channel_ids, group_mapping = try_with_proxy(None)

    # 2. Proxies if needed
    if not channel_ids:
        proxies = get_proxies(limit=8)
        print(f"Fetched {len(proxies)} proxies")
        for proxy in proxies:
            channel_ids, group_mapping = try_with_proxy(proxy)
            if channel_ids:
                break

    # 3. Fallback
    if not channel_ids:
        print("All live strategies failed – using fallback IDs")
        channel_ids = load_fallback_ids()

    if not channel_ids:
        print("ERROR: no channel IDs available. Aborting.")
        sys.exit(1)

    # Deduplicate
    channel_ids = list(dict.fromkeys(channel_ids))
    print(f"\nFinal channel ID count: {len(channel_ids)}")

    # Auto-save if we got a high-quality list
    save_fallback_ids(channel_ids)

    epg_data = fetch_epg_data(channel_ids)
    if not epg_data:
        print("ERROR: EPG returned no data. Aborting.")
        sys.exit(1)

    m3u = create_m3u_playlist(epg_data, group_mapping)
    epg_xml = create_epg_xml(epg_data)

    save_text(m3u, "tubi_playlist.m3u")
    save_xml(epg_xml, "tubi_epg.xml")

    print(f"\nDone. {len(epg_data)} channels written.")

if __name__ == "__main__":
    main()
