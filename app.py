#!/usr/bin/python3
import os
import json
import gzip
import time
import argparse
import threading
import traceback
from base64 import b64decode
from tempfile import gettempdir
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qsl, urlencode, unquote, parse_qs

import re

import requests


PLAYLIST_PATH = 'playlist.m3u8'
TVH_PLAYLIST_PATH = 'tvh.m3u8'
EPG_PATH = 'epg.xml'
CLEAR_CACHE_PATH = 'clear_cache'
DEEPLINK_PATH = 'deep_link'
STATUS_PATH = ''
APP_URL = 'https://i.mjh.nz/au/{region}/tv.json.gz'
EPG_URL = 'https://i.mjh.nz/au/{region}/epg.xml.gz'
DELIMITER = '|'
TIMEOUT = (5, 20)  # connect,read
CACHE_TIME = int(os.getenv("CACHE_TIME", 300))  # default of 5mins
CHUNKSIZE = 1024
REGION = os.environ.get('REGION', 'all')

# REGION is fixed per process, so this is exactly two cacheable resources --
# name includes REGION so multiple instances/regions can share a host tmpdir.
CACHE_DIR = os.path.join(gettempdir(), 'iptv-au-docker')
APP_CACHE_PATH = os.path.join(CACHE_DIR, f'app-{REGION}.json')
EPG_CACHE_PATH = os.path.join(CACHE_DIR, f'epg-{REGION}.xml')
os.makedirs(CACHE_DIR, exist_ok=True)
print(f"Cache dir: {CACHE_DIR}")

# Derive group-title from slug region/city suffix, fall back to national.
_CITY_STATE = {
    'mel': 'VIC', 'syd': 'NSW', 'bri': 'QLD', 'ade': 'SA', 'per': 'WA',
    'cns': 'QLD', 'tsv': 'QLD', 'mky': 'QLD', 'rky': 'QLD',
    'wby': 'QLD', 'twb': 'QLD', 'ssc': 'QLD', 'coast': 'QLD',
    'newcastle': 'NSW', 'lismore': 'NSW', 'mountains': 'NSW',
}


def _cache_fresh(path):
    return os.path.exists(path) and (time.time() - os.path.getmtime(path) < CACHE_TIME)


def _atomic_write(path, data):
    tmp_path = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    try:
        with open(tmp_path, 'wb') as f:
            f.write(data)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def tvh_headers(headers=None):
    string = ''
    if headers:
        for key in headers:
            string += u'{0}:\ {1}\\r\\n'.format(key, '{}'.format(headers[key]).replace(' ', '\ '))
    return string.strip()


