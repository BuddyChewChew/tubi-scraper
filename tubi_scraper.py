import requests
from bs4 import BeautifulSoup
import json
import re
import xml.etree.ElementTree as ET
import os
from urllib.parse import unquote
from urllib.parse import urlparse, urlunparse
from datetime import datetime
import unicodedata
import urllib3

# Disable InsecureRequestWarning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Default headers with User-Agent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def get_proxies(country_code):
    url = f"https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4&timeout=10000&country={country_code}&ssl=all&anonymity=elite"
    response = requests.get(url, headers=HEADERS)
    if response.status_code == 200:
        proxy_list = response.text.splitlines()
        print(f"Fetched {len(proxy_list)} proxies for {country_code}")
        return [f"socks4://{proxy}" for proxy in proxy_list]
    else:
        print(f"Failed to fetch proxies for {country_code}. Status code: {response.status_code}")
        return []

def fetch_channel_list(proxy, retries=3):
    url = "https://tubitv.com/live"
    for attempt in range(retries):
        try:
            if proxy:
                response = requests.get(url, headers=HEADERS, proxies={"http": proxy, "https": proxy}, verify=False, timeout=20)
            else:
                response = requests.get(url, headers=HEADERS, verify=False, timeout=20)
            response.encoding = 'utf-8'
            if response.status_code != 200:
                print(f"Failed to fetch {url} with proxy {proxy}. Status code: {response.status_code}")
                continue

            html_content = response.content.decode('utf-8', errors='replace')
            html_content = html_content.replace('�', 'ñ')
            soup = BeautifulSoup(html_content, "html.parser")

            script_tags = soup.find_all("script")
            target_script = None
            for script in script_tags:
                if script.string and script.string.strip().startswith("window.__data"):
                    target_script = script.string
                    break

            if not target_script:
                print("Error: Could not locate window.__data in the page.")
                print(f"Response preview:\n{html_content[:500]}...")
                continue

            start_index = target_script.find("{")
            end_index = target_script.rfind("}") + 1
            json_string = target_script[start_index:end_index]
            json_string = json_string.encode('utf-8', errors='replace').decode('utf-8')
            json_string = json_string.replace('undefined', 'null')
            json_string = re.sub(r'new Date\("([^"]*)"\)', r'"\1"', json_string)
            print(f"Extracted JSON preview (first 500 chars): {json_string[:500]}...")
            data = json.loads(json_string)
            containers = data.get('epg', {}).get('contentIdsByContainer', {})
            print(f"Successfully decoded JSON with {len(containers)} containers!")
            return data
        except requests.RequestException as e:
            print(f"Error fetching data with proxy {proxy}: {e}")
    return []

def create_group_mapping(json_data):
    group_mapping = {}
    if isinstance(json_data, list):
        for item in json_data:
            content_ids_by_container = item.get('epg', {}).get('contentIdsByContainer', {})
            for container_key, container_list in content_ids_by_container.items():
                for category in container_list:
                    group_name = category.get('name', 'Other')
                    for content_id in category.get('contents', []):
                        group_mapping[str(content_id)] = group_name
    else:
        content_ids_by_container = json_data.get('epg', {}).get('contentIdsByContainer', {})
        for container_key, container_list in content_ids_by_container.items():
            for category in container_list:
                group_name = category.get('name', 'Other')
                for content_id in category.get('contents', []):
                    group_mapping[str(content_id)] = group_name
    print(f"Created group mapping with {len(group_mapping)} content IDs")
    return group_mapping

def fetch_epg_data(channel_list, proxy=None):
    epg_data = []
    group_size = 150
    grouped_ids = [channel_list[i:i + group_size] for i in range(0, len(channel_list), group_size)]

    for group in grouped_ids:
        url = "https://tubitv.com/oz/epg/programming"
        params = {"content_id": ','.join(map(str, group))}
        try:
            if proxy:
                response = requests.get(url, params=params, headers=HEADERS, proxies={"http": proxy, "https": proxy}, timeout=20)
            else:
                response = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if response.status_code != 200:
                print(f"Failed to fetch EPG for {len(group)} IDs. Status code: {response.status_code}")
                continue
            epg_json = response.json()
            rows = epg_json.get('rows', [])
            epg_data.extend(rows)
            print(f"Requested {len(group)} IDs, got {len(rows)} channels from EPG")
        except (requests.RequestException, json.JSONDecodeError) as e:
            print(f"Error fetching EPG for group: {e}")
    print(f"Total EPG channels fetched: {len(epg_data)}")
    return epg_data

def clean_stream_url(url):
    parsed_url = urlparse(url)
    clean_url = urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, '', '', ''))
    return clean_url

def normalize_text(text):
    normalized_text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode('ascii')
    return normalized_text

