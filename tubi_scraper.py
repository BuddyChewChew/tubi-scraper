import requests
import json
import re
import sys
import xml.etree.ElementTree as ET
import os
from urllib.parse import unquote, urlparse, urlunparse
from datetime import datetime
import unicodedata
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIVE_PAGE_URL       = "https://tubitv.com/live"
CONTAINERS_URL      = "https://tubitv.com/oz/containers/linear"
EPG_URL             = "https://tubitv.com/oz/epg/programming"
PROXY_API_URL       = (
    "https://api.proxyscrape.com/v2/"
    "?request=displayproxies&protocol=socks4&timeout=10000"
    "&country={country}&ssl=all&anonymity=elite"
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

# ---------------------------------------------------------------------------
# Proxy helpers
# ---------------------------------------------------------------------------

def get_proxies(country_code):
    url = PROXY_API_URL.format(country=country_code)
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return [f"socks4://{p}" for p in r.text.splitlines() if p.strip()]
        print(f"Proxy fetch returned {r.status_code}")
    except Exception as e:
        print(f"Proxy fetch error: {e}")
    return []

def _req_kwargs(proxy):
    kw = {"headers": HEADERS, "verify": False, "timeout": 20}
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    return kw

# ---------------------------------------------------------------------------
# Strategy 1 – direct JSON API (preferred, no HTML scraping)
# ---------------------------------------------------------------------------

def fetch_channel_ids_via_api(proxy):
    """
    Hit /oz/containers/linear which returns a JSON list of live channels
    with content_id values ready to pass straight to the EPG endpoint.
    Returns a list of int content_ids, or [] on failure.
    """
    try:
        r = requests.get(CONTAINERS_URL, **_req_kwargs(proxy))
        if r.status_code != 200:
            print(f"containers/linear → {r.status_code} (proxy={proxy})")
            return []
        data = r.json()
        ids = []
        # Response shape: {"rows": [{"contents": [{"content_id": ...}, ...]}]}
        for row in data.get("rows", []):
            for item in row.get("contents", []):
                cid = item.get("content_id") or item.get("id")
                if cid:
                    ids.append(int(cid))
        # Alternate flat shape: {"contents": [...]}
        if not ids:
            for item in data.get("contents", []):
                cid = item.get("content_id") or item.get("id")
                if cid:
                    ids.append(int(cid))
        print(f"API strategy: found {len(ids)} channel IDs")
        return ids
    except Exception as e:
        print(f"API strategy error: {e}")
        return []

# ---------------------------------------------------------------------------
# Strategy 2 – scrape window.__data from HTML (legacy, may be gone)
# ---------------------------------------------------------------------------

def fetch_channel_list_via_html(proxy, retries=3):
    """
    Original approach: load /live, extract window.__data JSON blob.
    Returns the parsed dict/list, or None on failure.
    """
    for attempt in range(retries):
        try:
            r = requests.get(LIVE_PAGE_URL, **_req_kwargs(proxy))
            if r.status_code != 200:
                print(f"HTML fetch → {r.status_code} attempt {attempt+1} (proxy={proxy})")
                continue

            html = r.content.decode("utf-8", errors="replace")
            soup = BeautifulSoup(html, "html.parser")

            target = None
            for script in soup.find_all("script"):
                text = script.string or ""
                if "window.__data" in text:
                    target = text
                    break

            if not target:
                # Try alternate embed names Tubi has used
                for script in soup.find_all("script"):
                    text = script.string or ""
                    if text.strip().startswith("{") and '"epg"' in text:
                        target = text
                        break

            if not target:
                print(f"HTML strategy: no window.__data found (attempt {attempt+1})")
                print("First 1000 chars of page:", html[:1000])
                continue

            start = target.find("{")
            end   = target.rfind("}") + 1
            js    = target[start:end]
            js    = js.encode("utf-8", errors="replace").decode("utf-8")
            js    = js.replace("undefined", "null")
            js    = re.sub(r'new Date\("([^"]*)"\)', r'"\1"', js)
            data  = json.loads(js)
            print("HTML strategy: successfully decoded window.__data")
            return data
        except Exception as e:
            print(f"HTML strategy error (attempt {attempt+1}): {e}")
    return None

def extract_ids_from_html_data(json_data):
    """Pull content_id list out of the window.__data blob."""
    ids = []
    container = json_data if isinstance(json_data, dict) else {}
    epg_containers = container.get("epg", {}).get("contentIdsByContainer", {})
    for cat_list in epg_containers.values():
        for cat in cat_list:
            ids.extend(cat.get("contents", []))
    return [int(i) for i in ids if i]

def create_group_mapping_from_html(json_data):
    mapping = {}
    container = json_data if isinstance(json_data, dict) else {}
    epg_containers = container.get("epg", {}).get("contentIdsByContainer", {})
    for cat_list in epg_containers.values():
        for cat in cat_list:
            name = cat.get("name", "Other")
            for cid in cat.get("contents", []):
                mapping[str(cid)] = name
    return mapping

# ---------------------------------------------------------------------------
# EPG fetch (shared by both strategies)
# ---------------------------------------------------------------------------

def fetch_epg_data(channel_ids):
    """
    Fetch EPG rows for a list of content_ids.
    Batches into groups of 150 to stay within URL length limits.
    Returns list of EPG row dicts.
    """
    if not channel_ids:
        return []

    epg_data   = []
    group_size = 150
    batches    = [channel_ids[i:i+group_size] for i in range(0, len(channel_ids), group_size)]

    for batch in batches:
        params = {"content_id": ",".join(map(str, batch))}
        try:
            r = requests.get(EPG_URL, params=params, headers=HEADERS, timeout=20)
            if r.status_code != 200:
                print(f"EPG batch failed: {r.status_code}")
                continue
            rows = r.json().get("rows", [])
            epg_data.extend(rows)
        except Exception as e:
            print(f"EPG batch error: {e}")

    print(f"EPG fetch: {len(epg_data)} rows returned")
    return epg_data

# ---------------------------------------------------------------------------
# M3U + XMLTV generation
# ---------------------------------------------------------------------------

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
        f"# Generated on {datetime.utcnow().isoformat()}Z",
    ]
    seen_urls = set()
    for ch in sorted(epg_data, key=lambda x: x.get("title", "").lower()):
        name   = (ch.get("title") or "Unknown Channel").encode("utf-8", errors="ignore").decode("utf-8")
        tvg_id = str(ch.get("content_id", ""))
        logo   = (ch.get("images", {}).get("thumbnail") or [None])[0] or ""
        group  = group_mapping.get(tvg_id, "Other").encode("utf-8", errors="ignore").decode("utf-8")

        resources = ch.get("video_resources") or []
        if not resources:
            continue
        raw_url = (resources[0].get("manifest") or {}).get("url", "")
        url = clean_stream_url(raw_url)
        if not url or url in seen_urls:
            continue

        lines.append(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}" group-title="{group}",{name}')
        lines.append(url)
        seen_urls.add(url)

    return "\n".join(lines) + "\n"