def is_valid_url(url):
    if not url:
        return False

    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self._params = {}
        self._headers_sent = False
        super().__init__(*args, **kwargs)

    def flush_headers(self):
        # Only place bytes actually reach the socket -- track *here*, not in
        # send_response(), which only buffers the status line.
        self._headers_sent = True
        super().flush_headers()

    def _error(self):
        traceback.print_exc()
        # Response may already be underway (e.g. mid-playlist) -- can't
        # cleanly send a status line at that point, just stop.
        if self._headers_sent:
            return
        self.send_response(500)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(b'Internal server error')

    def do_GET(self):
        routes = {
            PLAYLIST_PATH: self._playlist,
            TVH_PLAYLIST_PATH: self._tvh_playlist,
            EPG_PATH: self._epg,
            STATUS_PATH: self._status,
            CLEAR_CACHE_PATH: self._clear_cache,
            DEEPLINK_PATH: self._deeplink
        }

        parsed = urlparse(self.path)
        func = parsed.path.split('/')[1]
        self._params = dict(parse_qsl(parsed.query, keep_blank_values=True))

        if func not in routes:
            self.send_response(404)
            self.end_headers()
            return

        try:
            routes[func]()
        except Exception:
            self._error()

    def _deeplink(self):
        plugin_url = '/'.join(self.path.split('/')[2:])
        parsed = urlparse(plugin_url)
        params = dict(parse_qsl(parsed.query))
        access_token = requests.get('https://i.mjh.nz/.tokens/9now.tk', timeout=TIMEOUT).text

        query = {
            'device': 'web',
            'streamParams': 'web,chrome,windows',
            'region': params['region'],
            'offset': 0,
        }
        data = requests.get('https://api.9now.com.au/ctv/livestreams', params=query, headers={'Authorization': f'Bearer {access_token}'}, timeout=TIMEOUT).json()
        if "errors" in data:
            raise Exception(data)
        data = data['data']['getLivestream']
        data['channels'].extend([row for row in data['events'] if row['type'] == 'live-event' and row['nextEvent']['name']])

        url = None
        for row in data['channels']:
            if row['referenceId'] == params['reference']:
                url = row['stream']['url']
                break

        if not url:
            raise Exception("couldnt find stream url")

        try:
            _url = url
            parsed_url = urlparse(_url)
            query_params = parse_qs(parsed_url.query)

            yo_fb_encoded = query_params.get('yo.eb.fb', [None])[0]
            if yo_fb_encoded:
                yo_fb_decoded_once = unquote(yo_fb_encoded)
                _url = b64decode(yo_fb_decoded_once).decode('utf-8')
            else:
                _url = None

            if not is_valid_url(_url):
                yo_pp_encoded = query_params.get('yo.pp', [None])[0]
                if yo_pp_encoded:
                    yo_pp_decoded_once = unquote(yo_pp_encoded)
                    yo_pp_base64_decoded = b64decode(yo_pp_decoded_once).decode('utf-8')
                else:
                    yo_pp_base64_decoded = ''
                yo_up_encoded = query_params.get('yo.up', [None])[0]
                yo_up_decoded = unquote(yo_up_encoded)
                _url = yo_up_decoded + 'index.m3u8?' + yo_pp_base64_decoded

            if not is_valid_url(_url):
                raise Exception(f"Invalid url: {_url}")

            url = _url
        except Exception as e:
            print(f"failed to get raw url for: {url} ({e}). Fallback to yo stream")
            # fix encoded query
            if '?' in url:
                url = url.split('?')[0] + '?' + urlencode(parse_qsl(url.split('?')[1]))

        url = url.strip('?')
        print(f"Redirect to: {url}")
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _clear_cache(self):
        for path in (APP_CACHE_PATH, EPG_CACHE_PATH):
            if os.path.exists(path):
                os.unlink(path)
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'Cache cleared')

    def _app_data(self):
        app_url = APP_URL.format(region=REGION)
        if _cache_fresh(APP_CACHE_PATH):
            self.log_message(f"Cache hit: {app_url}")
            with open(APP_CACHE_PATH, 'r') as f:
                return json.load(f)

        self.log_message(f"Downloading {app_url}...")
        resp = requests.get(app_url, timeout=TIMEOUT)
        resp.raise_for_status()
        data = json.loads(gzip.decompress(resp.content))
        _atomic_write(APP_CACHE_PATH, json.dumps(data).encode('utf-8'))
        return data

    def _tvh_playlist(self):
        self._playlist(_type='tvh')

    def _playlist(self, _type='m3u8'):
        channels = self._app_data()

        start_chno = int(self._params['start_chno']) if 'start_chno' in self._params else None
        sort = self._params.get('sort', 'chno')
        include = [x for x in self._params.get('include', '').split(DELIMITER) if x]
        exclude = [x for x in self._params.get('exclude', '').split(DELIMITER) if x]

        self.send_response(200)
        self.send_header('content-type', 'vnd.apple.mpegurl')
        self.end_headers()

        host = self.headers.get('Host')

        self.wfile.write(b'#EXTM3U\n\n')
        for slug in sorted(channels.keys(), key=lambda x: channels[x].get('chno', 9999999) if sort == 'chno' else channels[x]['name'].strip().lower()):
            channel = channels[slug]
            logo = channel['logo']
            name = channel['name']
            url = channel['mjh_master']
            channel_id = f'iptv-au-{slug}'
            is_radio = channel.get('is_radio', False)

            if url.lower().startswith('plugin://slyguy.9now/'):
                url = f"http://{host}/{DEEPLINK_PATH}/{url}"
            elif not url.lower().startswith('http'):
                continue

            # Skip channels that require a license
            if channel.get('license_url'):
                continue

            # Apply include/exclude filters
            if (include and channel_id not in include) or (exclude and channel_id in exclude):
                continue

            chno = ''
            if start_chno is not None:
                if start_chno > 0:
                    chno = f' tvg-chno="{start_chno}"'
                    start_chno += 1
            elif channel.get('chno') is not None:
                chno = ' tvg-chno="{}"'.format(channel['chno'])

            if _type == 'tvh':
                headers = tvh_headers(channel.get('headers'))
                url = "pipe://ffmpeg -loglevel fatal -probesize 10M -analyzeduration 0 -fpsprobesize 0 {headers}-i {url}{radio} -vcodec copy -acodec copy -metadata service_name={id} -f mpegts pipe:1".format(
                    headers=headers, url=url, radio=' -mpegts_service_type digital_radio' if is_radio else '', id=channel_id)
                tags = ''
            else:
                headers = {key: value for key, value in channel.get('headers', {}).items() if key in ('user-agent', 'referer')}
                tags = []
                for key, value in headers.items():
                    if value.startswith(' '):
                        value = u'%20{}'.format(value)
                    tags.append(f'http-{key.lower()}={value}')
                tags = "\n".join(f'#EXTVLCOPT:{tag}' for tag in tags)
                if tags:
                    tags = '\n' + tags

            # Derive group from slug region/city suffix, fall back to national
            m = re.search(r'-(nsw|vic|qld|sa|wa|tas|nt|act)(-|$)', slug, re.I)
            if m:
                state = m.group(1).upper()
            else:
                city = slug.split('-')[-1].lower()
                state = _CITY_STATE.get(city)
            group = f'AU| {state}' if state else 'AU| FREE TO AIR'

            # Write channel information
            self.wfile.write(f'#EXTINF:-1 channel-id="{channel_id}" tvg-id="{slug}" tvg-logo="{logo}"{chno} group-title="{group}",{name}{tags}\n{url}\n\n'.encode('utf8'))

    def _epg(self):
        url = EPG_URL.format(region=REGION)

        if _cache_fresh(EPG_CACHE_PATH):
            self.log_message(f"Cache hit: {url}")
        else:
            self.log_message(f"Downloading {url}...")
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            _atomic_write(EPG_CACHE_PATH, gzip.decompress(resp.content))

        # Serve from the (now guaranteed fresh + complete) cache file only.
        # Headers are sent only once the download has fully succeeded, so a
        # failed download never leaves a half-written response on the wire.
        self.send_response(200)
        self.send_header('Content-Type', 'application/xml')
        self.end_headers()
        with open(EPG_CACHE_PATH, 'rb') as f:
            chunk = f.read(CHUNKSIZE)
            while chunk:
                self.wfile.write(chunk)
                chunk = f.read(CHUNKSIZE)

    def _status(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

        host = self.headers.get('Host')
        self.wfile.write(f'''
            <html>
            <head>
                <title>IPTV AU for Docker</title>
            </head>
            <body>
                Playlist URL: <b><a href="http://{host}/{PLAYLIST_PATH}">http://{host}/{PLAYLIST_PATH}</a></b><br>
                TvHeadend Playlist URL: <b><a href="http://{host}/{TVH_PLAYLIST_PATH}">http://{host}/{TVH_PLAYLIST_PATH}</a></b><br>
                EPG URL (Set to refresh once per hour): <b><a href="http://{host}/{EPG_PATH}">http://{host}/{EPG_PATH}</a></b></body></html>
        '''.encode('utf8'))


class ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
    pass


def run():
    if os.getenv('IS_DOCKER'):
        PORT = 80
    else:
        parser = argparse.ArgumentParser(description="IPTV AU for Docker")
        parser.add_argument("-port", "--PORT", default=80, help="Port number for server to use (optional)")
        args = parser.parse_args()
        PORT = args.PORT

    print(f"Starting server on port {PORT}")
    server = ThreadingSimpleServer(('0.0.0.0', int(PORT)), Handler)
    server.serve_forever()


if __name__ == '__main__':
    run()
