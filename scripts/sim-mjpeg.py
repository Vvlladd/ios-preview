#!/usr/bin/env python3
"""
Interactive iOS simulator preview server for any iOS app.

Uses `axe` (bundled by XcodeBuildMCP) for the video feed and input synthesis:
the preview pane shows the booted simulator's screen and forwards browser
clicks/drags as real simulator taps and swipes. Also streams the app's Swift
os.Logger output via SSE for a side-by-side log panel.

Routes:
  /             HTML page (img + log pane + click/drag JS + SSE)
  /stream       MJPEG video; axe stream-video stdout piped raw to socket
  /logs/stream  Server-Sent Events stream of simctl log output
  POST /tap     JSON {x, y} (0-1 normalized) -> axe tap
  POST /swipe   JSON {fromX, fromY, toX, toY} (0-1) -> axe swipe
  POST /key     JSON {key: "home"|"lock"|"siri"|"side-button"|"apple-pay"} -> axe button

Env:
  PORT              HTTP port (default 8765)
  FPS               stream fps 1-30 (default 12)
  QUALITY           JPEG quality 1-100 (default 55)
  SCALE             video scale 0.1-1.0 (default 0.75; lower = smaller frames = less lag)
  IOS_SIM_UDID      simulator UDID (set by detect.sh; falls back to SIM then booted)
  SIM               legacy alias for IOS_SIM_UDID
  AXE               path to axe binary (default: search XcodeBuildMCP install)
  IOS_PRODUCT_NAME  product/process name for log predicate
  IOS_LOG_SUBSYSTEM optional subsystem filter for log predicate
  LOG_PREDICATE     full NSPredicate override (bypasses IOS_PRODUCT_NAME)
  LOG_LEVEL         simctl log stream level (default: debug)
"""
import atexit
import glob
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(os.environ.get("PORT", "8765"))
FPS = int(os.environ.get("FPS", "12"))
QUALITY = int(os.environ.get("QUALITY", "55"))
try:
    SCALE = max(0.1, min(1.0, float(os.environ.get("SCALE", "0.75"))))
except ValueError:
    SCALE = 0.75
LOG_LEVEL = os.environ.get("LOG_LEVEL", os.environ.get("IOS_LOG_LEVEL", "debug"))

# S2: allowed key names for /key route (match axe button types)
ALLOWED_KEYS = {"home", "lock", "siri", "side-button", "apple-pay"}


def build_predicate() -> str:
    """Build the NSPredicate string for simctl log stream.

    Priority:
      1. LOG_PREDICATE env var (full override, passed through verbatim)
      2. IOS_PRODUCT_NAME (+ optional IOS_LOG_SUBSYSTEM) -- both escaped
      3. Empty product: warn to stderr + broad fallback (all log events)
    """
    # Full override
    override = os.environ.get("LOG_PREDICATE", "")
    if override:
        return override

    product = os.environ.get("IOS_PRODUCT_NAME", "")
    if not product:
        print(
            "sim-mjpeg.py: IOS_PRODUCT_NAME not set; using broad log predicate (all processes).",
            file=sys.stderr,
        )
        return 'eventType == "logEvent"'

    # S1: escape backslash first, then double-quote to prevent predicate injection
    escaped_product = product.replace("\\", "\\\\").replace('"', '\\"')

    subsystem = os.environ.get("IOS_LOG_SUBSYSTEM", "")
    if subsystem:
        escaped_sub = subsystem.replace("\\", "\\\\").replace('"', '\\"')
        return f'process == "{escaped_product}" AND subsystem == "{escaped_sub}"'

    return f'process == "{escaped_product}"'


LOG_PREDICATE = build_predicate()


