#!/usr/bin/python3
import os
import json
import gzip
import argparse
from base64 import b64encode
from io import BytesIO
from tempfile import gettempdir
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from base64 import b64encode, b64decode
from urllib.parse import urlparse, parse_qsl, urlencode, unquote, parse_qs

import requests
from cachelib import SimpleCache


PLAYLIST_PATH = 'playlist.m3u8'
EPG_PATH = 'epg.xml'
CLEAR_CACHE_PATH = 'clear_cache'
DEEPLINK_PATH = 'deep_link'
STATUS_PATH = ''
APP_URL = 'https://i.mjh.nz/au/{region}/tv.json.gz'
EPG_URL = 'https://i.mjh.nz/au/{region}/epg.xml.gz'
DELIMITER = '|'
TIMEOUT = (5,20) #connect,read
CACHE_TIME = os.getenv("CACHE_TIME", 300) # default of 5mins
CHUNKSIZE = 1024
REGION = os.environ.get('REGION', 'all')

CACHE_DIR = os.path.join(gettempdir(), 'iptv-au-docker')
os.makedirs(CACHE_DIR, exist_ok=True)
print(f"Cache dir: {CACHE_DIR}")
cache = SimpleCache()


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
        super().__init__(*args, **kwargs)

    def _error(self, message):
        self.send_response(500)
        self.end_headers()
        self.wfile.write(f'Error: {message}'.encode('utf8'))
        raise

    def do_GET(self):
        # Serve the favicon.ico file
        if self.path == '/favicon.ico':
            self._serve_favicon()
            return

        routes = {
            PLAYLIST_PATH: self._playlist,
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
        except Exception as e:
            self._error(e)

    def _deeplink(self):
        plugin_url = '/'.join(self.path.split('/')[2:])
        parsed = urlparse(plugin_url)
        params = dict(parse_qsl(parsed.query))
        access_token = requests.get('https://i.mjh.nz/.tokens/9now.tk').text

        query = {
            'device': 'web',
            'streamParams': 'web,chrome,windows',
            'region': params['region'],
            'offset': 0,
        }
        data = requests.get('https://api.9now.com.au/ctv/livestreams', params=query, headers={'Authorization': f'Bearer {access_token}'}).json()['data']['getLivestream']
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
        cache.clear()
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'Cache cleared')

    def _serve_favicon(self):
        # Serve the favicon file as an ICO file
        try:
            with open('favicon.ico', 'rb') as f:
                self.send_response(200)
                self.send_header('Content-Type', 'image/x-icon')
                self.end_headers()
                self.wfile.write(f.read())
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def _app_data(self):
        app_url = APP_URL.format(region=REGION)
        cache_path = cache.get(app_url)
        if cache_path and os.path.exists(cache_path):
            self.log_message(f"Cache hit: {app_url}")
            with open(cache_path, 'r') as f:
                return json.load(f)

        cache_path = os.path.join(CACHE_DIR, b64encode(app_url.encode()).decode())
        self.log_message(f"Downloading {app_url}...")
        resp = requests.get(app_url, stream=True, timeout=TIMEOUT)
        resp.raise_for_status()
        json_text = gzip.GzipFile(fileobj=BytesIO(resp.content)).read()
        data = json.loads(json_text)
        with open(cache_path, 'w') as f:
            json.dump(data, f)
        cache.set(app_url, cache_path, timeout=CACHE_TIME)
        return data

    def _playlist(self):
        channels = self._app_data()

        start_chno = int(self._params['start_chno']) if 'start_chno' in self._params else None
        sort = self._params.get('sort', 'chno')
        include = [x for x in self._params.get('include', '').split(DELIMITER) if x]
        exclude = [x for x in self._params.get('exclude', '').split(DELIMITER) if x]

        self.send_response(200)
        self.send_header('content-type', 'vnd.apple.mpegurl')
        self.end_headers()

        host = self.headers.get('Host')

        self.wfile.write(b'#EXTM3U\n')
        for key in sorted(channels.keys(), key=lambda x: channels[x].get('chno', 9999999) if sort == 'chno' else channels[x]['name'].strip().lower()):
            channel = channels[key]
            logo = channel['logo']
            name = channel['name']
            url = channel['mjh_master']
            channel_id = f'iptv-au-{key}'

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

            # Write channel information
            self.wfile.write(f'#EXTINF:-1 channel-id="{channel_id}" tvg-id="{key}" tvg-logo="{logo}"{chno},{name}\n{url}\n'.encode('utf8'))

    def _epg(self):
        url = EPG_URL.format(region=REGION)

        cache_path = cache.get(url)
        if cache_path and os.path.exists(cache_path):
            self.log_message(f"Cache hit: {url}...")
            self.send_response(200)
            self.send_header('Content-Type', 'application/xml')
            self.end_headers()
            with open(cache_path, 'rb') as f:
                chunk = f.read(CHUNKSIZE)
                while chunk:
                    self.wfile.write(chunk)
                    chunk = f.read(CHUNKSIZE)
            return

        self.log_message(f"Downloading {url}...")
        cache_path = os.path.join(CACHE_DIR, b64encode(url.encode()).decode())
        # Download the .gz EPG file
        with open(cache_path, 'wb') as cache_f:
            with requests.get(url, stream=True, timeout=TIMEOUT) as resp:
                resp.raise_for_status()

                self.send_response(200)
                self.send_header('Content-Type', 'application/xml')
                self.end_headers()

                # Decompress the .gz content
                with gzip.GzipFile(fileobj=BytesIO(resp.content)) as gz:
                    chunk = gz.read(CHUNKSIZE)
                    while chunk:
                        cache_f.write(chunk)
                        self.wfile.write(chunk)
                        chunk = gz.read(CHUNKSIZE)
        cache.set(url, cache_path, timeout=CACHE_TIME)

    def _status(self):
        # Generate HTML content with the favicon link
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.end_headers()

        host = self.headers.get('Host')
        self.wfile.write(f'''
            <html>
            <head>
                <title>IPTV AU for Docker</title>
                <link rel="icon" href="/favicon.ico" type="image/x-icon">
            </head>
            <body>
                Playlist URL: <b><a href="http://{host}/{PLAYLIST_PATH}">http://{host}/{PLAYLIST_PATH}</a></b><br>
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