def create_m3u_playlist(epg_data, group_mapping, country):
    sorted_epg_data = sorted(epg_data, key=lambda x: x.get('title', '').lower())
    playlist = f"#EXTM3U url-tvg=\"https://raw.githubusercontent.com/BuddyChewChew/tubi-scraper/refs/heads/main/tubi_epg.xml\"\n"
    playlist += f"# Generated on {datetime.now().isoformat()}\n"
    seen_urls = set()
    channel_count = 0

    for elem in sorted_epg_data:
        channel_name = elem.get('title', 'Unknown Channel')
        channel_name = channel_name.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')
        stream_url = unquote(elem['video_resources'][0]['manifest']['url']) if elem.get('video_resources') else ''
        clean_url = clean_stream_url(stream_url)
        tvg_id = str(elem.get('content_id', ''))
        logo_url = elem.get('images', {}).get('thumbnail', [None])[0]
        group_title = group_mapping.get(tvg_id, 'Other')
        group_title = group_title.encode('utf-8', errors='ignore').decode('utf-8', errors='ignore')

        if clean_url and clean_url not in seen_urls:
            playlist += f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo_url}" group-title="{group_title}",{channel_name}\n{clean_url}\n'
            seen_urls.add(clean_url)
            channel_count += 1
    print(f"Generated M3U playlist with {channel_count} unique channels")
    return playlist

def convert_to_xmltv_format(iso_time):
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ")
        xmltv_time = dt.strftime("%Y%m%d%H%M%S +0000")
        return xmltv_time
    except ValueError:
        return iso_time

def create_epg_xml(epg_data):
    root = ET.Element("tv")
    for station in epg_data:
        channel = ET.SubElement(root, "channel", id=str(station.get("content_id")))
        display_name = ET.SubElement(channel, "display-name")
        display_name.text = station.get("title", "Unknown Title")
        icon = ET.SubElement(channel, "icon", src=station.get("images", {}).get("thumbnail", [None])[0])

        for program in station.get('programs', []):
            programme = ET.SubElement(root, "programme", channel=str(station.get("content_id")))
            start_time = convert_to_xmltv_format(program.get("start_time", ""))
            stop_time = convert_to_xmltv_format(program.get("end_time", ""))
            programme.set("start", start_time)
            programme.set("stop", stop_time)
            title = ET.SubElement(programme, "title")
            title.text = program.get("title", "")
            if program.get("description"):
                desc = ET.SubElement(programme, "desc")
                desc.text = program.get("description", "")
    tree = ET.ElementTree(root)
    print(f"Generated EPG XML for {len(epg_data)} channels")
    return tree

def save_file(content, filename):
    file_path = os.path.join(os.getcwd(), filename)
    with open(file_path, 'w', encoding='utf-8') as file:
        file.write(content)
    print(f"File saved: {file_path}")

def save_epg_to_file(tree, filename):
    file_path = os.path.join(os.getcwd(), filename)
    tree.write(file_path, encoding='utf-8', xml_declaration=True)
    print(f"EPG XML file saved: {file_path}")

def main():
    countries = ["US"]
    for country in countries:
        proxies = get_proxies(country)
        json_data = None
        selected_proxy = None

        if not proxies:
            print(f"No proxies found for {country}. Trying without proxy...")
            json_data = fetch_channel_list(None)
        else:
            for proxy in proxies:
                print(f"Trying proxy {proxy} for {country}...")
                json_data = fetch_channel_list(proxy)
                if json_data:
                    selected_proxy = proxy
                    break
            else:
                print(f"All proxies failed for {country}. Trying without proxy...")
                json_data = fetch_channel_list(None)

        if not json_data:
            print(f"Failed to fetch data for {country}")
            continue

        print(f"Successfully fetched data for {country}")
        channel_list = []
        if isinstance(json_data, list):
            for item in json_data:
                content_ids_by_container = item.get('epg', {}).get('contentIdsByContainer', {})
                for container_list in content_ids_by_container.values():
                    for category in container_list:
                        contents = category.get('contents', [])
                        channel_list.extend(contents)
                        print(f"Found {len(contents)} channels in category '{category.get('name', 'Unknown')}'")
        else:
            content_ids_by_container = json_data.get('epg', {}).get('contentIdsByContainer', {})
            for container_list in content_ids_by_container.values():
                for category in container_list:
                    contents = category.get('contents', [])
                    channel_list.extend(contents)
                    print(f"Found {len(contents)} channels in category '{category.get('name', 'Unknown')}'")
        print(f"Total channels extracted from JSON: {len(channel_list)}")

        epg_data = fetch_epg_data(channel_list, selected_proxy)
        if not epg_data:
            print("No EPG data found.")
            continue

        group_mapping = create_group_mapping(json_data)
        m3u_playlist = create_m3u_playlist(epg_data, group_mapping, country.lower())
        epg_tree = create_epg_xml(epg_data)

        save_file(m3u_playlist, "tubi_playlist.m3u")
        save_epg_to_file(epg_tree, "tubi_epg.xml")

if __name__ == "__main__":
    main()