def find_axe() -> str:
    """Locate the axe binary with validation. (S5)

    Priority:
      1. AXE env var -- if set and is a file, validate and use it
      2. axe on PATH
      3. Glob ~/.npm/_npx/*/node_modules/xcodebuildmcp/bundled/axe

    On >1 candidate from glob: sort newest-by-mtime, warn to stderr listing all.
    Validate executable bit and Mach-O magic bytes. Warns but does not abort on
    magic check failure (handles wrapper scripts).
    """
    MACHO_MAGIC = {
        b"\xfe\xed\xfa\xce",  # 32-bit big-endian
        b"\xce\xfa\xed\xfe",  # 32-bit little-endian
        b"\xfe\xed\xfa\xcf",  # 64-bit big-endian
        b"\xcf\xfa\xed\xfe",  # 64-bit little-endian
        b"\xca\xfe\xba\xbe",  # fat binary
    }

    def _validate(path: str, label: str) -> str:
        if not os.access(path, os.X_OK):
            print(
                f"sim-mjpeg.py: {label} '{path}' is not executable.",
                file=sys.stderr,
            )
        else:
            try:
                with open(path, "rb") as fh:
                    magic = fh.read(4)
                if magic not in MACHO_MAGIC:
                    print(
                        f"sim-mjpeg.py: {label} '{path}' does not look like a Mach-O binary "
                        f"(magic={magic.hex()}); using anyway.",
                        file=sys.stderr,
                    )
            except OSError as exc:
                print(
                    f"sim-mjpeg.py: could not read {label} '{path}': {exc}",
                    file=sys.stderr,
                )
        return path

    # 1. Explicit AXE env var
    env_axe = os.environ.get("AXE", "")
    if env_axe:
        if os.path.isfile(env_axe):
            return _validate(env_axe, "AXE env")
        print(
            f"sim-mjpeg.py: AXE='{env_axe}' not found; falling back to PATH search.",
            file=sys.stderr,
        )

    # 2. axe on PATH
    on_path = shutil.which("axe")
    if on_path:
        return _validate(on_path, "PATH axe")

    # 3. npx cache glob
    candidates = glob.glob(
        os.path.expanduser(
            "~/.npm/_npx/*/node_modules/xcodebuildmcp/bundled/axe"
        )
    )
    if not candidates:
        sys.exit(
            "axe binary not found. Install XcodeBuildMCP "
            "(npx -y xcodebuildmcp@1.15.1) or set AXE=/path/to/axe."
        )

    # Sort newest-by-mtime first
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    if len(candidates) > 1:
        print(
            "sim-mjpeg.py: multiple axe candidates found; using newest. All candidates:",
            file=sys.stderr,
        )
        for c in candidates:
            print(f"  {c}", file=sys.stderr)

    return _validate(candidates[0], "npx-cache axe")


def find_sim() -> str:
    """Resolve simulator UDID.

    Priority: IOS_SIM_UDID (set by detect.sh) -> SIM (legacy) -> first booted.
    """
    udid = os.environ.get("IOS_SIM_UDID", "") or os.environ.get("SIM", "")
    if udid:
        return udid
    out = subprocess.check_output(
        ["xcrun", "simctl", "list", "devices", "booted"], text=True
    )
    m = re.search(r"\(([0-9A-F-]{36})\) \(Booted\)", out)
    if not m:
        sys.exit("No booted simulator. `xcrun simctl boot <udid>` first.")
    return m.group(1)


AXE = find_axe()
SIM = find_sim()


