# Atrium

**One door to everything on your local network.**

Every service on a home server gets its own port — `:4321` here, `:8600` there,
`:32400` for the thing that only accepts `web` as a path. Nobody remembers port
numbers, so nobody uses half of it.

Atrium is a single process on **port 80** that fixes that. Open the machine's
address and you get a board of everything it serves. Open
`http://<machine>/zingy` and you land on whatever is running on port 4321.

```
http://192.168.68.129/          →  a board of every service, with live status
http://192.168.68.129/zingy     →  http://192.168.68.129:4321/
http://192.168.68.129/plex      →  http://192.168.68.129:32400/web/
http://192.168.68.129/nothing   →  404, with a link back to the board
```

Named hosts are left alone: a request for `rasoi.mac` is passed straight through
to an upstream, so existing vhosts keep working exactly as they did.

One config file. No dependencies — the Python standard library and nothing else.

---

## Why a redirect instead of a proxy

Atrium answers with a `302` and lets the browser talk to the app directly. That
is deliberate:

- **No rewriting.** The app sees its own origin and its own port. No
  base-path configuration, no broken absolute URLs, no asset paths to patch.
- **No new bottleneck.** Static assets and uploads go straight to the app rather
  than through a proxy.
- **Debuggable.** The address bar tells you the real port, which is exactly what
  you want when something is misbehaving.

The cost is that the URL changes after you land, and a deep link inside an app
will not survive being shared as a `/slug/...` path. For a home network that is
the right trade.

---

## How a request is routed

Atrium asks one question first: **is this request addressed by name or by
address?**

| Request | Behaviour |
| --- | --- |
| `Host` is an IP, `localhost`, or `<host>.local` | Atrium answers: board and `/slug` redirects |
| `Host` is any other name | Passed through to `upstream` (nginx), untouched |
| Path is `/` | The board |
| Path starts with a configured slug | `302` to `host:port`, remainder of the path appended |
| Path is anything else | `404` with a link to the board |

So `http://192.168.68.129/zingy/info` → `http://192.168.68.129:4321/info`,
query string included, and `http://rasoi.mac/` still reaches the Rasoi that
nginx has always served.

---

## Quick start

```bash
git clone https://github.com/ayaanngandhi/atrium.git
cd atrium
cp config.example.json config.json     # or edit the shipped one
python3 atrium.py --check              # validate, print the routing table
sudo bash deploy/install.sh            # systemd unit, starts on :80
```

Without installing anything, a port above 1024 needs no privileges:

```bash
python3 -c "
import json; c=json.load(open('config.json')); c['listen']['port']=8080
json.dump(c, open('/tmp/atrium-dev.json','w'))"
ATRIUM_CONFIG=/tmp/atrium-dev.json python3 atrium.py
```

---

## config.json

```json
{
  "title": "Atrium",
  "advertise": "192.168.68.129",
  "listen": { "host": "0.0.0.0", "port": 80 },
  "upstream": "http://127.0.0.1:8081",
  "board_hosts": ["192.168.68.129", "localhost", "127.0.0.1"],
  "passthrough_hosts": ["rasoi.mac"],
  "routes": [
    { "slug": "zingy", "port": 4321, "name": "Zingy", "note": "Video downloader" },
    { "slug": "plex",  "port": 32400, "path": "/web", "name": "Plex", "note": "Movies" }
  ]
}
```

| Key | Meaning |
| --- | --- |
| `title` | Wordmark on the board |
| `advertise` | The address shown in the header. Left out, it is detected. |
| `listen.host` / `listen.port` | Where Atrium binds. `80` by default. |
| `upstream` | Where named hosts are passed through. `null` to disable. |
| `board_hosts` | Extra names that should get the board. IPs and `.local` always do. |
| `passthrough_hosts` | Extra names that must go to the upstream even if they look local. |
| `routes[].slug` | The path. `/zingy` for `"zingy"`. Case-insensitive, must be unique. |
| `routes[].port` | The port it forwards to on the same host. Required. |
| `routes[].path` | A prefix to append, for apps that live under one — `/web`, `/admin`. |
| `routes[].name` | Label on the board. Defaults to the slug. |
| `routes[].note` | One line of description on the board. |
| `routes[].scheme` | `http` (default) or `https`. |

**The config is re-read when the file changes.** Add a route, save, refresh the
page. No restart.

### Adding a service

Start the app, confirm the port answers, then append a route:

```json
{ "slug": "jellyfin", "port": 8096, "name": "Jellyfin", "note": "Films and TV" }
```

`http://192.168.68.129/jellyfin` now works, and the board picks it up with a
live status dot. Nothing else to configure, no restart, no vhost.

---

## The board

The header shows the machine's address and how many services it publishes. Each
row is one route: a status LED (green if the port accepted a TCP connection in
the last few seconds, red if not), the name, the description, the path, and the
port it lands on. The footer shows the config path and how many are up.

Status is a real TCP connect to `127.0.0.1:<port>`, cached for four seconds — a
red dot means the process genuinely is not listening, not that it returned an
unhappy status code.

---

## Running it as a service

Port 80 is privileged, so `deploy/atrium.service` grants the one capability it
needs and runs the process as an ordinary user:

```ini
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
```

```bash
sudo bash deploy/install.sh          # install + enable + start
sudo systemctl restart atrium        # after changing code
journalctl -u atrium -f              # logs
```

`install.sh` is idempotent — re-run it after a pull. Logs also go to
`atrium.log` next to the script.

---

## Living with an existing web server

If something already owns port 80 (nginx, caddy, apache), move it to a private
port and point `upstream` at it. Atrium becomes the front door and the existing
server keeps its configuration, its vhosts and its behaviour — it just stops
being the thing the network talks to first.

```nginx
# /etc/nginx/sites-available/mysite.conf
server {
    listen 8081;                 # was: listen 80
    server_name mysite.mac;
    location / { proxy_pass http://127.0.0.1:3000; }
}
```

```json
{ "upstream": "http://127.0.0.1:8081" }
```

`http://mysite.mac/` still works, unchanged, and now `http://<ip>/slug` works
alongside it.

---

## Layout

```
atrium/
├── atrium.py              the server — stdlib only, no build step
├── config.json            the only file you edit
├── deploy/atrium.service  systemd unit with CAP_NET_BIND_SERVICE
├── deploy/install.sh      validate, install, enable, start, verify
└── config.example.json    a starting point, no real services
```

---

## Limits, stated plainly

- **One host.** Routes forward to ports on the machine running Atrium. It is a
  front door for one box, not a network-wide reverse proxy. Forward to another
  host by giving it a `scheme` and adjusting `port` — or just use nginx.
- **Redirects, not rewrites.** A client that does not follow redirects (some
  CLI tools, some fetch clients) will see the `302` and stop there.
- **No TLS.** It speaks plain HTTP on port 80, as a LAN front door should.
- **No auth.** Anything on the network can see the board. It shows service names
  and ports, nothing more — but do not add secrets to `note`.

---

## License

MIT — see [LICENSE](LICENSE).
