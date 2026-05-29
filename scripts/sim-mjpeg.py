#!/usr/bin/env python3
"""
Interactive iOS simulator preview server for any iOS app.

Uses `axe` (bundled by XcodeBuildMCP) for the video feed and input synthesis:
the preview pane shows the booted simulator's screen and forwards browser
clicks/drags as real simulator taps and swipes. Also streams the app's Swift
os.Logger output via SSE for a side-by-side log panel.

Routes:
  /             HTML page (img + run/stop bar + log pane + click/drag JS + SSE)
  /stream       MJPEG video; axe stream-video stdout piped raw to socket
  /logs/stream  Server-Sent Events stream of simctl log output
  /status       JSON run-state for the in-pane Run/Stop button
  /sims         JSON list of available iOS simulators (for the sim picker)
  /build/stream Server-Sent Events: build output + run-state changes
  POST /tap     JSON {x, y} (0-1 normalized) -> axe tap
  POST /swipe   JSON {fromX, fromY, toX, toY} (0-1) -> axe swipe
  POST /key     JSON {key: "home"|"lock"|"siri"|"side-button"|"apple-pay"} -> axe button
  POST /run     build + install + launch (run-ios.sh --no-stream) in a background thread
  POST /stop    xcrun simctl terminate the app (and kill an in-flight build)
  POST /sim     JSON {udid} -> boot + switch the live preview to another simulator

Env:
  PORT              HTTP port (default 8765)
  FPS               stream fps 1-30 (default 12)
  QUALITY           JPEG quality 1-100 (default 55)
  SCALE             video scale 0.1-1.0 (default 0.75; lower = smaller frames = less lag)
  IOS_SIM_UDID      simulator UDID (set by detect.sh; falls back to SIM then booted)
  SIM               legacy alias for IOS_SIM_UDID
  AXE               path to axe binary (default: search XcodeBuildMCP install)
  IOS_PRODUCT_NAME  product/process name for log predicate
  IOS_BUNDLE_ID     bundle id for launch/terminate (Run/Stop button)
  IOS_SCHEME        scheme name (shown on the run bar)
  IOS_LOG_SUBSYSTEM optional subsystem filter for log predicate
  IOS_PREVIEW_ALLOW_ORIGIN  extra allowed POST Origin (exact, or "*") for a proxied pane
  LOG_PREDICATE     full NSPredicate override (bypasses IOS_PRODUCT_NAME)
  LOG_LEVEL         simctl log stream level (default: debug)
"""
import atexit
import collections
import glob
import json
import os
import queue
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = int(os.environ.get("PORT", "8765"))
FPS = int(os.environ.get("FPS", "12"))
QUALITY = int(os.environ.get("QUALITY", "55"))
try:
    SCALE = max(0.1, min(1.0, float(os.environ.get("SCALE", "0.75"))))
except ValueError:
    SCALE = 0.75
LOG_LEVEL = os.environ.get("LOG_LEVEL", os.environ.get("IOS_LOG_LEVEL", "debug"))

# Log filter mode for the preview dropdown: "app" (Xcode-console-like, default)
# or "all" (full firehose). IOS_LOG_MODE sets the initial value; IOS_LOG_VERBOSE
# is a legacy alias for "all".
DEFAULT_LOG_MODE = os.environ.get("IOS_LOG_MODE", "").lower()
if DEFAULT_LOG_MODE not in ("app", "all"):
    DEFAULT_LOG_MODE = (
        "all"
        if os.environ.get("IOS_LOG_VERBOSE", "").lower() in ("1", "true", "yes")
        else "app"
    )
_app_selected = " selected" if DEFAULT_LOG_MODE == "app" else ""
_all_selected = " selected" if DEFAULT_LOG_MODE == "all" else ""

# S2: allowed key names for /key route (match axe button types)
ALLOWED_KEYS = {"home", "lock", "siri", "side-button", "apple-pay"}

# ---- Run/Stop ("play button") config ----
# write-launch-json.py bakes IOS_BUNDLE_ID + the build vars into the interactive
# launch.json entry so the in-pane Run/Stop button can build, launch, and
# terminate the app. run-ios.sh lives next to this script.
BUNDLE_ID = os.environ.get("IOS_BUNDLE_ID", "")
SCHEME = os.environ.get("IOS_SCHEME", "")
RUN_IOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run-ios.sh")
# S2: optional escape hatch when the preview pane is proxied under a non-loopback
# origin. Otherwise loopback origins (any port) and a missing Origin are allowed.
ALLOW_ORIGIN = os.environ.get("IOS_PREVIEW_ALLOW_ORIGIN", "")