def screen_size_points() -> tuple:
    """Query the top-level a11y frame to learn point dimensions (e.g. 402x874)."""
    try:
        raw = subprocess.check_output(
            [AXE, "describe-ui", "--udid", SIM],
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        tree = json.loads(raw)
        root = tree[0] if isinstance(tree, list) else tree
        f = root.get("frame", {})
        w, h = int(f.get("width", 0)), int(f.get("height", 0))
        if w > 0 and h > 0:
            return w, h
    except Exception as e:
        print(f"sim-mjpeg.py: describe-ui failed ({e}); falling back to 402x874", file=sys.stderr)
    return 402, 874


W_PTS, H_PTS = screen_size_points()


HTML = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sim — interactive</title>
<style>
  html,body{{margin:0;background:#111;color:#ddd;font:13px -apple-system,Helvetica,Arial;height:100%;overflow:hidden;}}
  #wrap{{display:flex;height:100vh;width:100vw;}}
  #sim{{flex:0 0 auto;display:flex;align-items:center;justify-content:center;background:#000;padding:8px;}}
  #screen{{max-height:calc(100vh - 16px);max-width:100%;display:block;cursor:crosshair;user-select:none;-webkit-user-drag:none;touch-action:none;border-radius:24px;}}
  #logs{{flex:1 1 auto;display:flex;flex-direction:column;border-left:1px solid #222;min-width:0;}}
  #logbar{{display:flex;gap:6px;padding:6px;background:#161616;border-bottom:1px solid #222;}}
  #logbar input{{flex:1;background:#0c0c0c;color:#ddd;border:1px solid #2a2a2a;padding:4px 8px;border-radius:4px;font:12px ui-monospace,Menlo,monospace;}}
  #logbar button{{background:#2a2a2a;color:#ddd;border:0;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px;}}
  #logbar button:hover{{background:#3a3a3a;}}
  #logbody{{flex:1;overflow-y:auto;font:11.5px/1.4 ui-monospace,Menlo,monospace;padding:6px 8px;white-space:pre-wrap;word-break:break-all;}}
  .ll{{padding:1px 0;}}
  .ll.hide{{display:none;}}
  .ll.err{{color:#ff8a80;}}
  .ll.warn{{color:#ffcc80;}}
  .ll.dim{{color:#777;}}
  #hud{{position:fixed;top:10px;left:10px;background:rgba(0,0,0,.65);padding:3px 8px;border-radius:6px;pointer-events:none;font-size:11px;}}
  #status{{align-self:center;white-space:nowrap;flex:0 0 auto;padding:3px 8px;border-radius:6px;font-size:11px;color:#9c9;}}
  #logbody .empty{{color:#666;font-style:italic;padding:6px 0;}}
</style>
</head><body>
<div id="hud">sim {W_PTS}x{H_PTS}pt · click=tap · drag=swipe</div>
<div id="wrap">
  <div id="sim"><img id="screen" src="/stream" alt="sim"/></div>
  <div id="logs">
    <div id="logbar">
      <input id="filter" placeholder="filter (substring; case-insensitive)"/>
      <button id="pause">Pause</button>
      <button id="clear">Clear</button>
      <span id="status">connecting…</span>
    </div>
    <div id="logbody"><div class="empty">Waiting for logs… interact with the app, or set IOS_LOG_SUBSYSTEM if your app uses a custom os.Logger subsystem.</div></div>
  </div>
</div>
<script>
const W = {W_PTS}, H = {H_PTS};
const img = document.getElementById('screen');
const status = document.getElementById('status');

/* ---- click / drag -> tap / swipe ---- */
function norm(e) {{
  const r = img.getBoundingClientRect();
  return {{
    x: Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)),
    y: Math.max(0, Math.min(1, (e.clientY - r.top) / r.height))
  }};
}}
async function post(path, body) {{
  try {{ await fetch(path, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(body)}}); }}
  catch(e) {{ console.warn(e); }}
}}
let down = null;
const SWIPE_THRESHOLD = 6;
img.addEventListener('pointerdown', e => {{
  e.preventDefault();
  down = {{...norm(e), startX: e.clientX, startY: e.clientY}};
  img.setPointerCapture(e.pointerId);
}});
img.addEventListener('pointerup', e => {{
  if (!down) return;
  const dx = e.clientX - down.startX, dy = e.clientY - down.startY;
  const up = norm(e);
  if (Math.hypot(dx, dy) < SWIPE_THRESHOLD) post('/tap', up);
  else post('/swipe', {{fromX: down.x, fromY: down.y, toX: up.x, toY: up.y}});
  down = null;
}});
img.addEventListener('pointercancel', () => {{ down = null; }});

/* ---- log stream via SSE ---- */
const logbody = document.getElementById('logbody');
const filterInput = document.getElementById('filter');
const pauseBtn = document.getElementById('pause');
const clearBtn = document.getElementById('clear');
let paused = false;
const MAX_LINES = 600;
let bufferOverflow = [];

function lineClass(line) {{
  const tag = line.slice(24, 26);
  if (tag.startsWith('E') || tag.startsWith('F')) return 'll err';
  if (tag.startsWith('W')) return 'll warn';
  if (tag.startsWith('Db')) return 'll dim';
  return 'll';
}}

function applyFilter() {{
  const q = filterInput.value.toLowerCase();
  for (const el of logbody.children) {{
    el.classList.toggle('hide', !!q && !el.textContent.toLowerCase().includes(q));
  }}
}}

function append(line) {{
  const empty = logbody.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = lineClass(line);
  div.textContent = line;
  const q = filterInput.value.toLowerCase();
  if (q && !line.toLowerCase().includes(q)) div.classList.add('hide');
  logbody.appendChild(div);
  while (logbody.children.length > MAX_LINES) logbody.removeChild(logbody.firstChild);
  if (logbody.scrollTop + logbody.clientHeight > logbody.scrollHeight - 50) {{
    logbody.scrollTop = logbody.scrollHeight;
  }}
}}

filterInput.addEventListener('input', applyFilter);
pauseBtn.addEventListener('click', () => {{
  paused = !paused;
  pauseBtn.textContent = paused ? 'Resume' : 'Pause';
  if (!paused && bufferOverflow.length) {{
    for (const l of bufferOverflow) append(l);
    bufferOverflow = [];
  }}
}});
clearBtn.addEventListener('click', () => {{ logbody.innerHTML = ''; bufferOverflow = []; }});

function openSse() {{
  const es = new EventSource('/logs/stream');
  es.onopen = () => {{ status.textContent = 'logs live'; status.style.color = '#9c9'; }};
  es.onerror = () => {{
    status.textContent = 'logs reconnecting';
    status.style.color = '#fc9';
    es.close();
    setTimeout(openSse, 2000);
  }};
  es.onmessage = e => {{
    let line;
    try {{ line = JSON.parse(e.data); }} catch {{ line = e.data; }}
    if (typeof line === 'string' && line.startsWith('Filtering the log data using')) return;
    if (paused) {{
      bufferOverflow.push(line);
      if (bufferOverflow.length > MAX_LINES) bufferOverflow.shift();
    }} else {{
      append(line);
    }}
  }};
}}
openSse();
</script>
</body></html>"""


def run_axe(args: list) -> tuple:
    try:
        r = subprocess.run(
            [AXE, *args, "--udid", SIM],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.returncode, (r.stderr or r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def pt(coord_norm: float, span: int) -> int:
    return max(0, min(span - 1, int(round(coord_norm * span))))


def _check_origin(handler: "Handler") -> bool:
    """S2: CSRF origin check for POST routes.

    Accept requests where Origin is absent (CLI/curl) or matches
    http://localhost:<PORT> or http://127.0.0.1:<PORT>. Return True if
    allowed, False if forbidden (caller sends 403).
    """
    origin = handler.headers.get("Origin", "")
    if not origin:
        return True
    allowed = {
        f"http://localhost:{PORT}",
        f"http://127.0.0.1:{PORT}",
    }
    return origin in allowed


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args, **kwargs):
        pass

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/logs/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            proc = subprocess.Popen(
                [
                    "xcrun", "simctl", "spawn", SIM, "log", "stream",
                    "--style=compact",
                    "--level", LOG_LEVEL,
                    "--predicate", LOG_PREDICATE,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=1,
                text=True,
            )
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    payload = json.dumps(line)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        elif self.path == "/stream":
            # Lower latency: disable Nagle so each MJPEG chunk ships immediately.
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            proc = subprocess.Popen(
                [
                    AXE, "stream-video",
                    "--udid", SIM,
                    "--format", "mjpeg",
                    "--fps", str(FPS),
                    "--quality", str(QUALITY),
                    "--scale", str(SCALE),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                while True:
                    chunk = proc.stdout.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
        else:
            self.send_response(404)
            self.send_header("Connection", "close")
            self.end_headers()

    def do_POST(self):
        # S2: CSRF Origin check applied to all POST routes
        if not _check_origin(self):
            return self._send_json(403, {"error": "forbidden: cross-origin request"})

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._send_json(400, {"error": "bad json"})

        if self.path == "/tap":
            x = pt(float(body.get("x", 0)), W_PTS)
            y = pt(float(body.get("y", 0)), H_PTS)
            rc, msg = run_axe(["tap", "-x", str(x), "-y", str(y)])
            return self._send_json(
                200 if rc == 0 else 500, {"ok": rc == 0, "x": x, "y": y, "msg": msg}
            )
        if self.path == "/swipe":
            fx = pt(float(body.get("fromX", 0)), W_PTS)
            fy = pt(float(body.get("fromY", 0)), H_PTS)
            tx = pt(float(body.get("toX", 0)), W_PTS)
            ty = pt(float(body.get("toY", 0)), H_PTS)
            rc, msg = run_axe([
                "swipe",
                "--start-x", str(fx), "--start-y", str(fy),
                "--end-x", str(tx), "--end-y", str(ty),
            ])
            return self._send_json(
                200 if rc == 0 else 500, {"ok": rc == 0, "msg": msg}
            )
        if self.path == "/key":
            name = str(body.get("key", "")).lower()
            if not name:
                return self._send_json(400, {"error": "missing key"})
            # S2: allowlist check
            if name not in ALLOWED_KEYS:
                return self._send_json(
                    400, {"error": f"unknown key '{name}'; allowed: {sorted(ALLOWED_KEYS)}"}
                )
            rc, msg = run_axe(["button", name])
            return self._send_json(
                200 if rc == 0 else 500, {"ok": rc == 0, "msg": msg}
            )
        return self._send_json(404, {"error": "unknown route"})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_log_mirror():
    """Spawn one simctl log stream writing lines to our stdout.

    Each SSE client also gets its own simctl spawn (so disconnections do not
    affect this one), but having a persistent mirror means the assistant
    reading the preview's stdout sees the same lines visible in the browser.
    """
    proc = subprocess.Popen(
        [
            "xcrun", "simctl", "spawn", SIM, "log", "stream",
            "--style=compact",
            "--level", LOG_LEVEL,
            "--predicate", LOG_PREDICATE,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=1,
        text=True,
    )

    def _pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()

    threading.Thread(target=_pump, daemon=True).start()

    def _cleanup(*_args):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

    atexit.register(_cleanup)
    signal.signal(signal.SIGTERM, lambda *a: (_cleanup(), sys.exit(0)))
    signal.signal(signal.SIGINT, lambda *a: (_cleanup(), sys.exit(0)))


if __name__ == "__main__":
    print(f"sim-mjpeg.py: axe:  {AXE}", flush=True)
    print(f"sim-mjpeg.py: sim:  {SIM}  (screen {W_PTS}x{H_PTS} pt)", flush=True)
    print(f"sim-mjpeg.py: http://localhost:{PORT}/  (mjpeg {FPS}fps q={QUALITY} scale={SCALE})", flush=True)
    print(f"sim-mjpeg.py: log predicate: {LOG_PREDICATE}", flush=True)
    start_log_mirror()
    # R12: catch OSError on bind (port already in use)
    try:
        ThreadedHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except OSError:
        print(
            f"Port {PORT} in use; set PORT=<free> or run /ios-preview:stop.",
            file=sys.stderr,
        )
        sys.exit(1)