def create_epg_xml(epg_data):
    root = ET.Element("tv")
    for ch in epg_data:
        cid = str(ch.get("content_id", ""))
        channel_el = ET.SubElement(root, "channel", id=cid)
        dn = ET.SubElement(channel_el, "display-name")
        dn.text = ch.get("title", "Unknown")
        thumb = (ch.get("images", {}).get("thumbnail") or [None])[0]
        if thumb:
            ET.SubElement(channel_el, "icon", src=thumb)

        for prog in ch.get("programs", []):
            p = ET.SubElement(root, "programme",
                              channel=cid,
                              start=convert_to_xmltv_time(prog.get("start_time", "")),
                              stop=convert_to_xmltv_time(prog.get("end_time", "")))
            t = ET.SubElement(p, "title")
            t.text = prog.get("title", "")
            if prog.get("description"):
                d = ET.SubElement(p, "desc")
                d.text = prog["description"]

    return ET.ElementTree(root)

# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def save_text(content, filename):
    path = os.path.join(os.getcwd(), filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Saved: {path}")

def save_xml(tree, filename):
    path = os.path.join(os.getcwd(), filename)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    print(f"Saved: {path}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def try_with_proxy(proxy):
    """
    Full attempt with one proxy (or None).
    Returns (channel_ids, group_mapping) or ([], {}).
    """
    label = proxy or "no proxy"

    # --- Strategy 1: direct API ---
    ids = fetch_channel_ids_via_api(proxy)
    if ids:
        return ids, {}  # group_mapping not available from API alone

    # --- Strategy 2: HTML scrape ---
    print(f"API strategy empty, trying HTML scrape ({label})...")
    html_data = fetch_channel_list_via_html(proxy)
    if html_data:
        ids     = extract_ids_from_html_data(html_data)
        mapping = create_group_mapping_from_html(html_data)
        if ids:
            return ids, mapping

    return [], {}


def main():
    proxies = get_proxies("US")
    if not proxies:
        print("No proxies fetched; will try direct connection.")

    channel_ids  = []
    group_mapping = {}

    # Work through proxies, then fall back to direct
    for proxy in (proxies or [None]):
        print(f"Trying proxy: {proxy}")
        channel_ids, group_mapping = try_with_proxy(proxy)
        if channel_ids:
            print(f"Got {len(channel_ids)} channel IDs via {proxy or 'direct'}")
            break
    else:
        # proxies exhausted — one last direct attempt
        if proxies:
            print("All proxies failed. Trying direct connection...")
            channel_ids, group_mapping = try_with_proxy(None)

    if not channel_ids:
        print("ERROR: could not retrieve any channel IDs. Aborting.")
        sys.exit(1)

    epg_data = fetch_epg_data(channel_ids)
    if not epg_data:
        print("ERROR: EPG endpoint returned no data. Aborting.")
        sys.exit(1)

    m3u     = create_m3u_playlist(epg_data, group_mapping)
    epg_xml = create_epg_xml(epg_data)

    save_text(m3u, "tubi_playlist.m3u")
    save_xml(epg_xml, "tubi_epg.xml")

    print(f"Done. {len(epg_data)} channels written.")


if __name__ == "__main__":
    main()
