#!/usr/bin/env python3
"""
Atrium — the entry hall for a local network.

One process on port 80. It answers at the root with a directory board of every
service on this machine, and sends /<slug> to the port that slug is configured
for. Anything addressed by a real hostname (rasoi.mac, stremio.mac, ...) is
passed straight through to the existing upstream so named vhosts keep working
exactly as they always have.

Standard library only. No dependencies, no build step, no database.

    python3 atrium.py                 # serve
    python3 atrium.py --check         # validate config.json and exit
    ATRIUM_CONFIG=/path/x.json python3 atrium.py
"""

import http.client
import ipaddress
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("ATRIUM_CONFIG") or os.path.join(HERE, "config.json")

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}

# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

_config = None
_config_mtime = 0.0
_config_lock = threading.Lock()


def local_addresses():
    """Every address this box answers to, so the board recognises itself."""
    addrs = {"localhost", "127.0.0.1", "::1", socket.gethostname().lower()}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addrs.add(info[4][0].lower())
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.68.1", 1))  # no packet is sent; picks the LAN iface
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    addrs.discard("")
    return addrs


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    listen = cfg.setdefault("listen", {})
    listen.setdefault("host", "0.0.0.0")
    listen.setdefault("port", 80)

    routes = cfg.setdefault("routes", [])
    if not isinstance(routes, list):
        raise ValueError("config.routes must be a list of route objects")

    seen = set()
    by_slug = {}
    for r in routes:
        slug = str(r.get("slug", "")).strip().lower().lstrip("/")
        if not slug:
            raise ValueError("every route needs a slug")
        if slug in seen:
            raise ValueError(f"duplicate slug: {slug}")
        seen.add(slug)
        r["slug"] = slug
        r["port"] = int(r["port"])
        r.setdefault("name", slug.capitalize())
        r.setdefault("note", "")
        r.setdefault("scheme", "http")
        r.setdefault("path", "")
        if r["path"] and not r["path"].startswith("/"):
            r["path"] = "/" + r["path"]
        by_slug[slug] = r
    cfg["_by_slug"] = by_slug

    board = {h.lower() for h in cfg.get("board_hosts", [])}
    board |= local_addresses()
    cfg["_board_hosts"] = board
    cfg["_passthrough_hosts"] = {h.lower() for h in cfg.get("passthrough_hosts", [])}

    upstream = cfg.get("upstream")
    if upstream:
        u = urlsplit(upstream)
        cfg["_upstream"] = (u.hostname or "127.0.0.1", u.port or 80)
    else:
        cfg["_upstream"] = None
    return cfg


def get_config():
    """Re-read config.json when it changes on disk. No restart needed."""
    global _config, _config_mtime
    try:
        mtime = os.stat(CONFIG_PATH).st_mtime
    except OSError:
        mtime = _config_mtime
    with _config_lock:
        if _config is None or mtime != _config_mtime:
            _config = load_config()
            _config_mtime = mtime
        return _config


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def is_ip(value):
    try:
        ipaddress.ip_address(value.strip("[]"))
        return True
    except ValueError:
        return False


def is_board_host(hostname, cfg):
    h = (hostname or "").lower().strip("[]")
    if not h:
        return True
    if h in cfg["_passthrough_hosts"]:
        return False
    if h in cfg["_board_hosts"]:
        return True
    # An address with no name of its own is the front door; a named host
    # belongs to whatever vhost already answers for it.
    return is_ip(h) or h.endswith(".local") or h in ("localhost",)


_status_cache = {}
_status_lock = threading.Lock()


def port_open(port, host="127.0.0.1", timeout=0.35):
    key = (host, port)
    now = time.time()
    with _status_lock:
        hit = _status_cache.get(key)
        if hit and now - hit[1] < 4:
            return hit[0]
    try:
        with socket.create_connection((host, port), timeout=timeout):
            up = True
    except OSError:
        up = False
    with _status_lock:
        _status_cache[key] = (up, now)
    return up


