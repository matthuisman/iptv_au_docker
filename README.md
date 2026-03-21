# IPTV AU for Docker

Australian free-to-air IPTV proxy. Serves an M3U8 playlist and EPG from [i.mjh.nz](https://i.mjh.nz).

## Run

```yaml
services:
  iptv-au:
    image: matthuisman/iptv-au
    environment:
      - REGION=all  # or: nsw, vic, qld, sa, wa, tas, nt, act
    ports:
      - 8183:80
    restart: unless-stopped
```

## Endpoints

| URL | Description |
|-----|-------------|
| `http://host:8183/playlist.m3u8` | M3U8 playlist |
| `http://host:8183/tvh.m3u8` | TvHeadend playlist (ffmpeg pipe) |
| `http://host:8183/epg.xml` | XMLTV EPG |
| `http://host:8183/clear_cache` | Clear cached data |

## Playlist query params

| Param | Example | Description |
|-------|---------|-------------|
| `sort` | `sort=name` | Sort by name (default: channel number) |
| `start_chno` | `start_chno=1000` | Override starting channel number |
| `include` | `include=iptv-au-abc1\|iptv-au-sbs` | Whitelist channels by ID |
| `exclude` | `exclude=iptv-au-abc1\|iptv-au-sbs` | Blacklist channels by ID |

## Notes

- Cache defaults to 5 minutes, override with `CACHE_TIME` env var
- DRM/licensed channels are excluded automatically
- Channels are grouped by state (e.g. `AU| VIC`, `AU| NSW`) or `AU| FREE TO AIR` for national streams
