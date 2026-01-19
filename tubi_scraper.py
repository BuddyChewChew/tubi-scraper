import requests
import gzip
import json
import os
import logging
import re
import xml.etree.ElementTree as ET
import unicodedata
import urllib3
from io import BytesIO
from urllib.parse import unquote
from datetime import datetime
from bs4 import BeautifulSoup

# Disable insecure warnings for Tubi scraping
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuration ---
OUTPUT_DIR = "playlists"
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

# Get repo info for the Tubi EPG link
REPO_OWNER = os.getenv('GITHUB_REPOSITORY_OWNER', 'YOUR_USERNAME')
REPO_NAME = os.getenv('GITHUB_REPOSITORY', 'YOUR_REPO').split('/')[-1]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Helper Functions ---

def cleanup_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

def fetch_url(url, is_json=True, is_gzipped=False):
    try:
        response = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=30)
        response.raise_for_status()
        content = response.content
        if is_gzipped:
            with gzip.GzipFile(fileobj=BytesIO(content), mode='rb') as f:
                content = f.read()
        return json.loads(content.decode('utf-8')) if is_json else content.decode('utf-8')
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

def format_extinf(c_id, tvg_id, chno, name, logo, group):
    clean_name = name.replace('"', "'")
    return f'#EXTINF:-1 channel-id="{c_id}" tvg-id="{tvg_id}" tvg-chno="{chno or ""}" tvg-logo="{logo}" group-title="{group}",{clean_name}\n'

# --- TUBI SCRAPER LOGIC (INTEGRATED FROM YOUR SCRIPT) ---

def generate_tubi_m3u_and_epg():
    logger.info("Starting Tubi Scraper Logic...")
    url = "https://tubitv.com/live"
    try:
        # 1. Scrape raw data from the window.__data script tag
        resp = requests.get(url, headers={'User-Agent': USER_AGENT}, verify=False, timeout=20)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        target_script = None
        for script in soup.find_all("script"):
            if script.string and "window.__data" in script.string:
                target_script = script.string
                break
        
        if not target_script:
            logger.error("Could not find window.__data in Tubi page.")
            return

        # Clean JSON-like string
        json_str = target_script[target_script.find("{"):target_script.rfind("}") + 1]
        json_str = json_str.replace('undefined', 'null')
        json_str = re.sub(r'new Date\("([^"]*)"\)', r'"\1"', json_str)
        data = json.loads(json_str)

        # 2. Extract Genres/Groups using create_group_mapping logic
        group_mapping = {}
        containers = data.get('epg', {}).get('contentIdsByContainer', {})
        for container_list in containers.values():
            for category in container_list:
                g_name = category.get('name', 'Other')
                for cid in category.get('contents', []):
                    group_mapping[str(cid)] = g_name

        # 3. Fetch EPG data in chunks of 150
        channel_ids = list(group_mapping.keys())
        epg_rows = []
        for i in range(0, len(channel_ids), 150):
            chunk = channel_ids[i:i+150]
            r = requests.get("https://tubitv.com/oz/epg/programming", params={"content_id": ','.join(chunk)})
            if r.status_code == 200:
                epg_rows.extend(r.json().get('rows', []))

        # 4. Generate Files
        epg_url = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/main/playlists/tubi_epg.xml"
        m3u = [f'#EXTM3U url-tvg="{epg_url}"\n']
        root_xml = ET.Element("tv")

        for ch in sorted(epg_rows, key=lambda x: x.get('title', '').lower()):
            cid = str(ch.get('content_id'))
            name = ch.get('title', 'Tubi')
            logo = ch.get('images', {}).get('thumbnail', [None])[0]
            group = group_mapping.get(cid, "Other")
            
            # Manifest URL handling
            stream_raw = ch.get('video_resources', [{}])[0].get('manifest', {}).get('url', '')
            if stream_raw:
                # Clean URL (remove query params)
                stream_url = unquote(stream_raw).split('?')[0]
                m3u.append(format_extinf(cid, cid, "", name, logo, group))
                m3u.append(f"{stream_url}\n")

            # EPG Generation
            c_node = ET.SubElement(root_xml, "channel", id=cid)
            ET.SubElement(c_node, "display-name").text = name
            ET.SubElement(c_node, "icon", src=logo or "")
            
            for p in ch.get('programs', []):
                p_node = ET.SubElement(root_xml, "programme", channel=cid)
                # ISO to XMLTV Time Format
                for k, xk in [("start_time", "start"), ("end_time", "stop")]:
                    raw_t = p.get(k, "")
                    t = raw_t.replace("-","").replace(":","").replace("T","").replace("Z","") + " +0000"
                    p_node.set(xk, t)
                ET.SubElement(p_node, "title").text = p.get("title", "")
                if p.get("description"):
                    ET.SubElement(p_node, "desc").text = p.get("description", "")

        # Save files
        with open(os.path.join(OUTPUT_DIR, "tubi_all.m3u"), 'w', encoding='utf-8') as f:
            f.write("".join(m3u))
        ET.ElementTree(root_xml).write(os.path.join(OUTPUT_DIR, "tubi_epg.xml"), encoding='utf-8', xml_declaration=True)
        logger.info("Tubi playlist and EPG saved.")

    except Exception as e:
        logger.error(f"Tubi processing failed: {e}")

# --- Other functions (Left exactly as they were) ---

def generate_standard_m3u(service_name, base_url, prefix):
    # This remains the same as your existing standard logic
    data = fetch_url(f'{base_url}/.channels.json.gz', is_gzipped=True)
    if not data: return
    
    output = [f'#EXTM3U url-tvg="{base_url}/all.xml.gz"\n']
    channels = data.get('channels', {}) if 'channels' in data else data.get('regions', {}).get('us', {}).get('channels', {})
    
    for cid, ch in channels.items():
        grps = ch.get('groups', [])
        group = ch.get('group') or (grps[0] if grps else "Unsorted")
        output.append(format_extinf(cid, cid, ch.get('chno'), ch['name'], ch['logo'], group))
        output.append(f"https://jmp2.uk/{prefix}-{cid}.m3u8\n")
        
    with open(os.path.join(OUTPUT_DIR, f"{service_name}_all.m3u"), 'w', encoding='utf-8') as f:
        f.write("".join(output))

if __name__ == "__main__":
    cleanup_output_dir()
    # Your integrated Tubi Scraper logic
    generate_tubi_m3u_and_epg()
    # Standard services pointing to Matt's EPG
    generate_standard_m3u("pluto", "https://i.mjh.nz/PlutoTV", "plu")
    generate_standard_m3u("plex", "https://i.mjh.nz/Plex", "plex")
    generate_standard_m3u("roku", "https://i.mjh.nz/Roku", "rok")
    generate_standard_m3u("samsung", "https://i.mjh.nz/SamsungTVPlus", "stvp")
    logger.info("All tasks completed.")