# --------------------------------------------------------------------------
# the board
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ · __ADDR__</title>
<style>
  :root {
    --panel: #1a1d1f;
    --panel-edge: #0c0e0f;
    --silk: #d5dade;
    --silk-dim: #79838b;
    --hair: #2b3135;
    --socket: #111416;
    --up: #3fd07a;
    --down: #b4402f;
    --amber: #d9a441;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: var(--panel-edge);
    color: var(--silk);
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    padding: clamp(20px, 4vw, 56px);
    display: flex;
    justify-content: center;
  }
  main { width: 100%; max-width: 780px; }

  header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 26px;
  }
  h1 {
    margin: 0;
    font-size: clamp(2.5rem, 11vw, 4.4rem);
    line-height: 0.86;
    font-weight: 700;
    letter-spacing: -0.045em;
    color: #eef1f3;
    text-shadow: 0 1px 0 #000, 0 -1px 0 rgba(255,255,255,.14);
  }
  .addr {
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.74rem;
    letter-spacing: 0.04em;
    color: var(--silk-dim);
    text-align: right;
    line-height: 1.7;
  }
  .addr b { color: var(--amber); font-weight: 500; }

  .panel {
    list-style: none;
    margin: 0;
    padding: 10px;
    border-radius: 3px;
    background: linear-gradient(180deg, #212528, #191c1e);
    box-shadow: inset 0 1px 0 rgba(255,255,255,.07),
                inset 0 -1px 0 rgba(0,0,0,.6),
                0 22px 44px -28px #000;
  }
  .panel li + li { border-top: 1px solid var(--hair); }

  a.row {
    display: grid;
    grid-template-columns: 26px minmax(0, 1fr) auto 62px;
    align-items: center;
    gap: 14px;
    padding: 13px 10px 13px 8px;
    text-decoration: none;
    color: inherit;
    border-radius: 2px;
    transition: background .12s ease-out;
  }
  a.row:hover, a.row:focus-visible { background: #24292c; outline: none; }
  a.row:focus-visible { box-shadow: inset 0 0 0 2px var(--amber); }

  .socket {
    width: 26px; height: 26px;
    border-radius: 2px;
    background: var(--socket);
    box-shadow: inset 0 2px 5px rgba(0,0,0,.85), 0 1px 0 rgba(255,255,255,.09);
    display: grid;
    place-items: center;
  }
  .led {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--down);
    box-shadow: 0 0 6px -1px currentColor;
  }
  .led.up { background: var(--up); box-shadow: 0 0 7px 0 rgba(63,208,122,.55); }

  .label { min-width: 0; }
  .label .name {
    display: block;
    font-size: 1.02rem;
    font-weight: 600;
    letter-spacing: -0.012em;
    color: #eaeef0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .label .note {
    display: block;
    margin-top: 2px;
    font-size: 0.79rem;
    color: var(--silk-dim);
  }

  .dest {
    text-align: right;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.78rem;
    line-height: 1.5;
    color: var(--silk-dim);
    white-space: nowrap;
  }
  .dest .slug { color: var(--silk); }
  .dest .arrow { color: #4d565d; }
  .dest .port { color: var(--amber); }
  .right { text-align: right; }
  .portcell {
    text-align: right;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.78rem;
    color: var(--amber);
    white-space: nowrap;
  }
  .portcell.dead { color: var(--down); }

  footer {
    margin-top: 22px;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    font-size: 0.71rem;
    color: #7d878e;
    display: flex;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  @media (max-width: 460px) {
    a.row { grid-template-columns: 22px minmax(0, 1fr) 58px; }
    .socket { width: 22px; height: 22px; }
    .dest { grid-column: 2; grid-row: 2; text-align: left; margin-top: -6px; }
    .portcell { grid-column: 3; grid-row: 2; font-size: 0.72rem; }
  }
  @media (prefers-reduced-motion: reduce) {
    a.row { transition: none; }
  }
</style>
</head>
<body>
<main>
  <header>
    <h1>__TITLE__</h1>
    <div class="addr"><b>__ADDR__</b><br>__COUNT__ services on this machine</div>
  </header>
  <ul class="panel">
__ROWS__
  </ul>
  <footer>
    <span>__CONFIG__</span>
    <span>__UP__ up of __COUNT__</span>
  </footer>
</main>
</body>
</html>
"""

ROW = """    <li><a class="row" href="/{slug}" aria-label="{name}, {status}">
      <span class="socket" title="{status}"><span class="led{led}"></span></span>
      <span class="label"><span class="name">{name}</span><span class="note">{note}</span></span>
      <span class="dest"><span class="slug">/{slug}</span> <span class="arrow">&rarr;</span></span>
      <span class="portcell{port_cls}">:{port}</span>
    </a></li>"""


def render_board(cfg, addr):
    rows = []
    up_count = 0
    for r in cfg["routes"]:
        up = port_open(r["port"])
        up_count += 1 if up else 0
        rows.append(ROW.format(
            slug=r["slug"], name=r["name"],
            note=r["note"] or r.get("scheme", "http") + "://" + addr + ":" + str(r["port"]),
            led=" up" if up else "",
            status="up" if up else "not responding",
            port_cls="" if up else " dead",
            port=r["port"],
        ))
    page = (PAGE
            .replace("__TITLE__", cfg.get("title", "Atrium"))
            .replace("__ADDR__", addr)
            .replace("__COUNT__", str(len(cfg["routes"])))
            .replace("__UP__", str(up_count))
            .replace("__CONFIG__", CONFIG_PATH.replace(os.path.expanduser("~"), "~"))
            .replace("__ROWS__", "\n".join(rows)))
    return page


def render_missing(cfg, addr, slug):
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>No route for /{slug}</title>
<style>
 body {{ margin:0; padding: clamp(24px,6vw,64px); background:#0c0e0f; color:#d5dade;
        font-family:"Helvetica Neue",Helvetica,Arial,sans-serif; }}
 h1 {{ font-size:clamp(1.8rem,6vw,2.6rem); letter-spacing:-.03em; margin:0 0 10px; }}
 code {{ font-family:ui-monospace,Menlo,monospace; color:#d9a441; }}
 p {{ color:#79838b; max-width:46ch; line-height:1.65; }}
 a {{ color:#eef1f3; text-decoration:none; border-bottom:1px solid #3a4247; }}
</style></head><body>
<h1>No route for <code>/{slug}</code></h1>
<p>Atrium only forwards the slugs listed in its config. Everything this machine
publishes is on <a href="/">the board</a>.</p>
<p>To add one, append a route to <code>config.json</code> &mdash; it is re-read on
save, no restart needed.</p>
</body></html>"""
    return body


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Atrium"
    sys_version = ""

    # ---- plumbing ------------------------------------------------------

    def log_message(self, fmt, *args):
        if os.environ.get("ATRIUM_DEBUG"):
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _hostname(self):
        raw = self.headers.get("Host", "") or ""
        return raw.split(":")[0].strip().lower()

    def _body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = b""
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    continue
                size = int(line.split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                out += self.rfile.read(size)
                self.rfile.read(2)
            return out
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n > 0 else b""

    # ---- dispatch ------------------------------------------------------

    def do_GET(self):
        self._dispatch()

    do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    def _dispatch(self):
        try:
            cfg = get_config()
        except Exception as exc:                       # noqa: BLE001
            self._send(500, f"Atrium: config.json is not readable or invalid — {exc}")
            return
        if not is_board_host(self._hostname(), cfg):
            self._proxy(cfg)
            return
        self._local(cfg)

    def _local(self, cfg):
        parts = urlsplit(self.path)
        segments = [s for s in parts.path.split("/") if s]
        addr = cfg.get("advertise")
        if not addr:
            ips = [a for a in cfg["_board_hosts"]
                   if is_ip(a) and not a.startswith("127.") and ":" not in a]
            addr = ips[0] if ips else "localhost"

        if not segments:
            self._send(200, render_board(cfg, addr))
            return

        slug = segments[0].lower()
        route = cfg["_by_slug"].get(slug)
        if route is None:
            self._send(404, render_missing(cfg, addr, slug))
            return

        rest = ("/" + "/".join(segments[1:])) if len(segments) > 1 else "/"
        target = "%s://%s:%d%s%s" % (
            route["scheme"], self._hostname() or addr, route["port"], route["path"], rest,
        )
        if parts.query:
            target += "?" + parts.query
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _proxy(self, cfg):
        if not cfg["_upstream"]:
            self._send(502, "Atrium: this host has no upstream configured.")
            return
        host, port = cfg["_upstream"]
        body = self._body()
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP}
        headers["Host"] = self.headers.get("Host", "")
        headers["X-Forwarded-For"] = self.headers.get("X-Forwarded-For",
                                                     self.client_address[0])
        headers["X-Forwarded-Proto"] = "http"
        try:
            conn = http.client.HTTPConnection(host, port, timeout=120)
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        except Exception as exc:                       # noqa: BLE001
            self._send(502, f"Atrium: upstream {host}:{port} did not answer — {exc}")
            return
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv):
    if "--check" in argv:
        cfg = load_config()
        print("config ok: %d routes, listen %s:%s, upstream %s" % (
            len(cfg["routes"]),
            cfg["listen"]["host"], cfg["listen"]["port"],
            cfg.get("upstream") or "none",
        ))
        for r in cfg["routes"]:
            print("  /%-14s -> %s:%d%s  (%s)" % (
                r["slug"], r["scheme"], r["port"], r["path"], r["name"]))
        return 0

    cfg = get_config()
    host, port = cfg["listen"]["host"], int(cfg["listen"]["port"])
    srv = Server((host, port), Handler)
    sys.stderr.write("atrium: serving on http://%s:%d  (%d routes)\n"
                     % (host, port, len(cfg["routes"])))
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