def build_predicate(mode: str = None) -> str:
    """Build the NSPredicate string for simctl log stream.

    `mode` selects how much to show when no LOG_PREDICATE override and no
    IOS_LOG_SUBSYSTEM is set (the preview dropdown sends ?mode=app|all):
      - "app" (default): the app's own logs, Xcode-console-like -- drop the
        com.apple.* subsystems AND anything emitted by a system framework
        (senderImagePath under /System or /usr/lib). This catches both the
        subsystem'd firehose (boringssl/CFNetwork) and the subsystem-less
        library noise shown as (Security)/(CoreVideo)/(UIKitCore)/...
      - "all": everything the process emits (full firehose).

    Priority: LOG_PREDICATE override > IOS_LOG_SUBSYSTEM > mode.
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

    if mode is None:
        mode = DEFAULT_LOG_MODE
    if mode == "all":
        return f'process == "{escaped_product}"'
    # "app" (default): app's own logs only -- drop com.apple.* subsystems and
    # anything emitted by a system framework (sender under /System or /usr/lib).
    return (
        f'process == "{escaped_product}"'
        ' AND NOT (subsystem BEGINSWITH "com.apple")'
        ' AND NOT (senderImagePath CONTAINS "/System/")'
        ' AND NOT (senderImagePath CONTAINS "/usr/lib/")'
    )


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


def sim_label() -> str:
    """Best-effort 'iPhone 17 · iOS 26.5' label for the resolved sim UDID."""
    try:
        raw = subprocess.check_output(
            ["xcrun", "simctl", "list", "devices", "--json"],
            text=True, stderr=subprocess.DEVNULL, timeout=8,
        )
        for runtime_key, sims in json.loads(raw).get("devices", {}).items():
            for sim in sims:
                if sim.get("udid") == SIM:
                    name = sim.get("name", "")
                    m = re.search(r"SimRuntime\.([A-Za-z]+)-([0-9-]+)$", runtime_key)
                    ver = f"{m.group(1)} {m.group(2).replace('-', '.')}" if m else ""
                    return " · ".join(p for p in (name, ver) if p)
    except Exception:
        pass
    return ""


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Run bar shows the scheme; the device is shown/selected via the sim picker.
_run_title = _esc(SCHEME or BUNDLE_ID or "app")

# SIM / W_PTS / H_PTS are reassigned at runtime when the user picks another
# simulator from the pane. Writes are guarded; reads (run_axe, /stream, /logs)
# read the latest value at call time.
_sim_lock = threading.Lock()


def list_sims() -> list:
    """Available iOS simulators: [{udid, name, os, booted}], booted first."""
    try:
        raw = subprocess.check_output(
            ["xcrun", "simctl", "list", "devices", "--json"],
            text=True, stderr=subprocess.DEVNULL, timeout=8,
        )
    except Exception:
        return []
    out = []
    for runtime_key, sims in json.loads(raw).get("devices", {}).items():
        m = re.search(r"SimRuntime\.([A-Za-z]+)-([0-9-]+)$", runtime_key)
        if not m or m.group(1) != "iOS":
            continue
        osver = f"iOS {m.group(2).replace('-', '.')}"
        for s in sims:
            if not s.get("isAvailable", False):
                continue
            out.append({
                "udid": s.get("udid", ""),
                "name": s.get("name", ""),
                "os": osver,
                "booted": s.get("state") == "Booted",
            })
    out.sort(key=lambda d: (not d["booted"], d["os"], d["name"]))
    return out


def switch_sim(udid: str) -> dict:
    """Switch the live preview to another simulator (boot + re-point streams).

    Validates the UDID against the available list, boots it, updates the streamed
    SIM + screen dimensions, and restarts the stdout log mirror. The /sim route
    then tells the client to reload video + logs so video, logs, taps, and the
    next Run/Stop all follow the newly-selected simulator.
    """
    global SIM, W_PTS, H_PTS
    if not re.match(r"^[0-9A-Fa-f-]{36}$", udid):
        return {"ok": False, "error": "invalid udid"}
    if not any(s["udid"] == udid for s in list_sims()):
        return {"ok": False, "error": "unknown simulator"}
    subprocess.run(["xcrun", "simctl", "boot", udid], capture_output=True, text=True)
    subprocess.run(["open", "-a", "Simulator"], capture_output=True, text=True)
    with _sim_lock:
        SIM = udid
        W_PTS, H_PTS = screen_size_points()
        w, h = W_PTS, H_PTS
    restart_log_mirror()
    return {"ok": True, "udid": udid, "label": sim_label(), "w": w, "h": h}


HTML = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Sim — interactive</title>
<style>
  html,body{{margin:0;background:#111;color:#ddd;font:13px -apple-system,Helvetica,Arial;height:100%;overflow:hidden;}}
  #runbar{{display:flex;align-items:center;gap:10px;height:40px;padding:0 12px;background:#0c0c0c;border-bottom:1px solid #222;box-sizing:border-box;}}
  #runbtn{{background:#2ea043;color:#fff;border:0;padding:5px 14px;border-radius:5px;cursor:pointer;font-size:13px;font-weight:600;}}
  #runbtn:hover{{filter:brightness(1.12);}}
  #runbtn.stop{{background:#d73a49;}}
  #runbtn:disabled{{opacity:.55;cursor:default;}}
  #runstate{{font-size:12px;color:#9c9;}}
  #runtitle{{margin-left:auto;font:11px ui-monospace,Menlo,monospace;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40%;}}
  #runbar select{{background:#2a2a2a;color:#ddd;border:0;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:12px;max-width:240px;flex:0 0 auto;}}
  #runbar select:disabled{{opacity:.55;cursor:default;}}
  #wrap{{display:flex;height:calc(100vh - 40px);width:100vw;}}
  #sim{{flex:0 0 auto;display:flex;align-items:center;justify-content:center;background:#000;padding:8px;}}
  #screen{{max-height:calc(100vh - 56px);max-width:100%;display:block;cursor:crosshair;user-select:none;-webkit-user-drag:none;touch-action:none;border-radius:24px;}}
  #logs{{flex:1 1 auto;display:flex;flex-direction:column;border-left:1px solid #222;min-width:0;}}
  #logbar{{display:flex;gap:6px;padding:6px;background:#161616;border-bottom:1px solid #222;}}
  #logbar input{{flex:1;background:#0c0c0c;color:#ddd;border:1px solid #2a2a2a;padding:4px 8px;border-radius:4px;font:12px ui-monospace,Menlo,monospace;}}
  #logbar button{{background:#2a2a2a;color:#ddd;border:0;padding:4px 10px;border-radius:4px;cursor:pointer;font-size:12px;}}
  #logbar select{{background:#2a2a2a;color:#ddd;border:0;padding:4px 6px;border-radius:4px;cursor:pointer;font-size:12px;flex:0 0 auto;}}
  #logbar button:hover{{background:#3a3a3a;}}
  #logbody{{flex:1;overflow-y:auto;font:11.5px/1.4 ui-monospace,Menlo,monospace;padding:6px 8px;white-space:pre-wrap;word-break:break-all;}}
  .ll{{padding:1px 0;}}
  .ll.hide{{display:none;}}
  .ll.err{{color:#ff8a80;}}
  .ll.warn{{color:#ffcc80;}}
  .ll.dim{{color:#777;}}
  .ll.build{{color:#9cdcfe;}}
  .ll.bmark{{color:#7fd1ff;font-weight:600;}}
  .ll.bok{{color:#56d364;font-weight:600;}}
  .ll.bfail{{color:#ff8a80;font-weight:600;}}
  #hud{{position:fixed;bottom:12px;left:12px;background:rgba(0,0,0,.65);padding:3px 8px;border-radius:6px;pointer-events:none;font-size:11px;z-index:5;}}
  #status{{align-self:center;white-space:nowrap;flex:0 0 auto;padding:3px 8px;border-radius:6px;font-size:11px;color:#9c9;}}
  #logbody .empty{{color:#666;font-style:italic;padding:6px 0;}}
</style>
</head><body>
<div id="hud">sim {W_PTS}x{H_PTS}pt · click=tap · drag=swipe</div>
<div id="runbar">
  <button id="runbtn" title="Build, install &amp; launch on the simulator (like Xcode Run)">▶ Run</button>
  <span id="runstate">…</span>
  <span id="runtitle">{_run_title}</span>
  <select id="simpicker" title="Switch simulator (boot + re-point the preview)"></select>
</div>
<div id="wrap">
  <div id="sim"><img id="screen" src="/stream" alt="sim"/></div>
  <div id="logs">
    <div id="logbar">
      <select id="logmode" title="Which logs to show">
        <option value="app"{_app_selected}>App logs</option>
        <option value="all"{_all_selected}>All logs</option>
      </select>
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
const logmodeSel = document.getElementById('logmode');
let paused = false;
const MAX_LINES = 600;
let bufferOverflow = [];
let autoscroll = true;
// Pin to the newest line unless the user scrolls up; resume when back at bottom.
logbody.addEventListener('scroll', () => {{
  autoscroll = logbody.scrollTop + logbody.clientHeight >= logbody.scrollHeight - 4;
}});

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
  if (autoscroll) logbody.scrollTop = logbody.scrollHeight;
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

let currentEs = null;
function openSse() {{
  if (currentEs) {{ try {{ currentEs.close(); }} catch (e) {{}} }}
  const es = new EventSource('/logs/stream?mode=' + encodeURIComponent(logmodeSel.value));
  currentEs = es;
  es.onopen = () => {{ status.textContent = 'logs live'; status.style.color = '#9c9'; }};
  es.onerror = () => {{
    status.textContent = 'logs reconnecting';
    status.style.color = '#fc9';
    es.close();
    if (currentEs === es) setTimeout(openSse, 2000);
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
logmodeSel.addEventListener('change', () => {{
  bufferOverflow = [];
  logbody.innerHTML = '<div class="empty">switching to ' + logmodeSel.value + ' logs…</div>';
  openSse();
}});
openSse();

/* ---- Run / Stop (build + install + launch, Xcode-style) ---- */
const runbtn = document.getElementById('runbtn');
const runstate = document.getElementById('runstate');
let runState = 'idle';
const STATE_LABEL = {{idle:'Stopped', building:'Building…', running:'Running', failed:'Build failed'}};
const STATE_COLOR = {{idle:'#999', building:'#ffcc80', running:'#9c9', failed:'#ff8a80'}};
function setRunState(s) {{
  runState = s;
  runstate.textContent = STATE_LABEL[s] || s;
  runstate.style.color = STATE_COLOR[s] || '#999';
  const active = (s === 'building' || s === 'running');
  runbtn.textContent = active ? '⏹ Stop' : '▶ Run';
  runbtn.classList.toggle('stop', active);
  runbtn.disabled = false;
}}
runbtn.addEventListener('click', async () => {{
  const path = (runState === 'building' || runState === 'running') ? '/stop' : '/run';
  runbtn.disabled = true;
  try {{ await fetch(path, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:'{{}}'}}); }}
  catch (e) {{ console.warn(e); runbtn.disabled = false; }}
}});
function appendBuild(line) {{
  const empty = logbody.querySelector('.empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  let cls = 'll build';
  if (line.indexOf('── Build succeeded') === 0) cls = 'll bok';
  else if (line.indexOf('── Build FAILED') === 0) cls = 'll bfail';
  else if (line.indexOf('──') === 0) cls = 'll bmark';
  div.className = cls;
  div.textContent = line;
  logbody.appendChild(div);
  while (logbody.children.length > MAX_LINES) logbody.removeChild(logbody.firstChild);
  if (autoscroll) logbody.scrollTop = logbody.scrollHeight;
}}
function openBuildSse() {{
  const es = new EventSource('/build/stream');
  es.addEventListener('build', e => {{ try {{ appendBuild(JSON.parse(e.data)); }} catch (_e) {{}} }});
  es.addEventListener('state', e => {{ try {{ setRunState(JSON.parse(e.data)); }} catch (_e) {{}} }});
}}
fetch('/status').then(r => r.json()).then(d => setRunState(d.state)).catch(() => setRunState('idle'));
openBuildSse();

/* ---- Simulator picker (switch the live preview to another sim) ---- */
const simpicker = document.getElementById('simpicker');
async function loadSims() {{
  try {{
    const d = await (await fetch('/sims')).json();
    simpicker.innerHTML = '';
    for (const s of d.sims) {{
      const o = document.createElement('option');
      o.value = s.udid;
      o.textContent = s.name + ' · ' + s.os + (s.booted ? ' ●' : '');
      if (s.udid === d.current) o.selected = true;
      simpicker.appendChild(o);
    }}
  }} catch (e) {{ console.warn(e); }}
}}
simpicker.addEventListener('change', async () => {{
  const udid = simpicker.value;
  runstate.textContent = 'switching sim…'; runstate.style.color = '#ffcc80';
  simpicker.disabled = true;
  try {{
    const d = await (await fetch('/sim', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{udid}})}})).json();
    if (d && d.w && d.h) document.getElementById('hud').textContent = 'sim ' + d.w + 'x' + d.h + 'pt · click=tap · drag=swipe';
    img.src = '/stream?t=' + Date.now();   // reconnect MJPEG to the new sim
    logbody.innerHTML = '<div class="empty">switched simulator — interact with the app or click Run</div>';
    openSse();                              // reconnect logs to the new sim
  }} catch (e) {{ console.warn(e); }}
  simpicker.disabled = false;
  await loadSims();                         // refresh booted markers + selection
  fetch('/status').then(r => r.json()).then(d => setRunState(d.state)).catch(() => setRunState('idle'));
}});
loadSims();
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


class RunController:
    """Thread-safe state machine for the in-pane Run/Stop ("play") button.

    States: "idle" (app stopped), "building", "running", "failed". Run does a
    full rebuild + install + launch via `run-ios.sh --no-stream` (Xcode-style);
    Stop terminates the app. Build output is streamed to /build/stream
    subscribers (SSE), replayed from a ring buffer for late joiners, and
    mirrored to stdout so the assistant reading the preview can see it too.
    Only one build runs at a time; Run while running = restart.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = "idle"
        self._build_proc = None
        self._buffer = collections.deque(maxlen=500)
        self._subs = []
        self._subs_lock = threading.Lock()

    # ---- SSE subscriber registry ----
    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._subs_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def _emit(self, event: str, data: str):
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass

    def snapshot(self) -> str:
        with self._lock:
            return self._state

    def replay(self):
        with self._lock:
            return list(self._buffer), self._state

    def _log(self, line: str):
        with self._lock:
            self._buffer.append(line)
        self._emit("build", line)
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass

    def _set_state(self, state: str):
        with self._lock:
            self._state = state
        self._emit("state", state)

    # ---- actions ----
    def run(self):
        with self._lock:
            if self._state == "building":
                return 409, {"ok": False, "state": "building",
                             "error": "build already in progress"}
            self._state = "building"
            self._buffer.clear()
        self._emit("state", "building")
        threading.Thread(target=self._do_run, daemon=True).start()
        return 202, {"ok": True, "state": "building"}

    def _do_run(self):
        # Restart semantics: kill the app first if it is already running.
        if BUNDLE_ID:
            subprocess.run(
                ["xcrun", "simctl", "terminate", SIM, BUNDLE_ID],
                capture_output=True, text=True,
            )
        if not os.path.isfile(RUN_IOS):
            self._log(f"run-ios.sh not found at {RUN_IOS}")
            self._set_state("failed")
            return
        self._log(f"── Building {SCHEME or BUNDLE_ID or 'app'}… ──")
        # The launch.json-baked IOS_* is the source of truth and MUST match the
        # simulator this server streams. Point IOS_ENV_FILE at /dev/null so
        # run-ios.sh does not re-source a possibly-stale
        # <project>/.claude/ios-preview.env that could override IOS_SIM_UDID with
        # a different simulator (which would launch the app off-screen).
        run_env = os.environ.copy()
        run_env["IOS_ENV_FILE"] = "/dev/null"
        run_env["IOS_SIM_UDID"] = SIM  # build/launch on the currently-selected sim
        try:
            proc = subprocess.Popen(
                ["bash", RUN_IOS, "--no-stream"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=run_env,
            )
        except OSError as exc:
            self._log(f"failed to start build: {exc}")
            self._set_state("failed")
            return
        with self._lock:
            self._build_proc = proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                self._log(line.rstrip("\n"))
        except (BrokenPipeError, ValueError):
            pass
        rc = proc.wait()
        with self._lock:
            self._build_proc = None
        if rc == 0:
            self._log("── Build succeeded — app launched ──")
            self._set_state("running")
        else:
            self._log(f"── Build FAILED (exit {rc}) — see errors above ──")
            self._set_state("failed")

    def stop(self):
        with self._lock:
            proc = self._build_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if BUNDLE_ID:
            r = subprocess.run(
                ["xcrun", "simctl", "terminate", SIM, BUNDLE_ID],
                capture_output=True, text=True,
            )
            msg = (r.stderr or r.stdout or "").strip()
        else:
            msg = "IOS_BUNDLE_ID not set; cannot terminate app"
        self._log("── Stopped ──")
        self._set_state("idle")
        return 200, {"ok": True, "state": "idle", "msg": msg}

    def cleanup(self):
        with self._lock:
            proc = self._build_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


RUN = RunController()


def _check_origin(handler: "Handler") -> bool:
    """S2: CSRF origin check for POST routes.

    Allow when the Origin is absent (CLI/curl, same-origin form posts), when the
    Origin host is loopback (localhost / 127.0.0.1 / ::1) on ANY port, or when
    IOS_PREVIEW_ALLOW_ORIGIN is "*" or matches the Origin exactly. Reject
    everything else (e.g. http://evil.com). Relaxed from an exact host:port match
    so taps AND the Run/Stop button keep working when Claude's preview pane
    proxies the page under a loopback origin on a different port.
    """
    origin = handler.headers.get("Origin", "")
    if not origin:
        return True
    if ALLOW_ORIGIN and (ALLOW_ORIGIN == "*" or origin == ALLOW_ORIGIN):
        return True
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    return host in ("localhost", "127.0.0.1", "::1")


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

    def _sse(self, event: str, data):
        self.wfile.write(f"event: {event}\n".encode())
        self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def do_GET(self):
        if self.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/status":
            return self._send_json(200, {
                "state": RUN.snapshot(),
                "scheme": SCHEME,
                "bundleId": BUNDLE_ID,
                "hasBundleId": bool(BUNDLE_ID),
            })
        elif self.path == "/sims":
            return self._send_json(200, {"current": SIM, "sims": list_sims()})
        elif self.path == "/build/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = RUN.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                lines, state = RUN.replay()
                for ln in lines:
                    self._sse("build", ln)
                self._sse("state", state)
                while True:
                    try:
                        event, data = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    self._sse(event, data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                RUN.unsubscribe(q)
        elif self.path.split("?", 1)[0] == "/logs/stream":
            # Optional ?mode=app|all from the toolbar dropdown selects how much
            # to show; LOG_PREDICATE / IOS_LOG_SUBSYSTEM still take precedence.
            _qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            _mode = (_qs.get("mode") or [None])[0]
            predicate = build_predicate(_mode)
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
                    "--predicate", predicate,
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
        elif self.path.split("?", 1)[0] == "/stream":
            # Lower latency: disable Nagle so each MJPEG chunk ships immediately.
            # (Tolerates a ?t=… cache-buster the client adds when switching sims.)
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
        if self.path == "/run":
            code, payload = RUN.run()
            return self._send_json(code, payload)
        if self.path == "/stop":
            code, payload = RUN.stop()
            return self._send_json(code, payload)
        if self.path == "/sim":
            result = switch_sim(str(body.get("udid", "")))
            return self._send_json(200 if result.get("ok") else 400, result)
        return self._send_json(404, {"error": "unknown route"})


class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


_mirror_proc = None
_mirror_lock = threading.Lock()


def _mirror_pump(proc):
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
    except Exception:
        pass


def _spawn_mirror():
    """Start a persistent `simctl log stream` mirror for the CURRENT sim.

    Caller holds _mirror_lock. Each SSE client gets its own stream; this extra
    mirror means the assistant reading the preview's stdout sees the same lines
    visible in the browser. Re-pointed by restart_log_mirror() on a sim switch.
    """
    global _mirror_proc
    _mirror_proc = subprocess.Popen(
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
    threading.Thread(target=_mirror_pump, args=(_mirror_proc,), daemon=True).start()


def _kill_mirror():
    with _mirror_lock:
        proc = _mirror_proc
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def restart_log_mirror():
    """Re-point the stdout log mirror at the current sim (after a sim switch)."""
    _kill_mirror()
    with _mirror_lock:
        _spawn_mirror()


def start_log_mirror():
    with _mirror_lock:
        _spawn_mirror()

    def _cleanup(*_args):
        RUN.cleanup()
        _kill_mirror()

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
